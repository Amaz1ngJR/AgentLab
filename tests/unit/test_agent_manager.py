"""Runtime delegation integration with independent deterministic clients."""
import json
import threading
import time

import pytest

from app.agent.agent_manager import AgentManager, DelegationRegistry
from app.agent.approval import AutoApprove
from app.agent.cancel import CancelToken, Cancelled
from app.agent.delegation_policy import delegation_allowed
from app.agent.runtime import AgentSession
from app.models.protocol import ModelResponse, ToolCall, ToolResult
from app.tools.registry import ToolExecutionError
from tests.unit.test_execution_plan import FakeRouter, registry, response


class ForkRouter(FakeRouter):
    def fork(self):
        return FakeRouter([response("CHILD_FINDING")])


def manager(tmp_path, **kwargs):
    return AgentManager(llm=ForkRouter([]), tools=registry(), approval=AutoApprove(),
                        workspace=tmp_path, cancel=CancelToken(), allowed=True, **kwargs)


def test_parallel_nonblocking_spawn_and_id_isolation(tmp_path):
    gate = threading.Event()
    barrier = threading.Barrier(3)

    class Child(FakeRouter):
        def create_message(self, messages, **kwargs):
            barrier.wait(timeout=2)
            gate.wait(2)
            return response("parallel finding")

    class Parent(ForkRouter):
        def fork(self):
            return Child([])

    m = manager(tmp_path)
    m.llm = Parent([])
    try:
        a = m.spawn("调查 A")["child_run_id"]
        b = m.spawn("调查 B")["child_run_id"]
        barrier.wait(timeout=2)  # Both children run before parent waits.
        assert m.wait([a, b], 0)["wait_timed_out"]
        with pytest.raises(ToolExecutionError, match="busy"):
            m.spawn("third")
        other = manager(tmp_path)
        with pytest.raises(ToolExecutionError, match="unknown"):
            other.wait([a], 0)
        other.finish()
        gate.set()
        for sid in [a, b]:
            assert m.wait([sid], 2)["agents"][0]["status"] == "succeeded"
        usage = dict(m.usage)
        m.wait([a, b], 0)
        assert m.usage == usage == {"input_tokens": 2, "output_tokens": 2}
    finally:
        gate.set()
        m.finish()


def test_close_is_idempotent_and_wait_is_cancellable(tmp_path):
    m = manager(tmp_path)
    sid = m.spawn("调查")["child_run_id"]
    m.wait([sid], 2)
    assert m.close(sid) == m.close(sid)
    m.finish()
    with pytest.raises(ToolExecutionError, match="disabled"):
        m.spawn("new")


@pytest.mark.parametrize("policy,goal,allowed", [
    ("off", "多个 agent 并行", False), ("explicit", "深入分析架构", False),
    ("explicit", "多个 agent 并行调查", True), ("explicit", "delegate to subagents", True),
    ("explicit", "不要使用多个 agent", False), ("auto", "调查架构", True),
])
def test_policy(policy, goal, allowed):
    assert delegation_allowed(policy, goal) is allowed


@pytest.mark.parametrize("orchestrate", [False, True])
def test_direct_and_task_can_spawn_wait_and_consume_result(orchestrate):
    class Parent(ForkRouter):
        def create_message(self, messages, **kwargs):
            self.calls.append({"messages": json.loads(json.dumps(messages)), **kwargs})
            names = [t["name"] for t in kwargs.get("tools") or []]
            if not names:  # Parent Planner
                return response(json.dumps({"tasks": [{"id": "t", "content": "调查并汇总"}]}))
            text = json.dumps(messages, ensure_ascii=False)
            if "child_run_id" not in text:
                return ModelResponse("", [ToolCall("spawn", "spawn_agent", {"instruction": "总结"})], {}, [])
            if "CHILD_FINDING" not in text:
                for message in messages:
                    for block in message.get("content", []) if isinstance(message.get("content"), list) else []:
                        if block.get("type") == "tool_result" and block.get("tool_use_id") == "spawn":
                            sid = json.loads(block["content"])["child_run_id"]
                            return ModelResponse("", [ToolCall("wait", "wait_agent", {"ids": [sid], "timeout": 2})], {}, [])
            return response("综合 CHILD_FINDING")

    router = Parent([])
    session = AgentSession(router, registry(), orchestrate=orchestrate, max_steps=12)
    assert session.chat("用多个 agent 调查") == "综合 CHILD_FINDING"
    assert session.child_records[0]["status"] == "succeeded"
    assert "spawn_agent" not in [t.name for t in session.tools.all()]


def test_off_rejects_tool_even_if_replayed(tmp_path):
    m = manager(tmp_path)
    m.allowed = False
    tools = DelegationRegistry(registry(), m)
    assert "spawn_agent" not in [s["name"] for s in tools.schemas()]
    assert tools.execute("spawn_agent", {"instruction": "test"})[1]
    m.finish()


def test_storage_roundtrip_preserves_execution_and_marks_interrupted(tmp_path):
    from app.storage import Storage
    db = Storage(tmp_path / "state.db")
    db.create_session("s", "default", "default")
    task = {"id": "t", "content": "child", "execution": {"executor": "subagent", "consumed": {"model_rounds": 3}},
            "retry_of": "old"}
    db.save_tasks("s", [task])
    restored = db.load_tasks("s")[0]
    assert restored["execution"] == task["execution"]
    assert restored["retry_of"] == "old"
    db.save_child_runs("s", [{"child_run_id": "c", "status": "running"}])
    assert db.load_child_runs("s")[0]["status"] == "interrupted"
    db.close()


def test_planner_children_run_parallel_and_parent_consumes_dependencies():
    barrier = threading.Barrier(2)
    class Child(FakeRouter):
        def create_message(self, messages, **kwargs):
            barrier.wait(timeout=2)
            return response("PLAN_CHILD_EVIDENCE")
    class Parent(ForkRouter):
        def fork(self):
            return Child([])
    tasks = [{"id": sid, "content": "总结", "executor": "subagent",
              "subagent": {"id": "child-" + sid, "role": "research", "read_only": True,
                           "instruction": "总结", "max_steps": 2, "max_tool_calls": 2}}
             for sid in ["a", "b"]]
    tasks.append({"id": "c", "content": "汇总", "dependencies": ["a", "b"]})
    parent = Parent([response(json.dumps({"version": 1, "tasks": tasks})), response("汇总完成")])
    session = AgentSession(parent, registry(), orchestrate=True, max_steps=12)
    assert session.chat("多个 agent 并行调查并汇总") == "汇总完成"
    assert all(task["status"] == "completed" for task in session.task_store.snapshot())
    assert "PLAN_CHILD_EVIDENCE" in json.dumps(parent.calls[-1]["messages"])


def test_steering_is_targeted_and_parent_mailbox_untouched(tmp_path):
    entered = threading.Event()
    release = threading.Event()
    calls = []
    class Child(FakeRouter):
        def create_message(self, messages, **kwargs):
            calls.append(json.dumps(messages))
            if len(calls) == 1:
                entered.set()
                release.wait(2)
            return response("done")
    class Parent(ForkRouter):
        def fork(self):
            return Child([])
    m = manager(tmp_path)
    m.llm = Parent([])
    sid = m.spawn("总结")["child_run_id"]
    assert entered.wait(2)
    m.cancel.steer("PARENT_ONLY")
    assert m.send_input(sid, "CHILD_ONLY")["accepted"]
    release.set()
    m.cancel.drain_steering()
    assert m.wait([sid], 2)["agents"][0]["status"] == "succeeded"
    assert "CHILD_ONLY" in calls[-1]
    assert all("PARENT_ONLY" not in call for call in calls)
    m.finish()


def test_task_parent_rounds_share_child_budget(tmp_path):
    from app.agent.executor import Executor
    from app.agent.tasks import Task
    m = manager(tmp_path, model_rounds=3)
    tools = DelegationRegistry(registry(), m)
    m.charge_parent_round()  # planning
    executor = Executor(FakeRouter([response("one"), response("two")]), tools, AutoApprove())
    executor.run_task(Task("a", "调查"), [], system="", max_steps=1)
    executor.run_task(Task("b", "调查"), [], system="", max_steps=1)
    with pytest.raises(Cancelled, match="aggregate model"):
        executor.run_task(Task("c", "调查"), [], system="", max_steps=1)
    m.finish()


def test_spawn_reserves_a_parent_round_and_preserves_readonly_executor(tmp_path):
    m = manager(tmp_path, model_rounds=3)
    with pytest.raises(ToolExecutionError, match="budget_exhausted"):
        m.spawn("all", max_steps=3)
    sid = m.spawn("调查", role="executor", read_only=True, max_steps=2,
                  timeout_seconds=2, output_limit=5)["child_run_id"]
    snapshot = m.wait([sid], 2)["agents"][0]
    assert snapshot["status"] == "succeeded"
    assert snapshot["summary"] == "CHILD"
    assert m.children[sid].worktree is None
    assert "write_file" not in m.children[sid].spec.tools
    m.finish()


def test_taskstore_cannot_fall_back_to_sync_child_when_capacity_full():
    from app.agent.tasks import TaskStore, Task
    store = TaskStore()
    store.extend([Task("child", "child", execution={"executor": "subagent"}), Task("parent", "main")])
    assert store.claim_next(exclude_executors=frozenset({"subagent"})).id == "parent"
    assert store.get("child").status == "pending"


@pytest.mark.parametrize("policy,goal,workflow,expected", [
    ("explicit", "调查架构", "使用多个 agent 分别调查模块", True),
    ("explicit", "调查架构", "不要使用多个 agent", False),
    ("off", "调查架构", "使用多个 agent", False),
    ("auto", "不要使用多个 agent", "使用多个 agent", False),
])
def test_applicable_workflow_cannot_override_policy_or_user_veto(policy, goal, workflow, expected):
    assert delegation_allowed(policy, goal, workflow=workflow) is expected


def test_internal_wait_and_repeated_notifications_deliver_once(tmp_path):
    m = manager(tmp_path)
    sid = m.spawn("调查")["child_run_id"]
    m.wait([sid], 2, consume=False)
    messages = []
    assert m.deliver(messages)
    assert not m.deliver(messages)
    m.wait([sid], 0, consume=False)
    m.close(sid)
    assert not m.deliver(messages)
    assert len(messages) == 1
    m.finish()


def test_interrupt_preserves_readonly_role_timeout_and_output_budget(tmp_path):
    m = manager(tmp_path)
    sid = m.spawn("调查", role="executor", read_only=True, timeout_seconds=3,
                  output_limit=5, allowed_tools=["read_file"])["child_run_id"]
    m.wait([sid], 2)
    replacement = m.send_input(sid, "补充", "interrupt")["child_run_id"]
    m.wait([replacement], 2)
    spec = m.children[replacement].spec
    assert spec.read_only and spec.tools == ("read_file",)
    assert spec.timeout_seconds <= 3 and spec.output_limit == 5
    assert m.snapshot(sid)["superseded_by"] == replacement
    assert not m.unresolved()
    m.finish()


def test_finish_does_not_claim_noncooperative_worker_stopped(tmp_path, monkeypatch):
    from app.agent.subagent_runtime import SubagentRuntimeFactory
    from app.agent.subagents import SubagentResult
    entered, release = threading.Event(), threading.Event()
    def stuck(self, spec, workspace, cancel):
        entered.set()
        release.wait(3)
        return SubagentResult(spec.subagent_id, spec.role, "cancelled")
    monkeypatch.setattr(SubagentRuntimeFactory, "delegate", stuck)
    m = manager(tmp_path)
    try:
        sid = m.spawn("调查")["child_run_id"]
        assert entered.wait(2)
        start = time.monotonic()
        m.finish()
        assert time.monotonic() - start < 1
        assert m.snapshot(sid)["status"] == "cancelling"
        assert m.unresolved()
    finally:
        release.set()
        m.children[sid].done.wait(2)
        m.pump()
    assert m.snapshot(sid)["status"] == "cancelled"


def test_restore_preserves_old_consumption_without_billing_twice(tmp_path):
    m = manager(tmp_path)
    sid = m.spawn("调查")["child_run_id"]
    m.wait([sid], 2)
    old = m.records()
    m.finish()
    restored = manager(tmp_path)
    restored.restore_results(old)
    new = restored.records()[0]
    assert new["consumed"] == old[0]["consumed"]
    assert new["usage"] == old[0]["usage"]
    assert new["parent_run_id"] == old[0]["parent_run_id"]
    assert restored.usage == {"input_tokens": 0, "output_tokens": 0}
    restored.finish()


def test_resume_obeys_readonly_retry_limit_and_keeps_write_blocked():
    from app.agent.tasks import BLOCKED, FAILED, PENDING, Task, TaskStore
    store = TaskStore()
    store.extend([Task("zero", "read", FAILED, execution={"executor": "subagent",
                      "subagent": {"read_only": True, "retry_limit": 0}}),
                  Task("one", "read", FAILED, execution={"executor": "subagent",
                      "subagent": {"read_only": True, "retry_limit": 1},
                      "consumed": {"model_rounds": 2}})])
    assert store.reset_for_resume() == 1
    assert store.get("zero").status == BLOCKED
    assert store.get("one").status == PENDING
    assert store.get("one").execution["consumed"] == {"model_rounds": 2}
    store.update_status("one", FAILED)
    assert store.reset_for_resume() == 0
    assert store.get("one").status == BLOCKED


def test_all_child_plan_gets_a_real_parent_synthesis():
    tasks = [{"id": sid, "content": "调查", "executor": "subagent", "subagent": {
        "id": sid, "role": "research", "read_only": True, "instruction": "调查",
        "max_steps": 2, "max_tool_calls": 2}} for sid in ["a", "b"]]
    parent = ForkRouter([response(json.dumps({"version": 1, "tasks": tasks})), response("PARENT_SYNTHESIS")])
    session = AgentSession(parent, registry(), orchestrate=True, max_steps=12)
    assert session.chat("多个 agent 调查") == "PARENT_SYNTHESIS"
    assert "CHILD_FINDING" in json.dumps(parent.calls[-1]["messages"])
    assert session.last_run_status == "completed"


def test_bounded_result_retains_findings_and_verification_tail():
    from app.agent.subagent_runtime import bounded_result
    text = "CONCLUSION" + "x" * 1000 + "VERIFIED: failed; unresolved: auth"
    result = bounded_result(text, 100)
    assert len(result) <= 100 and result.startswith("CONCLUSION")
    assert result.endswith("VERIFIED: failed; unresolved: auth")
    for limit in range(1, 50):
        assert len(bounded_result(text, limit)) <= limit


def test_enabled_matching_skill_can_authorize_but_not_off_or_next_turn():
    from app.skills.catalog import SkillCatalog
    from app.skills.loader import Skill
    skill = Skill("parallel", "parallel", workflow="使用多个 agent 并行调查", triggers=["架构"], enabled=True)
    session = AgentSession(ForkRouter([]), registry(), orchestrate=False)
    session.skill_catalog = SkillCatalog({"parallel": skill})
    session._prepare_skill_context("调查架构")
    assert delegation_allowed("explicit", "调查架构", workflow=session._delegation_workflow)
    assert not delegation_allowed("off", "调查架构", workflow=session._delegation_workflow)
    session._prepare_skill_context("打招呼")
    assert not session._delegation_workflow


def test_spawn_default_budget_works_with_default_session_round_limit(tmp_path):
    m = manager(tmp_path, model_rounds=8)
    m.charge_parent_round()
    m.charge_parent_round()
    sid = m.spawn("调查")["child_run_id"]
    assert m.children[sid].spec.max_steps == 2  # Two free child slots share five rounds.
    m.wait([sid], 2)
    m.finish()


def test_planned_parent_continues_while_child_runs_and_dependents_wait():
    started, released = threading.Event(), threading.Event()
    calls = []
    class Child(FakeRouter):
        def create_message(self, messages, **kwargs):
            started.set()
            assert released.wait(2)
            return response("AFTER_PARENT_WORK")
    class Parent(ForkRouter):
        def fork(self):
            return Child([])
        def create_message(self, messages, **kwargs):
            if not kwargs.get("tools"):
                return response(json.dumps({"version": 1, "tasks": [
                    {"id": "child", "content": "调查", "executor": "subagent", "subagent": {
                        "id": "c", "role": "research", "read_only": True, "instruction": "调查", "max_steps": 2}},
                    {"id": "parent", "content": "独立分析"},
                    {"id": "dependent", "content": "汇总", "dependencies": ["child", "parent"]},
                ]}))
            calls.append(json.dumps(messages))
            if len(calls) == 1:
                assert started.wait(2)
                assert not released.is_set()
                released.set()
                return response("parent worked during child execution")
            assert "AFTER_PARENT_WORK" in calls[-1]
            return response("combined")
    session = AgentSession(Parent([]), registry(), orchestrate=True, max_steps=16)
    assert session.chat("多个 agent 并行调查") == "combined"
    assert session.last_run_status == "completed"


def test_child_failure_blocks_only_its_dependents_and_keeps_other_findings():
    class Parent(ForkRouter):
        count = 0
        def fork(self):
            self.count += 1
            if self.count == 1:
                class Broken(FakeRouter):
                    def create_message(self, messages, **kwargs):
                        raise RuntimeError("child failed")
                return Broken([])
            return FakeRouter([response("INDEPENDENT_FINDING")])
    tasks = [{"id": sid, "content": "调查", "executor": "subagent", "subagent": {
        "id": sid, "role": "research", "read_only": True, "instruction": "调查", "max_steps": 2}}
        for sid in ["bad", "good"]]
    tasks.extend([{"id": "blocked", "content": "汇总失败任务", "dependencies": ["bad"]},
                  {"id": "independent", "content": "汇总成功任务", "dependencies": ["good"]}])
    parent = Parent([response(json.dumps({"version": 1, "tasks": tasks})), response("partial summary")])
    session = AgentSession(parent, registry(), orchestrate=True, max_steps=16)
    session.chat("多个 agent 并行调查")
    states = {t["id"]: t["status"] for t in session.task_store.snapshot()}
    assert states == {"bad": "failed", "good": "completed", "blocked": "pending", "independent": "completed"}
    assert session.last_run_status == "blocked"
    assert "INDEPENDENT_FINDING" in json.dumps(parent.calls[-1]["messages"])


def test_recovery_does_not_reset_aggregate_budget(tmp_path):
    m = manager(tmp_path, model_rounds=8, tool_calls=40)
    sid = m.spawn("调查", max_steps=2, max_tool_calls=3)["child_run_id"]
    m.wait([sid], 2)
    m.charge_parent_round()
    m.charge_parent_tool()
    records = m.records()
    expected = (m.rounds_left, m.tools_left)
    m.finish()
    recovered = manager(tmp_path, model_rounds=32, tool_calls=120)
    recovered.restore_results(records)
    assert (recovered.rounds_left, recovered.tools_left) == expected
    assert recovered.deadline <= time.monotonic() + records[-1]["budget"]["seconds_left"]
    recovered.finish()


def test_planned_readonly_retry_uses_same_task_permissions_and_remaining_budget():
    class Parent(ForkRouter):
        count = 0
        def fork(self):
            self.count += 1
            if self.count == 1:
                class Broken(FakeRouter):
                    def create_message(self, messages, **kwargs):
                        raise RuntimeError("temporary failure")
                return Broken([])
            return FakeRouter([response("RECOVERED_FINDING")])
    plan = {"version": 1, "tasks": [{"id": "a", "content": "调查", "executor": "subagent",
        "subagent": {"id": "c", "role": "research", "read_only": True, "instruction": "调查",
                     "tools": ["read_file"], "max_steps": 4, "max_tool_calls": 3, "retry_limit": 1}}]}
    session = AgentSession(Parent([response(json.dumps(plan))]), registry(), orchestrate=True, max_steps=16)
    session.chat("多个 agent 调查")
    task = session.task_store.get("a")
    assert task.status == "completed" and task.execution["retries"] == 1
    assert task.execution["consumed"]["model_rounds"] == 2
    assert len(session.child_records) == 2
    assert all(record["spec"]["tools"] == ("read_file",) for record in session.child_records)
    assert session.last_run_status == "completed"


def test_child_approvals_are_serialized_with_child_workspace_and_cancellation(tmp_path):
    import queue
    from app.agent.child_approval import ChildApprovalPolicy, service_child_approvals
    from app.agent.approval import ApprovalResult
    from app.config.loader import use_workspace_root, workspace_root
    from app.tools.registry import Tool
    requests = queue.Queue()
    seen = []
    owner = threading.get_ident()
    class Policy:
        def request_tool(self, tool, action, args):
            assert threading.get_ident() == owner
            seen.append((workspace_root(), tool.description, args["path"]))
            return ApprovalResult(True)
    policies = [ChildApprovalPolicy(Policy(), requests, CancelToken(), workspace=tmp_path / name,
                                   child_run_id=name) for name in ["a", "b"]]
    results = []
    tool = Tool("write_file", "write", {}, lambda args: "")
    def ask(policy):
        try:
            results.append(policy.request_tool(tool, "write_file", {"path": "relative.txt"}))
        except Cancelled:
            pass
    workers = [threading.Thread(target=ask, args=(policy,)) for policy in policies]
    for worker in workers:
        worker.start()
    deadline = time.monotonic() + 2
    while requests.qsize() < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert all(p.waiting.is_set() for p in policies)
    policies[1].cancel.cancel()
    # Avoid an unhandled thread exception for this cancelled request.
    with use_workspace_root(tmp_path):
        service_child_approvals(requests)
        assert workspace_root() == tmp_path
    for worker in workers:
        worker.join(2)
    assert seen == [(tmp_path / "a", "[child a; workspace " + str(tmp_path / "a") + "] write", "relative.txt")]
    assert results[0].approved
    assert not any(p.waiting.is_set() for p in policies)


def test_restored_result_delivery_is_idempotent_across_sqlite_restart(tmp_path):
    from app.storage import Storage
    m = manager(tmp_path)
    sid = m.spawn("调查")["child_run_id"]
    m.wait([sid], 2, consume=False)
    history = []
    assert m.deliver(history)
    db = tmp_path / "state.db"
    storage = Storage(db)
    storage.create_session("s", "default", "fake")
    storage.save_child_runs("s", m.records())
    storage.save_messages("s", history)
    m.finish()
    storage.close()
    storage = Storage(db)
    recovered = manager(tmp_path)
    history = storage.load_messages("s")
    recovered.restore_results(storage.load_child_runs("s"), messages=history)
    assert recovered.run_id == m.run_id
    assert not recovered.deliver(history)
    assert len(history) == 1
    missing_history = manager(tmp_path)
    missing_history.restore_results(storage.load_child_runs("s"), messages=[])
    recovered_history = []
    assert missing_history.deliver(recovered_history)  # checkpoint outran parent history save
    assert len(recovered_history) == 1
    missing_history.finish()
    recovered.finish()
    storage.close()


def test_fail_fast_cancels_active_sibling_and_does_not_dispatch_later_work():
    from app.tools.registry import Tool
    from app.tools.registry import current_cancel_token
    sibling_started = threading.Event()
    class Parent(ForkRouter):
        count = 0
        def fork(self):
            self.count += 1
            if self.count == 1:
                class Broken(FakeRouter):
                    def create_message(self, messages, **kwargs):
                        assert sibling_started.wait(2)
                        raise RuntimeError("fail fast")
                return Broken([])
            return FakeRouter([ModelResponse("", [ToolCall("read", "read_file", {})], {}, [])])
    tools = registry()
    def blocking_read(args):
        sibling_started.set()
        token = current_cancel_token()
        assert token.wait(2)
        token.raise_if_cancelled()
    tools.register(Tool("read_file", "read", {}, blocking_read))
    tasks = [{"id": sid, "content": "调查", "executor": "subagent", "subagent": {
        "id": sid, "role": "research", "read_only": True, "instruction": "调查", "max_steps": 2}}
        for sid in ["bad", "sibling", "never"]]
    parent = Parent([response(json.dumps({"version": 1, "execution": {"failure_policy": "fail_fast"}, "tasks": tasks}))])
    session = AgentSession(parent, tools, orchestrate=True, max_steps=16)
    session.chat("多个 agent 调查")
    assert session.last_run_status == "cancelled"
    assert len(session.child_records) == 2
    assert {r["execution_status"] for r in session.child_records} == {"failed", "cancelled"}


def test_subagents_policy_rejects_main_tasks_instead_of_ignoring_mode():
    from app.agent.execution_plan import parse_execution_plan, validate_execution_plan
    plan = parse_execution_plan({"version": 1, "execution": {"mode": "subagents"},
                                 "tasks": [{"id": "main", "content": "调查"}]}, "goal")
    assert any("subagents 模式" in error for error in validate_execution_plan(plan))


def test_explicit_history_fork_is_bounded_redacted_and_not_system(tmp_path):
    calls = []
    class Child(FakeRouter):
        def create_message(self, messages, **kwargs):
            calls.append((json.loads(json.dumps(messages)), kwargs.get("system", "")))
            return response("snapshot read")
    class Parent(ForkRouter):
        def fork(self):
            return Child([])
    history = [
        {"role": "user", "content": "CONFIRMED_FACT api_key=super_secret_value"},
        {"role": "assistant", "content": [{"type": "thinking", "thinking": "PRIVATE_REASONING"},
                                              {"type": "tool_use", "id": "c", "name": "read_file", "input": {"path": "x"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c", "content": "FILE_EVIDENCE"}]},
    ]
    m = manager(tmp_path, history=history)
    m.llm = Parent([])
    sid = m.spawn("分析快照", fork_history=True)["child_run_id"]
    m.wait([sid], 2)
    content = json.dumps(calls[0][0])
    assert "CONFIRMED_FACT" in content and "FILE_EVIDENCE" in content
    assert "super_secret_value" not in content and "PRIVATE_REASONING" not in content
    assert "CONFIRMED_FACT" not in calls[0][1]
    sid2 = m.spawn("无快照")["child_run_id"]
    m.wait([sid2], 2)
    assert "CONFIRMED_FACT" not in json.dumps(calls[1][0])
    assert history[0]["content"].endswith("super_secret_value")
    m.finish()


@pytest.mark.parametrize("steps,expected", [(3, "succeeded"), (1, "budget_exhausted")])
def test_accepted_steering_racing_with_completion_is_not_discarded(tmp_path, monkeypatch, steps, expected):
    from app.agent.executor import Executor
    gate, release = threading.Event(), threading.Event()
    original = Executor.run_task
    first = True
    calls = []
    def finish_race(self, *args, **kwargs):
        nonlocal first
        outcome = original(self, *args, **kwargs)
        if first:
            first = False
            gate.set()
            assert release.wait(2)
        return outcome
    monkeypatch.setattr(Executor, "run_task", finish_race)
    class Child(FakeRouter):
        def create_message(self, messages, **kwargs):
            calls.append(json.loads(json.dumps(messages)))
            return response("FOLLOWUP_DONE" if "LATE_DIRECTION" in json.dumps(messages) else "original done")
    class Parent(ForkRouter):
        def fork(self):
            return Child([])
    m = manager(tmp_path)
    m.llm = Parent([])
    try:
        sid = m.spawn("调查", max_steps=steps)["child_run_id"]
        assert gate.wait(2)
        assert m.send_input(sid, "LATE_DIRECTION")["accepted"]
        release.set()
        result = m.wait([sid], 2)["agents"][0]
        assert result["execution_status"] == expected
        if steps > 1:
            assert len(calls) == 2 and "LATE_DIRECTION" in json.dumps(calls[-1])
            assert result["summary"] == "FOLLOWUP_DONE"
        else:
            assert "accepted steering" in result["error"]
        with pytest.raises(ToolExecutionError, match="accepting"):
            m.send_input(sid, "too late")
    finally:
        release.set()
        m.finish()


def test_mixed_plan_with_terminal_child_gets_parent_synthesis():
    calls = []
    class Parent(ForkRouter):
        def create_message(self, messages, **kwargs):
            calls.append(json.loads(json.dumps(messages)))
            if not kwargs.get("tools"):
                return response(json.dumps({"version": 1, "tasks": [
                    {"id": "prep", "content": "prepare"},
                    {"id": "research", "content": "research", "dependencies": ["prep"],
                     "executor": "subagent", "subagent": {"id": "child", "instruction": "总结已给材料",
                     "role": "research", "read_only": True, "max_steps": 2, "max_tool_calls": 2}},
                ]}))
            if "CHILD_FINDING" in json.dumps(messages):
                return response("PARENT_SYNTHESIZED_FINDING")
            return response("parent prepared")
    session = AgentSession(Parent([]), registry(), orchestrate=True, max_steps=16)
    answer = session.chat("多个 agent 调查")
    assert answer == "PARENT_SYNTHESIZED_FINDING", (session.task_store.snapshot(), calls, session.child_records)
    assert session.last_run_status == "completed"
    assert "CHILD_FINDING" in json.dumps(calls[-1])
    assert len(calls) == 3  # planner, preparation, synthesis


def test_failed_spawn_checkpoint_never_starts_a_child(tmp_path, monkeypatch):
    from app.agent.subagent_runtime import SubagentRuntimeFactory
    invoked = []
    monkeypatch.setattr(SubagentRuntimeFactory, "delegate", lambda *args: invoked.append(True))
    def fail(records):
        raise RuntimeError("checkpoint failed")
    m = manager(tmp_path, checkpoint=fail)
    rounds, tools = m.rounds_left, m.tools_left
    with pytest.raises(RuntimeError, match="checkpoint failed"):
        m.spawn("调查")
    assert not invoked and not m.children
    assert (m.rounds_left, m.tools_left) == (rounds, tools)


@pytest.mark.parametrize("field,value", [("parent_run_id", "other-run"), ("result_id", "other-child"), ("revision", 0)])
def test_delivery_receipt_is_scoped_to_run_result_and_revision(tmp_path, field, value):
    m = manager(tmp_path)
    sid = m.spawn("调查")["child_run_id"]
    m.wait([sid], 2, consume=False)
    history = []
    m.deliver(history)
    records = m.records()
    records[0]["delivery_receipt"][field] = value
    restored = manager(tmp_path)
    restored.restore_results(records, messages=history)
    assert not restored.result_delivered(sid)
    assert restored.deliver(history)
    restored.finish()
    m.finish()


def test_legacy_delivery_record_still_restores_with_matching_history(tmp_path):
    m = manager(tmp_path)
    sid = m.spawn("调查")["child_run_id"]
    m.wait([sid], 2, consume=False)
    history = []
    m.deliver(history)
    records = m.records()
    records[0].pop("delivery_receipt")
    restored = manager(tmp_path)
    restored.restore_results(records, messages=history)
    assert not restored.deliver(history)
    assert restored.records()[0]["delivery_receipt"]["revision"] == 1
    restored.finish()
    m.finish()


@pytest.mark.parametrize("orchestrate", [False, True])
@pytest.mark.parametrize("cancel_at", [None, "wait_return", "bound_output"])
def test_wait_receipt_is_acknowledged_by_direct_and_task_history_append(tmp_path, monkeypatch, orchestrate, cancel_at):
    gate = threading.Event()
    cancel = CancelToken()
    original_wait = AgentManager.wait
    def wait(self, *args, **kwargs):
        gate.set()
        result = original_wait(self, *args, **kwargs)
        if cancel_at == "wait_return":
            cancel.cancel()
            cancel.raise_if_cancelled()
        return result
    monkeypatch.setattr(AgentManager, "wait", wait)
    original_bind = AgentManager.bind_wait_tool_result
    def bind(self, *args, **kwargs):
        original_bind(self, *args, **kwargs)
        if cancel_at == "bound_output":
            cancel.cancel()
            cancel.raise_if_cancelled()
    monkeypatch.setattr(AgentManager, "bind_wait_tool_result", bind)
    class Child(FakeRouter):
        def create_message(self, messages, **kwargs):
            assert gate.wait(3)
            return response("RECEIPT_FINDING")
    class Parent(ForkRouter):
        def fork(self):
            return Child([])
        def create_message(self, messages, **kwargs):
            if not kwargs.get("tools"):
                return response(json.dumps({"tasks": [{"id": "t", "content": "总结材料"}]}))
            text = json.dumps(messages)
            if "child_run_id" not in text:
                return ModelResponse("", [ToolCall("spawn", "spawn_agent", {"instruction": "总结"})], {}, [])
            if "RECEIPT_FINDING" not in text:
                # Read the structured tool output rather than depending on provider formatting.
                for message in messages:
                    for block in message.get("content", []) if isinstance(message.get("content"), list) else []:
                        if block.get("type") == "tool_result":
                            value = json.loads(block["content"])
                            if "child_run_id" in value:
                                return ModelResponse("", [ToolCall("wait", "wait_agent", {
                                    "ids": [value["child_run_id"]], "timeout": 2})], {}, [])
                raise AssertionError("missing spawn result")
            return response("parent conclusion")
    from app.models.history import make_history_portable
    from app.storage import Storage
    db = Storage(tmp_path / "ack.db")
    db.create_session("s", "default", "fake")
    session = AgentSession(Parent([]), registry(), orchestrate=orchestrate, max_steps=12)
    session.child_checkpoint = lambda records: db.save_delegation_checkpoint(
        "s", messages=make_history_portable(session.messages), tasks=session.task_store.snapshot(), records=records)
    try:
        from app.config.loader import use_workspace_root
        with use_workspace_root(tmp_path):
            if cancel_at:
                session.chat("多个 agent 总结", cancel=cancel)
                assert session.last_run_status == "cancelled"
            else:
                assert session.chat("多个 agent 总结", cancel=cancel) == "parent conclusion"
        # Reopen SQLite: test only committed checkpoint history/receipts.
        db.close()
        db = Storage(tmp_path / "ack.db")
        records, history = db.load_child_runs("s"), db.load_messages("s")
        restored = manager(tmp_path)
        restored.restore_results(records, messages=history)
        if cancel_at:
            from app.agent.executor import INTERRUPTED_TOOL_RESULT
            assert "RECEIPT_FINDING" not in json.dumps(history)
            assert INTERRUPTED_TOOL_RESULT in json.dumps(history, ensure_ascii=False)
            assert records[0]["delivery_receipt"] is None
            assert restored.deliver(history)
            assert not restored.deliver(history)
        else:
            assert records[0]["delivery_receipt"]["revision"] == 1
            assert not restored.deliver(history)
        assert json.dumps(history).count("RECEIPT_FINDING") == 1
        restored.finish()
    finally:
        gate.set()
        db.close()


def test_truncated_wait_json_cannot_commit_receipt(tmp_path):
    from app.agent.executor import _truncate_tool_output, TOOL_OUTPUT_MAX_CHARS
    m = manager(tmp_path)
    sid = m.spawn("调查")["child_run_id"]
    result = m.wait([sid], 2)
    raw_output = json.dumps({"agents": [{**result["agents"][0],
        "summary": "L" * (TOOL_OUTPUT_MAX_CHARS + 500)}]}, ensure_ascii=False)
    output = _truncate_tool_output(raw_output)
    with pytest.raises(ValueError):
        json.loads(output)
    m.bind_wait_tool_result("call-big", output, False)
    m.acknowledge_tool_results([ToolResult(tool_call_id="call-big", output=output)])
    assert m.records()[0]["delivery_receipt"] is None
    assert not m.result_delivered(sid)
    assert m.deliver([])
    m.finish()


def test_placeholder_output_does_not_commit_receipt(tmp_path):
    """取消占位结果与绑定输出不相等,不得确认回执,且结果不丢。"""
    from app.agent.executor import INTERRUPTED_TOOL_RESULT
    m = manager(tmp_path)
    sid = m.spawn("调查")["child_run_id"]
    result = m.wait([sid], 2)
    raw_output = json.dumps(result, ensure_ascii=False)
    m.bind_wait_tool_result("call-w", raw_output, False)
    m.acknowledge_tool_results([
        ToolResult(tool_call_id="call-w", output=INTERRUPTED_TOOL_RESULT, is_error=False),
    ])
    assert not m.result_delivered(sid)
    assert m.records()[0]["delivery_receipt"] is None
    # 未确认的结果必须仍可重新投递,不能丢。
    history = []
    assert m.deliver(history)
    assert m.records()[0]["delivery_receipt"]["revision"] == 1
    m.finish()


@pytest.mark.parametrize("rejected_result", ["other_call", "error", "placeholder"])
def test_wait_acknowledgement_only_confirms_matching_successful_call(tmp_path, rejected_result):
    from app.agent.executor import INTERRUPTED_TOOL_RESULT
    m = manager(tmp_path)
    first = m.spawn("first")["child_run_id"]
    second = m.spawn("second")["child_run_id"]
    for sid in (first, second):
        m.wait([sid], 2, consume=False)
    outputs = {}
    for call_id, sid in (("wait-first", first), ("wait-second", second)):
        outputs[call_id] = json.dumps(m.wait([sid], 0))
        m.bind_wait_tool_result(call_id, outputs[call_id], False)
    rejected = ToolResult(
        tool_call_id="other-call" if rejected_result == "other_call" else "wait-second",
        output=INTERRUPTED_TOOL_RESULT if rejected_result == "placeholder" else outputs["wait-second"],
        is_error=rejected_result == "error",
    )
    m.acknowledge_tool_results([
        ToolResult(tool_call_id="wait-first", output=outputs["wait-first"]), rejected,
    ])
    receipts = {record["child_run_id"]: record["delivery_receipt"] for record in m.records()}
    assert receipts[first]["revision"] == 1
    assert receipts[second] is None
    if rejected_result == "other_call":
        # An unrelated call cannot consume the binding of a later real result.
        m.acknowledge_tool_results([ToolResult(tool_call_id="wait-second", output=outputs["wait-second"])])
        assert all(record["delivery_receipt"] for record in m.records())
    else:
        history = []
        assert m.deliver(history)
        assert second in history[0]["content"] and first not in history[0]["content"]
    m.finish()


@pytest.mark.parametrize("orchestrate", [False, True])
def test_batch_wait_retains_every_summary_and_readable_reference_after_restart(tmp_path, monkeypatch, orchestrate):
    from app.agent.executor import TOOL_OUTPUT_MAX_CHARS, _truncate_tool_output
    from app.config.loader import use_workspace_root
    from app.models.history import make_history_portable
    from app.storage import Storage
    original_wait = AgentManager.wait
    outputs = [f"FINDING_{i}_HEAD" + ('\\\"\n中文' * 1900) + f"FINDING_{i}_TAIL" for i in range(3)]
    # Finish children deterministically before the model receives spawn responses.
    original_spawn = AgentManager.spawn
    def spawn(self, *args, **kwargs):
        result = original_spawn(self, *args, **kwargs)
        original_wait(self, [result["child_run_id"]], 2, consume=False)
        return result
    monkeypatch.setattr(AgentManager, "spawn", spawn)
    # The old raw snapshots exceed the history limit by a large margin.
    batches = []
    def wait(self, ids, *args, **kwargs):
        batches.append(len(json.dumps([self.snapshot(sid) for sid in ids], ensure_ascii=False)))
        return original_wait(self, ids, *args, **kwargs)
    monkeypatch.setattr(AgentManager, "wait", wait)
    class Parent(ForkRouter):
        def __init__(self):
            super().__init__([])
            self.next_child = 0
            self.step = 0
            self.batch_output = None
        def fork(self):
            child = FakeRouter([response(outputs[self.next_child])])
            self.next_child += 1
            return child
        def create_message(self, messages, **kwargs):
            if not kwargs.get("tools"):
                return response(json.dumps({"tasks": [{"id": "t", "content": "汇总材料"}]}))
            self.step += 1
            if self.step == 1:
                return ModelResponse("", [ToolCall(f"spawn-{i}", "spawn_agent", {
                    "instruction": f"总结材料 {i}", "max_steps": 2, "max_tool_calls": 1}) for i in range(3)], {}, [])
            blocks = [block for msg in messages if isinstance(msg.get("content"), list)
                      for block in msg["content"] if block.get("type") == "tool_result"]
            if self.step == 2:
                ids = [json.loads(block["content"])["child_run_id"] for block in blocks]
                return ModelResponse("", [ToolCall("batch-wait", "wait_agent", {"ids": ids, "timeout": 0})], {}, [])
            self.batch_output = next(block["content"] for block in blocks if block["tool_use_id"] == "batch-wait")
            assert len(self.batch_output) <= TOOL_OUTPUT_MAX_CHARS
            assert _truncate_tool_output(self.batch_output) == self.batch_output
            agents = json.loads(self.batch_output)["agents"]
            assert len(agents) == 3
            for i, agent in enumerate(agents):
                assert f"FINDING_{i}_HEAD" in agent["summary"]
                assert f"FINDING_{i}_TAIL" in agent["summary"]
                assert agent["summary_truncated"]
                assert agent["result_ref"]["ids"] == [agent["result_id"]]
            return response("parent synthesis")
    db_path = tmp_path / "batch.db"
    db = Storage(db_path)
    db.create_session("s", "default", "fake")
    parent = Parent()
    session = AgentSession(parent, registry(), orchestrate=orchestrate, max_steps=20)
    session.child_checkpoint = lambda records: db.save_delegation_checkpoint(
        "s", messages=make_history_portable(session.messages), tasks=session.task_store.snapshot(), records=records)
    try:
        with use_workspace_root(tmp_path):
            assert session.chat("多个 agent 总结材料") == "parent synthesis"
        assert max(batches) > TOOL_OUTPUT_MAX_CHARS * 2
        db.close()
        db = Storage(db_path)
        history, records = db.load_messages("s"), db.load_child_runs("s")
        restored = manager(tmp_path)
        restored.restore_results(records, messages=history)
        assert len(records) == 3 and all(record["delivery_receipt"] for record in records)
        assert not restored.deliver(history)
        agents = json.loads(parent.batch_output)["agents"]
        for i, agent in enumerate(agents):
            assert agent["result_id"] in json.dumps(history)
            assert f"FINDING_{i}_HEAD" in json.dumps(history)
            assert f"FINDING_{i}_TAIL" in json.dumps(history)
            # Follow the public reference after restart and read the entire retained output.
            args = dict(agent["result_ref"])
            args.pop("tool")
            pages = []
            while True:
                page = restored.wait(**args, consume=False)["agents"][0]
                pages.append(page["summary"])
                if page["next_output_offset"] is None:
                    break
                args["output_offset"] = page["next_output_offset"]
            assert "".join(pages) == outputs[i]
        restored.finish()
    finally:
        db.close()


def test_wait_bounds_eight_escaped_results_and_keeps_pages_contiguous(tmp_path):
    from app.agent.executor import TOOL_OUTPUT_MAX_CHARS
    m = manager(tmp_path)
    ids = []
    text = '\\"\n\t\x01' * 5000
    try:
        for i in range(8):
            sid = m.spawn(f"总结 {i}", max_steps=1, max_tool_calls=1)["child_run_id"]
            m.wait([sid], 2, consume=False)
            m.children[sid].result.output = text
            ids.append(sid)
        result = m.wait(ids + ids, 0)
        assert len(result["agents"]) == 8
        assert len(json.dumps(result, ensure_ascii=False)) <= TOOL_OUTPUT_MAX_CHARS
        # Large requested pages must remain contiguous even if JSON escaping fills the budget.
        args = {"ids": [ids[0]], "timeout": 0, "output_limit": 4000, "output_offset": 0}
        pages = []
        while True:
            result = m.wait(**args, consume=False)
            assert len(json.dumps(result, ensure_ascii=False)) <= TOOL_OUTPUT_MAX_CHARS
            page = result["agents"][0]
            pages.append(page["summary"])
            offset = page["next_output_offset"]
            if offset is None:
                break
            assert offset > args["output_offset"]
            args["output_offset"] = offset
        assert "".join(pages) == text
    finally:
        m.finish()
