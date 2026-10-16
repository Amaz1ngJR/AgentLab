"""Real isolated edit -> verification -> explicit patch approval -> integration."""
import json
import threading

import pytest

from app.agent.agent_manager import AgentManager
from app.agent.approval import AutoApprove, DenyAll
from app.agent.cancel import CancelToken
from app.models.protocol import ModelResponse, ToolCall
from app.tools.builtin.files import default_tools
from app.tools.builtin.shell import SHELL
from app.tools.registry import ToolRegistry, ToolExecutionError
from tests.unit.test_subagents import git_repo
from tests.unit.test_execution_plan import FakeRouter, response


class WriterRouter(FakeRouter):
    def fork(self):
        return FakeRouter([
            ModelResponse("", [ToolCall("w", "write_file", {"path": "child.txt", "content": "hello"})], {}, []),
            response("created"),
        ])


def make_manager(git_repo):
    tools = ToolRegistry()
    for tool in [*default_tools(), SHELL]:
        tools.register(tool)
    return AgentManager(llm=WriterRouter([]), tools=tools, approval=AutoApprove(),
                        workspace=git_repo, cancel=CancelToken(), allowed=True)


def test_writer_validates_and_integrates_once_without_parent_commit(git_repo):
    m = make_manager(git_repo)
    sid = m.spawn("创建文件", role="executor", file_scope=["child.txt"])["child_run_id"]
    assert m.wait([sid], 3)["agents"][0]["status"] == "succeeded"
    assert not (git_repo / "child.txt").exists()
    result = m.integrate(sid, ["test -f child.txt"])
    assert result["status"] == "integrated"
    assert (git_repo / "child.txt").read_text() == "hello"
    assert m.integrate(sid, ["test -f child.txt"])["already_integrated"]
    m.finish()


def test_integration_denial_or_failed_verification_never_applies_patch(git_repo):
    m = make_manager(git_repo)
    sid = m.spawn("创建文件", role="executor", file_scope=["child.txt"])["child_run_id"]
    m.wait([sid], 3)
    assert m.integrate(sid, ["exit 1"])["status"] == "verification_failed"
    assert not (git_repo / "child.txt").exists()
    m.approval = DenyAll()
    with pytest.raises(ToolExecutionError, match="denied"):
        m.integrate(sid, ["test -f child.txt"])
    assert not (git_repo / "child.txt").exists()
    m.finish()


def test_writer_rejects_dirty_baseline_and_overlapping_scopes(git_repo):
    m = make_manager(git_repo)
    (git_repo / "dirty.txt").write_text("user changes")
    with pytest.raises(ValueError, match="未提交"):
        m.spawn("write", role="executor", file_scope=["child.txt"])
    (git_repo / "dirty.txt").unlink()
    sid = m.spawn("创建文件", role="executor", file_scope=["child.txt"])["child_run_id"]
    m.wait([sid], 3)
    with pytest.raises(ToolExecutionError, match="conflicts"):
        m.spawn("again", role="executor", file_scope=["child.txt"])
    m.finish()


def test_validation_cannot_change_the_artifact_it_claimed_to_validate(git_repo):
    m = make_manager(git_repo)
    sid = m.spawn("创建文件", role="executor", file_scope=["child.txt"])["child_run_id"]
    m.wait([sid], 3)
    with pytest.raises(ToolExecutionError, match="verification changed"):
        m.integrate(sid, ["printf changed > child.txt"])
    assert not (git_repo / "child.txt").exists()
    assert m.unresolved()
    m.finish()


def test_delivery_record_contains_artifact_and_revision(git_repo):
    m = make_manager(git_repo)
    sid = m.spawn("创建文件", role="executor", file_scope=["child.txt"])["child_run_id"]
    m.wait([sid], 3)
    assert m.unresolved()
    assert m.snapshot(sid)["changed_files"] == ["child.txt"]
    m.integrate(sid, ["test -f child.txt"])
    record = m.records()[0]
    assert record["revision"] == 2
    assert record["verification"]["patch_sha256"]
    assert record["workspace"] != record["parent_workspace"]
    assert not m.unresolved()
    m.finish()


def test_git_metadata_is_not_a_permitted_write_scope(git_repo):
    m = make_manager(git_repo)
    with pytest.raises(ToolExecutionError, match="metadata"):
        m.spawn("overwrite", role="executor", file_scope=[".git"])
    m.finish()


def test_resume_can_integrate_saved_artifact_without_reexecuting_writer(git_repo):
    m = make_manager(git_repo)
    sid = m.spawn("创建文件", role="executor", file_scope=["child.txt"])["child_run_id"]
    m.wait([sid], 3)
    saved = m.records()
    m.finish()
    restored = make_manager(git_repo)
    restored.restore_results(saved)
    assert restored.usage == {"input_tokens": 0, "output_tokens": 0}
    assert restored.wait([sid], 0)["agents"][0]["status"] == "succeeded"
    assert restored.integrate(sid, ["test -f child.txt"])["status"] == "integrated"
    assert (git_repo / "child.txt").read_text() == "hello"
    with pytest.raises(ToolExecutionError, match="immutable"):
        restored.send_input(sid, "retry", "interrupt")
    restored.finish()


def test_interrupted_recovery_does_not_claim_worker_is_alive(git_repo):
    m = make_manager(git_repo)
    sid = m.spawn("创建文件", role="executor", file_scope=["child.txt"])["child_run_id"]
    m.wait([sid], 3)
    saved = m.records()
    saved[0]["status"] = saved[0]["execution_status"] = "cancelling"
    m.finish()
    restored = make_manager(git_repo)
    restored.restore_results(saved)
    assert restored.snapshot(sid)["execution_status"] == "interrupted"
    with pytest.raises(ToolExecutionError, match="successful"):
        restored.integrate(sid, ["test -f child.txt"])
    assert not (git_repo / "child.txt").exists()
    restored.finish()


@pytest.mark.parametrize("restart", [False, True])
def test_two_disjoint_writers_can_integrate_sequentially_without_baseline_false_conflict(git_repo, restart):
    class Writers(WriterRouter):
        def __init__(self):
            super().__init__([])
            self.count = 0
        def fork(self):
            self.count += 1
            name = f"file{self.count}.txt"
            return FakeRouter([
                ModelResponse("", [ToolCall("w", "write_file", {"path": name, "content": name})], {}, []),
                response("created"),
            ])
    m = make_manager(git_repo)
    m.llm = Writers()
    a = m.spawn("create a", role="executor", file_scope=["file1.txt"])["child_run_id"]
    b = m.spawn("create b", role="executor", file_scope=["file2.txt"])["child_run_id"]
    for sid in [a, b]:
        m.wait([sid], 3)
    assert m.integrate(a, ["test -f file1.txt"])["status"] == "integrated"
    if restart:
        from app.storage import Storage
        db_path = git_repo.parent / (git_repo.name + "-delivery.db")
        db = Storage(db_path)
        db.create_session("s", "default", "fake")
        db.save_child_runs("s", m.records())
        m.finish()
        db.close()
        db = Storage(db_path)
        m = make_manager(git_repo)
        m.restore_results(db.load_child_runs("s"))
        db.close()
        assert m.snapshot(a)["delivery_status"] == "integrated"
    assert m.integrate(b, ["test -f file2.txt"])["status"] == "integrated"
    assert (git_repo / "file1.txt").exists() and (git_repo / "file2.txt").exists()
    m.finish()


def test_task_write_delivery_runs_parent_verification_before_releasing_dependents(git_repo):
    from app.agent.runtime import AgentSession
    from app.config.loader import use_workspace_root
    class Parent(WriterRouter):
        def create_message(self, messages, **kwargs):
            names = [tool["name"] for tool in kwargs.get("tools") or []]
            if not names:
                return response(json.dumps({"version": 1, "tasks": [
                    {"id": "write", "content": "create", "executor": "subagent", "subagent": {
                        "id": "writer", "instruction": "create", "role": "executor", "read_only": False,
                        "file_scope": ["child.txt"], "max_steps": 3, "max_tool_calls": 3}},
                    {"id": "finish", "content": "verify integrated", "dependencies": ["write"]},
                ]}))
            text = json.dumps(messages, ensure_ascii=False)
            if "patch_sha256" in text:
                return response("integrated and verified")
            import re
            sid = re.search(r'[a-f0-9]{32}-[a-f0-9]{12}', text)[0]
            return ModelResponse("", [ToolCall("integrate", "integrate_agent", {
                "sid": sid, "checks": ["test -f child.txt"]})], {}, [])
    m = make_manager(git_repo)
    session = AgentSession(Parent([]), m.tools, approval=AutoApprove(), orchestrate=True, max_steps=16)
    with use_workspace_root(git_repo):
        assert session.chat("多个 agent 实现并验证") == "integrated and verified"
    assert session.last_run_status == "completed"
    assert (git_repo / "child.txt").read_text() == "hello"
    assert all(t["status"] == "completed" for t in session.task_store.snapshot())
    m.finish()


def test_parent_cannot_write_owned_scope_or_run_arbitrary_shell(git_repo):
    from app.agent.agent_manager import DelegationRegistry
    m = make_manager(git_repo)
    sid = m.spawn("create", role="executor", file_scope=["child.txt"])["child_run_id"]
    m.wait([sid], 3)
    tools = DelegationRegistry(m.tools, m)
    assert tools.execute("write_file", {"path": "child.txt", "content": "oops"})[1]
    assert tools.execute("shell", {"command": "true"})[1]
    assert not (git_repo / "child.txt").exists()
    m.finish()


@pytest.mark.parametrize("policy,goal,completed", [
    ("explicit", "继续", True),
    ("off", "继续", False),
    ("auto", "不要使用子 agent，继续", False),
])
def test_task_resume_saved_writer_uses_authorization_and_never_replays(git_repo, policy, goal, completed):
    from dataclasses import asdict
    from app.agent.runtime import AgentSession
    from app.agent.tasks import Task, BLOCKED
    from app.config.loader import use_workspace_root
    from app.storage import Storage
    m = make_manager(git_repo)
    sid = m.spawn("创建文件", role="executor", file_scope=["child.txt"], max_steps=3)["child_run_id"]
    m.wait([sid], 3)
    db = Storage(git_repo.parent / (git_repo.name + "-resume.db"))
    db.create_session("s", "default", "fake")
    db.save_child_runs("s", m.records())
    task = Task("write", "创建文件", BLOCKED, execution={"executor": "subagent",
        "subagent": asdict(m.children[sid].spec), "child_run_id": sid})
    from app.agent.tasks import TaskStore
    tasks = TaskStore()
    tasks.add(task)
    db.save_tasks("s", tasks.snapshot())
    m.finish()
    class Parent(WriterRouter):
        def fork(self):
            raise AssertionError("recovery must not replay writer")
        def create_message(self, messages, **kwargs):
            if any("patch_sha256" in json.dumps(message) for message in messages):
                return response("已集成")
            return ModelResponse("", [ToolCall("merge", "integrate_agent", {
                "sid": sid, "checks": ["test -f child.txt"]})], {}, [])
    session = AgentSession(Parent([]), m.tools, approval=AutoApprove(), orchestrate=True,
                           max_steps=16, delegation_policy=policy)
    session.child_records = db.load_child_runs("s")
    session.task_store.restore(db.load_tasks("s"))
    with use_workspace_root(git_repo):
        session.chat(goal, resume=True)
    assert (session.last_run_status == "completed") is completed
    assert (git_repo / "child.txt").exists() is completed
    assert (session.task_store.get("write").status == "completed") is completed
    db.close()


def test_integrated_retry_is_free_even_after_parent_budget_exhaustion(git_repo):
    m = make_manager(git_repo)
    sid = m.spawn("创建文件", role="executor", file_scope=["child.txt"])["child_run_id"]
    m.wait([sid], 3)
    m.integrate(sid, ["test -f child.txt"])
    revision = m.snapshot(sid)["revision"]
    m.tools_left = 0
    assert m.integrate(sid, ["exit 1"])["already_integrated"]
    assert m.snapshot(sid)["revision"] == revision
    m.finish()


def test_unstopped_writer_blocks_next_session_turn_until_real_exit(git_repo, monkeypatch):
    from app.agent.runtime import AgentSession
    from app.agent.subagent_runtime import SubagentRuntimeFactory
    from app.agent.subagents import SubagentResult
    from app.config.loader import use_workspace_root
    entered, release = threading.Event(), threading.Event()
    def stuck(self, spec, workspace, cancel):
        entered.set()
        release.wait(4)
        return SubagentResult(spec.subagent_id, spec.role, "cancelled")
    monkeypatch.setattr(SubagentRuntimeFactory, "delegate", stuck)
    from app.agent.cancel import CancelToken
    cancel = CancelToken()
    class Parent(WriterRouter):
        def create_message(self, messages, **kwargs):
            if entered.is_set():
                cancel.cancel()
                return response("stopping")
            return ModelResponse("", [ToolCall("spawn", "spawn_agent", {
                "instruction": "create", "role": "executor", "file_scope": ["child.txt"],
                "max_steps": 1})], {}, [])
    m = make_manager(git_repo)
    session = AgentSession(Parent([]), m.tools, approval=AutoApprove(), orchestrate=False, max_steps=4)
    try:
        with use_workspace_root(git_repo):
            answer = session.chat("多个 agent 创建文件", cancel=cancel)
            assert entered.is_set(), (answer, session.messages)
            assert session._retiring_managers
            assert "仍在停止中" in session.chat("下一步修改文件")
            assert session.last_run_status == "blocked"
            record = session.child_records[-1]
            assert record["execution_status"] == "cancelling"
    finally:
        release.set()
        for manager in session._retiring_managers:
            for child in manager.children.values():
                child.done.wait(2)
            manager.pump()
        m.finish()
    assert session.child_records[-1]["execution_status"] == "cancelled"


def test_child_deadline_includes_queued_approval_wait(git_repo):
    m = make_manager(git_repo)
    sid = m.spawn("create", role="executor", file_scope=["child.txt"], timeout_seconds=0.1)["child_run_id"]
    # Deliberately don't pump the owner approval queue.
    assert m.children[sid].done.wait(2)
    assert m.snapshot(sid)["execution_status"] == "timed_out"
    m.pump()
    assert not (m.children[sid].worktree.path / "child.txt").exists()
    assert not (git_repo / "child.txt").exists()
    m.finish()


@pytest.mark.parametrize("expire_at", ["verification", "approval"])
def test_integration_deadline_prevents_late_patch_application(git_repo, expire_at):
    import time
    from app.agent.cancel import Cancelled
    from app.agent.approval import ApprovalResult
    from app.tools.registry import Tool, current_cancel_token
    m = make_manager(git_repo)
    sid = m.spawn("create", role="executor", file_scope=["child.txt"])["child_run_id"]
    m.wait([sid], 3)
    if expire_at == "verification":
        def verify(args):
            token = current_cancel_token()
            assert token.wait(2)
            token.raise_if_cancelled()
        m.tools.register(Tool("shell", "verify", {}, verify))
    else:
        class SlowApproval(AutoApprove):
            def request_tool(self, tool, action, args):
                if action == "integrate_agent":
                    time.sleep(0.2)
                return ApprovalResult(True)
        m.approval = SlowApproval()
    m.deadline = time.monotonic() + 0.1
    try:
        with pytest.raises(Cancelled):
            m.integrate(sid, ["test -f child.txt"])
        assert not (git_repo / "child.txt").exists()
        assert m.snapshot(sid)["delivery_status"] == "pending_review"
        assert m.children[sid].worktree.path.exists()
    finally:
        m.finish()


def test_accepted_late_writer_steering_continues_same_worktree_and_budget(git_repo, monkeypatch):
    from app.agent.executor import Executor
    gate, release = threading.Event(), threading.Event()
    original = Executor.run_task
    first = True
    def finish_race(self, *args, **kwargs):
        nonlocal first
        outcome = original(self, *args, **kwargs)
        if first:
            first = False
            gate.set()
            assert release.wait(2)
        return outcome
    monkeypatch.setattr(Executor, "run_task", finish_race)
    class Writer(WriterRouter):
        def fork(self):
            return FakeRouter([
                ModelResponse("", [ToolCall("w1", "write_file", {"path": "child.txt", "content": "first"})], {}, []),
                response("first done"),
                ModelResponse("", [ToolCall("w2", "write_file", {"path": "child.txt", "content": "steered"})], {}, []),
                response("late change done"),
            ])
    m = make_manager(git_repo)
    m.llm = Writer([])
    try:
        sid = m.spawn("create", role="executor", file_scope=["child.txt"], max_steps=4, max_tool_calls=2)["child_run_id"]
        # Service the first write approval, then stop at Executor's completion boundary.
        import time
        deadline = time.monotonic() + 2
        while not gate.is_set() and time.monotonic() < deadline:
            m.pump()
            time.sleep(0.01)
        assert gate.is_set()
        assert m.send_input(sid, "write child.txt as steered")["accepted"]
        release.set()
        result = m.wait([sid], 3)["agents"][0]
        assert result["status"] == "succeeded"
        assert m.children[sid].result.model_rounds == 4
        assert m.children[sid].result.tool_calls == 2
        assert (m.children[sid].worktree.path / "child.txt").read_text() == "steered"
        assert not (git_repo / "child.txt").exists()
        m.integrate(sid, ["test -f child.txt"])
        assert (git_repo / "child.txt").read_text() == "steered"
    finally:
        release.set()
        m.finish()


def test_process_death_during_writer_retains_parent_checkpoint(git_repo):
    import subprocess
    import sys
    from app.storage import Storage
    db_path = git_repo.parent / (git_repo.name + "-crash.db")
    script = r'''
import json, os, sys
from app.agent.approval import AutoApprove
from app.agent.profiles import AgentProfile
from app.agent.runtime import AgentSession
from app.agent.session_router import SessionRouter
from app.config.loader import use_workspace_root
from app.models.protocol import ModelResponse
from app.storage import Storage
from app.tools.builtin.files import default_tools
from app.tools.registry import ToolRegistry
from tests.unit.test_execution_plan import FakeRouter, response
class CrashChild(FakeRouter):
    def create_message(self, messages, **kwargs):
        os._exit(0)  # No finally/persist_current; kill parent and child threads together.
class Parent(FakeRouter):
    def fork(self):
        return CrashChild([])
plan = {"version": 1, "tasks": [
    {"id": "prep", "content": "总结已给材料"},
    {"id": "write", "content": "create", "dependencies": ["prep"], "executor": "subagent",
     "subagent": {"id": "writer", "instruction": "create", "role": "executor", "read_only": False,
                   "file_scope": ["child.txt"], "max_steps": 2, "max_tool_calls": 2}}]}
tools = ToolRegistry()
for tool in default_tools():
    tools.register(tool)
prepared = ModelResponse("PREPARED", [], {}, [{"role": "assistant", "content": "PREPARED"}])
def factory(profile, sid):
    return AgentSession(Parent([response(json.dumps(plan)), prepared]), tools,
                        approval=AutoApprove(), orchestrate=True, max_steps=16)
db = Storage(__import__("pathlib").Path(sys.argv[1]))
profile = AgentProfile("default", "default", "fake")
router = SessionRouter(storage=db, session_factory=factory, profiles={"default": profile}, default_profile_id="default")
with use_workspace_root(__import__("pathlib").Path(sys.argv[2])):
    router.new("default")
    router.current.chat("多个 agent 创建文件")
raise SystemExit(3)  # The child must have terminated the process before return.
'''
    completed = subprocess.run([sys.executable, "-c", script, str(db_path), str(git_repo)],
                               capture_output=True, text=True, timeout=15)
    assert completed.returncode == 0, completed.stderr
    db = Storage(db_path)
    try:
        sid = db.list_sessions()[0]["id"]
        records = db.load_child_runs(sid)
        tasks = {task["id"]: task for task in db.load_tasks(sid)}
        assert len(records) == 1 and records[0]["execution_status"] == "interrupted"
        assert tasks["write"]["execution"]["child_run_id"] == records[0]["child_run_id"]
        assert tasks["write"]["dependencies"] == ["prep"]
        assert tasks["prep"]["status"] == "completed"
        assert "PREPARED" in json.dumps(db.load_messages(sid))
        assert not (git_repo / "child.txt").exists()
    finally:
        db.close()
