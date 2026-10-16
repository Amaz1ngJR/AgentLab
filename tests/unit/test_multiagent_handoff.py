"""多 Agent P0：真实父子模型链路、恢复与权限的确定性回归。"""
import copy
import json
import time
from dataclasses import asdict
from pathlib import Path

import pytest

from app.agent.approval import AutoApprove, InteractivePolicy
from app.agent.cancel import CancelToken
from app.agent.execution_plan import parse_execution_plan, validate_execution_plan
from app.agent.executor import TaskOutcome
from app.agent.orchestrator import Orchestrator
from app.agent.replanner import Replanner
from app.agent.subagent_runtime import RestrictedToolRegistry, SubagentRuntimeFactory, _fresh_approval
from app.agent.subagents import SubagentSpec
from app.agent.tasks import BLOCKED, COMPLETED, FAILED, IN_PROGRESS, Task, TaskStore
from app.models.protocol import ModelResponse, ToolCall
from tests.unit.test_execution_plan import FakeRouter, registry, response


class SnapshotRouter(FakeRouter):
    def create_message(self, messages, **kwargs):
        return super().create_message(copy.deepcopy(messages), **kwargs)


def child_task(read_only=True):
    return {"id": "t1", "content": "读取文件并输出发现", "executor": "subagent", "subagent": {
        "id": "research-1", "role": "research" if read_only else "executor",
        "instruction": "读取文件并输出发现", "read_only": read_only,
        "tools": ["read_file"], "max_steps": 5,
    }}


def child_responses():
    return [response(json.dumps({"tasks": [{"id": "c1", "content": "读取文件"}]})),
            ModelResponse("", [ToolCall("read1", "read_file", {})],
                          {"input_tokens": 1, "output_tokens": 1}, []),
            response("UNIQUE_CHILD_EVIDENCE")]


def test_real_parent_child_handoff_lifecycle_and_usage():
    plan = {"version": 1, "tasks": [child_task(),
            {"id": "t2", "content": "汇总结论", "dependencies": ["t1"]}]}
    router = SnapshotRouter([response(json.dumps(plan)), *child_responses(), response("综合结论")])
    events = []
    orch = Orchestrator(router, registry(), AutoApprove(), max_steps=12, on_event=events.append)
    assert orch.run("调查并汇总") == "综合结论"
    assert "UNIQUE_CHILD_EVIDENCE" in json.dumps(router.calls[-1]["messages"])
    assert json.dumps(router.calls[-1]["messages"]).count("UNIQUE_CHILD_EVIDENCE") == 1
    assert orch.last_run_usage == {"input_tokens": 5, "output_tokens": 5}
    assert orch.last_run_tool_calls == 1
    assert [e.kind for e in events].count("subagent_started") == 1
    assert [e.kind for e in events].count("subagent_completed") == 1
    assert all(e.payload.get("subagent_id") for e in events if e.payload.get("child_event"))
    assert orch.store.get("t1").execution["result"]["execution_status"] == "succeeded"


def test_task_dependencies_do_not_become_child_ids():
    task = child_task(False)
    task["dependencies"] = ["before"]
    plan = parse_execution_plan({"version": 1, "tasks": [
        {"id": "before", "content": "准备"}, task]}, "goal")
    assert not plan.subagent_specs[0].dependencies
    assert validate_execution_plan(plan) == []
    task["subagent"]["dependencies"] = ["before"]
    plan = parse_execution_plan({"version": 1, "tasks": [
        {"id": "before", "content": "准备"}, task]}, "goal")
    assert any("任务 dependencies" in e for e in validate_execution_plan(plan))


def test_snapshot_resume_preserves_child_executor_and_context():
    raw = child_task()
    raw["subagent"]["retry_limit"] = 1
    plan = parse_execution_plan({"version": 1, "tasks": [raw]}, "goal")
    plan.tasks[0].task.status = FAILED
    store = TaskStore()
    store.extend([p.task for p in plan.tasks])
    restored = TaskStore()
    restored.restore(json.loads(json.dumps(store.snapshot())))
    router = SnapshotRouter(child_responses())
    orch = Orchestrator(router, registry(), task_store=restored, max_steps=10)
    assert orch.run("继续", resume=True) == "done"
    assert "UNIQUE_CHILD_EVIDENCE" in json.dumps(router.calls[-1]["messages"])
    assert orch.subagent_results["research-1"].status == "succeeded"
    assert restored.get("t1").execution["executor"] == "subagent"


def test_interrupted_writer_is_not_automatically_replayed():
    store = TaskStore()
    store.add(Task("t1", "写入", IN_PROGRESS,
                   execution={"executor": "subagent", "subagent": asdict(SubagentSpec("w", "write"))}))
    store.reset_for_resume()
    assert store.get("t1").status == BLOCKED


def test_child_failure_never_falls_back_to_parent_retry():
    store = TaskStore()
    task = Task("t1", "调查", execution={"executor": "subagent", "subagent": {"role": "research"}})
    store.add(task)
    patch = Replanner(store).apply(task, TaskOutcome(FAILED, error="failure"))
    assert not patch.added
    assert store.get("t1").execution["executor"] == "subagent"


def test_successful_retry_recovers_logical_task_and_dependencies():
    store = TaskStore()
    store.add(Task("a", "操作"))
    store.add(Task("b", "汇总", dependencies=["a"]))
    replanner = Replanner(store)
    retry_id = replanner.apply(store.get("a"), TaskOutcome(FAILED, error="boom")).added[0]
    replanner.apply(store.get(retry_id), TaskOutcome(COMPLETED, evidence="fixed"))
    assert store.get("a").status == COMPLETED
    assert store.get("a").recovered_by == retry_id
    assert store.claim_next().id == "b"


@pytest.mark.parametrize("role", ["research", "reviewer", "verifier", "executor"])
def test_read_only_child_cannot_run_shell(role, tmp_path):
    router = SnapshotRouter([response(json.dumps({"tasks": [{"id": "c", "content": "总结"}]})),
                             response("done")])
    result = SubagentRuntimeFactory(llm=router, tools=registry()).delegate(
        SubagentSpec("r", "总结", role=role, read_only=True), tmp_path, CancelToken())
    assert result.status == "succeeded"
    assert all(t["name"] != "shell" for call in router.calls for t in (call.get("tools") or []))


def test_child_does_not_drain_parent_steering(tmp_path):
    parent = CancelToken()
    parent.steer("ONLY_FOR_PARENT")
    router = SnapshotRouter([response(json.dumps({"tasks": [{"id": "c", "content": "总结"}]})),
                             response("done")])
    SubagentRuntimeFactory(llm=router, tools=registry()).delegate(
        SubagentSpec("r", "总结", role="research", read_only=True), tmp_path, parent)
    assert parent.drain_steering() == ["ONLY_FOR_PARENT"]
    assert "ONLY_FOR_PARENT" not in json.dumps([call["messages"] for call in router.calls])


def test_auto_execute_does_not_remove_child_total_budget(tmp_path):
    router = SnapshotRouter([response(json.dumps({"tasks": [{"id": "c", "content": "读取文件"}]})),
        ModelResponse("", [ToolCall("r", "read_file", {})], {"input_tokens": 1, "output_tokens": 1}, [])])
    result = SubagentRuntimeFactory(llm=router, tools=registry(), auto_execute=True).delegate(
        SubagentSpec("r", "读取", role="research", read_only=True, max_steps=2), tmp_path, CancelToken())
    assert result.status == "budget_exhausted"
    assert result.model_rounds == 2
    assert result.usage == {"input_tokens": 2, "output_tokens": 2}


def test_deadline_cancels_slow_model_without_draining_parent(tmp_path):
    class Slow(SnapshotRouter):
        def create_message(self, messages, **kwargs):
            time.sleep(0.2)
            return response("late")
    parent = CancelToken()
    result = SubagentRuntimeFactory(llm=Slow([]), tools=registry()).delegate(
        SubagentSpec("r", "读取", role="research", read_only=True, timeout_seconds=0.03),
        tmp_path, parent)
    assert result.status == "timed_out"
    assert not parent.cancelled


def test_tool_budget_and_scope_are_execution_checks(tmp_path):
    tools = RestrictedToolRegistry(registry(), {"read_file", "edit_file"}, max_calls=1,
                                   workspace=tmp_path, file_scope=("allowed",))
    assert tools.execute("edit_file", {"path": "outside"})[1]
    assert tools.execute("read_file", {"path": "../outside"})[1]
    assert not tools.execute("read_file", {"path": "inside"})[1]
    from app.agent.cancel import Cancelled
    with pytest.raises(Cancelled, match="budget_exhausted"):
        tools.execute("read_file", {})


def test_child_approval_does_not_inherit_parent_temporary_grants():
    parent = InteractivePolicy()
    parent._always.add("write_file")
    child = _fresh_approval(parent)
    assert child is not parent
    assert not child._always


def test_invalid_plan_fallback_is_observable():
    router = SnapshotRouter([response("invalid JSON"), response("done")])
    events = []
    Orchestrator(router, registry(), on_event=events.append).run("总结")
    assert any(e.kind == "plan_fallback" and e.payload["reasons"] for e in events)


def test_child_cannot_recursively_execute_planner_delegation():
    router = SnapshotRouter([response(json.dumps({"version": 1, "tasks": [child_task()]}))])
    orch = Orchestrator(router, registry(), allow_subagents=False)
    orch.run("nested")
    assert orch.last_run_status == "blocked"
    assert not orch.subagent_results


def test_cli_and_protocol_distinguish_child_lifecycle(capsys):
    from app.cli import _print_run_event
    from app.agent.events import RunEvent
    from app.protocol.mapping import runtime_event_to_item
    _print_run_event(RunEvent("subagent_started", payload={"subagent_id": "a", "role": "research"}))
    _print_run_event(RunEvent("subagent_completed", payload={
        "subagent_id": "a", "execution_status": "succeeded", "delivery_status": "none"}))
    text = capsys.readouterr().out
    assert "a" in text and "research" in text and "succeeded" in text
    event = RunEvent("run_completed", payload={"child_event": True, "subagent_id": "a"})
    item = runtime_event_to_item(event, thread_id="t", turn_id="r", sequence=1)
    assert item.kind == "subagent.result"
    _print_run_event(event)
    assert capsys.readouterr().out == ""


def test_restored_unknown_executor_fails_closed():
    store = TaskStore()
    store.add(Task("a", "do not run", FAILED, execution={"executor": "unknown"}))
    router = SnapshotRouter([])
    orch = Orchestrator(router, registry(), task_store=store)
    orch.run("继续", resume=True)
    assert orch.last_run_status == "blocked"
    assert router.calls == []


def test_child_tool_limit_keeps_partial_usage(tmp_path):
    router = SnapshotRouter([
        response(json.dumps({"tasks": [{"id": "c", "content": "读取文件"}]})),
        ModelResponse("", [ToolCall("r1", "read_file", {}), ToolCall("r2", "read_file", {"path": "b"})],
                      {"input_tokens": 1, "output_tokens": 1}, []),
    ])
    result = SubagentRuntimeFactory(llm=router, tools=registry()).delegate(
        SubagentSpec("a", "读取", role="research", read_only=True, max_tool_calls=1),
        tmp_path, CancelToken())
    assert result.status == "budget_exhausted"
    assert result.tool_calls == 1
    assert result.usage == {"input_tokens": 2, "output_tokens": 2}


def test_child_path_checks_expand_home_before_scope_validation(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path.parent / "outside-home"))
    tools = RestrictedToolRegistry(registry(), {"read_file", "edit_file"}, workspace=tmp_path)
    assert tools.execute("read_file", {"path": "~/secret"})[1]
    assert tools.execute("edit_file", {"path": "~/secret"})[1]
    assert tools.calls == 0


def test_dependency_data_is_not_promoted_to_child_system(tmp_path):
    router = SnapshotRouter([
        response(json.dumps({"tasks": [{"id": "c", "content": "总结"}]})), response("done"),
    ])
    SubagentRuntimeFactory(llm=router, tools=registry(), parent_context="UNTRUSTED_EVIDENCE").delegate(
        SubagentSpec("a", "总结", role="research", read_only=True), tmp_path, CancelToken())
    assert "UNTRUSTED_EVIDENCE" in json.dumps(router.calls[-1]["messages"])
    assert all("UNTRUSTED_EVIDENCE" not in (call.get("system") or "") for call in router.calls)


def test_child_retry_limit_zero_does_not_create_internal_retry(tmp_path):
    router = SnapshotRouter([
        response(json.dumps({"tasks": [{"id": "c", "content": "读取文件"}]})),
        ModelResponse("", [ToolCall("r1", "edit_file", {})], {"input_tokens": 1, "output_tokens": 1}, []),
    ])
    result = SubagentRuntimeFactory(llm=router, tools=registry()).delegate(
        SubagentSpec("a", "读取", role="research", read_only=True, retry_limit=0), tmp_path, CancelToken())
    assert result.status == "failed"
    assert len(router.calls) == 2
