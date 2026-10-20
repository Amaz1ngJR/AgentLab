import json
import threading
import time

import pytest

from app.agent.cancel import CancelToken
from app.agent.executor import Executor
from app.agent.runtime import AgentSession
from app.agent.tasks import Task
from app.config.loader import use_workspace_root
from app.models.protocol import ModelResponse, ToolCall
from app.tools.registry import Tool, ToolRegistry
from tests.support.delegation import FakeRouter, registry, response


class WorkingModel(FakeRouter):
    rounds = 0

    def create_message(self, messages, **kwargs):
        if not kwargs.get("tools"):
            return response(json.dumps({"tasks": [{"id": "work", "content": "总结材料"}]}))
        self.rounds += 1
        if self.rounds <= 12:
            return ModelResponse("", [ToolCall(str(self.rounds), "read_file", {
                "path": str(self.rounds),
            })], {}, [])
        return response("WORK_DONE")


@pytest.mark.parametrize("mode", ["direct", "task"])
@pytest.mark.parametrize("policy", ["off", "auto"])
def test_auto_progress_exceeds_normal_round_tool_and_task_limits(tmp_path, mode, policy):
    session = AgentSession(WorkingModel([]), registry(), orchestrate=True, mode=mode,
                           delegation_policy=policy, max_steps=1, max_task_steps=1,
                           max_tool_calls=1, timeout_seconds=0.001,
                           auto_max_steps=100, auto_max_tool_calls=100, auto_timeout_seconds=5)
    session.enable_auto_execute()
    with use_workspace_root(tmp_path):
        assert session.chat("总结材料") == "WORK_DONE"
    assert session.last_run_status == "completed"


@pytest.mark.parametrize("mode", ["direct", "task"])
def test_auto_explicit_hard_budget_still_stops_and_reports_suspension(tmp_path, mode):
    session = AgentSession(WorkingModel([]), registry(), orchestrate=True, mode=mode,
                           auto_max_steps=3, auto_max_tool_calls=100, auto_timeout_seconds=5)
    session.enable_auto_execute()
    with use_workspace_root(tmp_path):
        assert "budget_exhausted" in session.chat("总结材料")
    assert session.last_run_status == "budget_exhausted"


@pytest.mark.parametrize("human_cancel", [False, True])
def test_long_run_deadline_and_user_cancel_interrupt_waiting_model(tmp_path, human_cancel):
    class WaitingModel(FakeRouter):
        def create_message(self, messages, **kwargs):
            cancel = kwargs["cancel"]
            assert cancel.wait(2)
            cancel.raise_if_cancelled()

    token = CancelToken()
    session = AgentSession(WaitingModel([]), registry(), mode="direct",
                           auto_timeout_seconds=5 if human_cancel else 0.05)
    session.enable_auto_execute()
    timer = threading.Timer(0.03, token.cancel) if human_cancel else None
    try:
        if timer:
            timer.start()
        started = time.monotonic()
        with use_workspace_root(tmp_path):
            session.chat("总结材料", cancel=token)
        assert time.monotonic() - started < 1
        assert session.last_run_status == ("cancelled" if human_cancel else "budget_exhausted")
    finally:
        if timer:
            timer.cancel()


@pytest.mark.parametrize("mode", ["direct", "task"])
def test_enabling_auto_mid_run_expands_remaining_budget_without_reset(tmp_path, mode):
    session = None

    def on_event(event):
        # Covers both legacy TurnEvent and Task RunEvent paths.
        if event.kind in {"tool_result", "tool_completed"} and not session.auto_execute_enabled:
            session.enable_auto_execute()

    session = AgentSession(WorkingModel([]), registry(), orchestrate=True, mode=mode,
                           delegation_policy="auto", max_steps=2, max_task_steps=1,
                           max_tool_calls=2, on_event=on_event, on_run_event=on_event,
                           auto_max_steps=100, auto_max_tool_calls=100, auto_timeout_seconds=5)
    with use_workspace_root(tmp_path):
        assert session.chat("总结材料") == "WORK_DONE"
    assert session.last_run_status == "completed"


def test_auto_tool_failure_is_returned_to_model_for_correction():
    tools = ToolRegistry()
    tools.register(Tool("operation", "operation", {},
                        lambda args: (_ for _ in ()).throw(ValueError("bad input")) if args.get("bad") else "OK"))
    model = FakeRouter([
        ModelResponse("", [ToolCall("bad", "operation", {"bad": True})], {}, []),
        ModelResponse("", [ToolCall("fixed", "operation", {})], {}, []),
        response("corrected"),
    ])
    messages = []
    result = Executor(model, tools).run_task(Task("t", "总结"), messages,
                                            system="", max_steps=1, auto_execute=True)
    assert result.status == "completed" and result.model_rounds == 3
    assert "bad input" in json.dumps(messages)


@pytest.mark.parametrize("mode", ["direct", "task"])
def test_auto_shell_timeout_continues_batch_and_returns_error_to_model(tmp_path, monkeypatch, mode):
    import subprocess
    from app.tools.builtin import shell

    token = CancelToken()
    executed = []

    def run_process(argv, **kwargs):
        executed.append(argv[-1])
        assert kwargs["cancel"] is token
        if len(executed) == 1:
            raise subprocess.TimeoutExpired(argv, 180, output=b"download partial")
        return subprocess.CompletedProcess(argv, 0, "probe result", "")

    monkeypatch.setattr(shell, "run_process", run_process)
    tools = ToolRegistry()
    tools.register(shell.SHELL)
    calls = [ToolCall("slow", "shell", {"command": "download", "timeout": 180}),
             ToolCall("probe", "shell", {"command": "probe"})]

    class Model(FakeRouter):
        def create_message(self, messages, **kwargs):
            if not self.calls:
                self.calls.append({})
                return ModelResponse("", calls, {}, [])
            history = json.dumps(messages)
            assert "download partial" in history and "probe result" in history
            return response("recovered")

    model = Model([])
    with use_workspace_root(tmp_path):
        if mode == "direct":
            session = AgentSession(model, tools, mode="direct")
            session.enable_auto_execute()
            assert session.chat("总结执行结果", cancel=token) == "recovered"
            assert session.last_run_status == "completed"
        else:
            result = Executor(model, tools).run_task(
                Task("t", "总结执行结果"), [], system="", max_steps=3,
                cancel=token, auto_execute=True,
            )
            assert result.status == "completed" and result.text == "recovered"
    assert executed == ["download", "probe"]
    assert not token.cancelled


def test_auto_repeated_errors_do_not_spin_forever():
    tools = ToolRegistry()
    tools.register(Tool("broken", "broken", {}, lambda args: (_ for _ in ()).throw(ValueError("broken"))))
    model = FakeRouter([ModelResponse("", [ToolCall(str(i), "broken", {"attempt": i})], {}, [])
                        for i in range(10)])
    result = Executor(model, tools).run_task(Task("t", "总结"), [],
                                            system="", max_steps=1, auto_execute=True)
    assert result.status == "failed" and result.model_rounds == 3


def test_auto_child_inherits_long_run_budget_not_normal_one(tmp_path):
    class Parent(FakeRouter):
        def fork(self):
            return WorkingModel([])

        def create_message(self, messages, **kwargs):
            if not kwargs.get("tools"):
                return response(json.dumps({"version": 1, "tasks": [{
                    "id": "child", "content": "总结", "executor": "subagent",
                    "subagent": {"id": "c", "role": "research", "instruction": "总结"},
                }]}))
            assert "WORK_DONE" in json.dumps(messages)
            return response("parent synthesis")

    session = AgentSession(Parent([]), registry(), orchestrate=True, mode="task",
                           delegation_policy="auto", max_steps=2, max_task_steps=1,
                           max_tool_calls=1, auto_max_steps=100,
                           auto_max_tool_calls=100, auto_timeout_seconds=5)
    session.enable_auto_execute()
    with use_workspace_root(tmp_path):
        assert session.chat("总结材料") == "parent synthesis"
    record = session.child_records[0]
    assert record["status"] == "succeeded"
    assert record["spec"]["max_steps"] > 8
    assert record["consumed"]["model_rounds"] == 13


def test_parent_waits_for_reserved_child_budget_instead_of_cancelling_it(tmp_path):
    from tests.support.delegation import manager
    release = threading.Event()

    class Child(FakeRouter):
        def create_message(self, messages, **kwargs):
            assert release.wait(2)
            assert kwargs["retry_until_cancelled"] is True
            return response("child done")

    class Parent(FakeRouter):
        def fork(self):
            return Child([])

    m = manager(tmp_path, model_rounds=4, tool_calls=4)
    m.llm = Parent([])
    m.enable_long_run(4, 4, 5)
    sid = m.spawn("总结", max_steps=2)["child_run_id"]
    timer = threading.Timer(0.03, release.set)
    try:
        m.charge_parent_round()
        m.charge_parent_round()
        timer.start()
        m.charge_parent_round()
        assert m.rounds_left == 0
        assert m.children[sid].result.status == "succeeded"
        assert not m.cancel.cancelled
    finally:
        release.set()
        timer.cancel()
        m.finish()


def test_budget_cancel_is_published_as_suspended_not_user_cancelled():
    from app.agent.service import RuntimeService
    from tests.unit.test_runtime_service import _Router, _Session

    class LimitedSession(_Session):
        def chat(self, message, *, cancel, **kwargs):
            self.last_run_status = "budget_exhausted"
            cancel.cancel()
            return "budget exhausted"

    service = RuntimeService(_Router({"s1": LimitedSession()}))
    events = []
    service.subscribe(events.append)
    try:
        assert service.send_message("总结") == "budget exhausted"
        assert events[-1].kind == "run_suspended"
        assert events[-1].payload["resumable"]
    finally:
        service.close()


@pytest.mark.parametrize("field", ["auto_max_steps", "auto_max_tool_calls", "auto_timeout_seconds"])
def test_invalid_auto_budget_is_rejected(field):
    from app.agent.profiles import AgentProfile
    with pytest.raises(ValueError):
        AgentProfile("test", "test", "fake", **{field: 0})
    with pytest.raises(ValueError):
        AgentSession(FakeRouter([]), registry(), **{field: 0})


def test_disabling_auto_mid_run_restores_approval(tmp_path):
    from app.agent.approval import DenyAll
    calls = []
    tools = ToolRegistry()
    tools.register(Tool("danger", "danger", {}, lambda args: calls.append(args) or "OK",
                        risk="write", requires_approval=True))

    class Parent(FakeRouter):
        def create_message(self, messages, **kwargs):
            if not kwargs.get("tools"):
                return response(json.dumps({"tasks": [{"id": "work", "content": "执行工作"}]}))
            i = len(self.calls)
            self.calls.append({})
            return ModelResponse("", [ToolCall(str(i), "danger", {"index": i})], {}, [])

    def on_event(event):
        if event.kind == "tool_completed":
            session.disable_auto_execute()

    session = AgentSession(Parent([]), tools, DenyAll(), orchestrate=True, mode="task",
                           on_run_event=on_event)
    session.enable_auto_execute()
    with use_workspace_root(tmp_path):
        session.chat("执行工作")
    assert len(calls) == 1
    assert session.last_run_status == "blocked"


def test_changing_output_from_same_tool_is_progress():
    tools = ToolRegistry()
    counter = iter(range(10))
    tools.register(Tool("observe", "observe", {}, lambda args: str(next(counter))))
    model = FakeRouter([ModelResponse("", [ToolCall(str(i), "observe", {})], {}, [])
                        for i in range(6)] + [response("done")])
    result = Executor(model, tools).run_task(Task("t", "总结"), [], system="",
                                            max_steps=1, auto_execute=True)
    assert result.status == "completed" and result.model_rounds == 7


def test_long_child_compacts_history_and_charges_compression_rounds(tmp_path):
    from app.agent.subagent_runtime import SubagentRuntimeFactory
    from app.agent.subagents import SubagentSpec

    class LongChild(FakeRouter):
        model = "fake"
        context_size = 8192
        work_rounds = 0
        compactions = 0

        def create_message(self, messages, **kwargs):
            if "上下文压缩器" in kwargs.get("system", ""):
                self.compactions += 1
                return response(json.dumps({"user_goal": "总结材料", "handoff_note": "继续剩余工作"}))
            self.work_rounds += 1
            if self.work_rounds <= 10:
                call = ToolCall(str(self.work_rounds), "read_file", {"path": str(self.work_rounds)})
                return ModelResponse("", [call], {}, [{"role": "assistant", "content": [{
                    "type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments,
                }]}])
            return response("done")

    tools = ToolRegistry()
    tools.register(Tool("read_file", "read", {}, lambda args: "X" * 4000))
    model = LongChild([])
    factory = SubagentRuntimeFactory(llm=model, tools=tools, max_steps=40,
                                     lightweight=True, retry_until_cancelled=True)
    result = factory.delegate(SubagentSpec("child", "总结材料", role="research",
                              tools=("read_file",), read_only=True, max_steps=40),
                              tmp_path, CancelToken())
    assert result.status == "succeeded", result.error
    assert model.compactions > 0
    assert result.model_rounds == model.work_rounds + model.compactions


def test_enabling_auto_twice_does_not_refill_consumed_budget(tmp_path):
    from tests.support.delegation import manager
    m = manager(tmp_path, model_rounds=2, tool_calls=2)
    try:
        m.charge_parent_round()
        m.charge_parent_tool()
        m.enable_long_run(100, 100, 10)
        assert (m.rounds_left, m.tools_left) == (99, 99)
        m.enable_long_run(100, 100, 10)
        assert (m.rounds_left, m.tools_left) == (99, 99)
    finally:
        m.finish()


@pytest.mark.parametrize("new_limit", [1, 200])
def test_restored_budget_preserves_consumption_when_config_changes(tmp_path, new_limit):
    from tests.support.delegation import manager
    m = manager(tmp_path, model_rounds=100, tool_calls=100)
    sid = m.spawn("总结", max_steps=2, max_tool_calls=1)["child_run_id"]
    m.wait([sid], 2, consume=False)
    m.charge_parent_round()
    m.charge_parent_tool()
    records = m.records()
    m.finish()
    restored = manager(tmp_path, model_rounds=new_limit, tool_calls=new_limit)
    try:
        restored.restore_results(records)
        restored.enable_long_run(new_limit, new_limit, 300)
        assert restored.rounds_left == max(0, new_limit - 2)
        assert restored.tools_left == max(0, new_limit - 1)
    finally:
        restored.finish()
