"""Controlled concurrency through both real tool loops, without model/network calls."""
import threading
import time

import pytest

from app.agent.cancel import CancelToken, Cancelled
from app.agent.executor import Executor
from app.agent.runtime import AgentSession
from app.agent.tasks import Task
from app.models.protocol import ModelResponse, ToolCall
from app.tools.registry import Tool, ToolRegistry, ToolExecutionError
from tests.support.runtime import FakeRouter, _resp_text


def invoke(path, registry, specs, *, cancel=None, events=None):
    calls = [ToolCall(id=f"c{i}", name=name, arguments=args) for i, (name, args) in enumerate(specs)]
    response = ModelResponse(text="", tool_calls=calls, usage={}, provider_payload=[
        {"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments} for c in calls
    ])
    model = FakeRouter([response, _resp_text("读取完成")])
    if path == "session":
        session = AgentSession(llm=model, tools=registry, orchestrate=False, on_event=events)
        outcome = session.chat("读取数据", cancel=cancel)
        return outcome, session.messages
    messages = []
    outcome = Executor(model, registry, on_event=events).run_task(
        Task(id="t", content="读取数据"), messages, system="", max_steps=3, cancel=cancel,
    )
    return outcome, messages


def register(registry, name, executor, *, parallel=True, risk="read", approval=False):
    registry.register(Tool(name, name, {"type": "object"}, executor,
                           risk=risk, parallel_safe=parallel, requires_approval=approval))


def results(messages):
    return [block for message in messages for block in message.get("content", [])
            if isinstance(block, dict) and block.get("type") == "tool_result"]


@pytest.mark.parametrize("path", ["session", "executor"])
def test_independent_reads_overlap_and_results_audits_stay_ordered_on_owner(path):
    registry = ToolRegistry()
    barrier = threading.Barrier(3)
    owner = threading.get_ident()
    audit_threads = []
    registry._audit_sink = lambda event: audit_threads.append(threading.get_ident())
    def read(args):
        barrier.wait(timeout=2)  # A serial implementation cannot pass this barrier.
        time.sleep((2 - args["n"]) * .01)
        return str(args["n"])
    register(registry, "read", read)
    _, messages = invoke(path, registry, [("read", {"n": n}) for n in range(3)])
    assert [r["content"] for r in results(messages)] == ["0", "1", "2"]
    assert audit_threads == [owner] * 3


@pytest.mark.parametrize("path", ["session", "executor"])
def test_bounded_groups_and_write_ordering_barrier(path):
    registry = ToolRegistry(max_parallel_tools=2)
    active = peak = finished = 0
    lock = threading.Lock()
    def read(args):
        nonlocal active, peak, finished
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(.02)
        with lock:
            active -= 1
            finished += 1
        return "read"
    def write(args):
        assert active == 0 and finished == 3
        return "write"
    register(registry, "read", read)
    register(registry, "write", write, risk="write")
    invoke(path, registry, [("read", {"n": n}) for n in range(3)] + [("write", {})] + [("read", {"n": 4})])
    assert peak == 2
    assert finished == 4


@pytest.mark.parametrize("path", ["session", "executor"])
@pytest.mark.parametrize("approval,parallel", [(False, False), (True, True)])
def test_unmarked_or_approval_reads_remain_serial(path, approval, parallel):
    registry = ToolRegistry()
    active = peak = 0
    def read(args):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        time.sleep(.005)
        active -= 1
        return "read"
    register(registry, "read", read, parallel=parallel, approval=approval)
    invoke(path, registry, [("read", {"n": n}) for n in range(3)])
    assert peak == 1


def test_executor_error_preserves_real_sibling_results_and_stops_before_write():
    registry = ToolRegistry()
    barrier = threading.Barrier(3)
    writes = []
    def read(args):
        barrier.wait(timeout=2)
        if args["n"] == 0:
            raise ToolExecutionError("failed read")
        return f"actual {args['n']}"
    register(registry, "read", read)
    register(registry, "write", lambda args: writes.append(args) or "write", risk="write")
    outcome, messages = invoke("executor", registry,
        [("read", {"n": n}) for n in range(3)] + [("write", {})])
    assert outcome.status == "failed"
    assert outcome.tool_calls_made == 3
    blocks = results(messages)
    assert [r["content"] for r in blocks[:3]] == ["failed read", "actual 1", "actual 2"]
    assert writes == []
    assert len(blocks) == 4  # Only the unexecuted write gets an interruption placeholder.


@pytest.mark.parametrize("path", ["session", "executor"])
def test_cancelled_batch_returns_promptly_and_no_late_history_or_callbacks(path):
    registry = ToolRegistry()
    started = threading.Barrier(3)
    release = threading.Event()
    cancel = CancelToken()
    events, outcome = [], []
    def read(args):
        started.wait(timeout=2)
        release.wait(timeout=3)
        return "late result"
    register(registry, "read", read)
    def run():
        try:
            outcome.append(invoke(path, registry, [("read", {"n": n}) for n in range(2)],
                                  cancel=cancel, events=events.append))
        except Cancelled:
            outcome.append("cancelled")
    thread = threading.Thread(target=run)
    thread.start()
    try:
        started.wait(timeout=2)
        cancel.cancel()
        thread.join(timeout=1)
        assert not thread.is_alive()
        event_count = len(events)
        release.set()
        time.sleep(.05)
        assert len(events) == event_count
        assert outcome
    finally:
        release.set()
        thread.join(timeout=3)


def test_delegation_overlay_reserves_budget_on_owner_thread_before_dispatch():
    from app.agent.agent_manager import DelegationRegistry
    registry = ToolRegistry()
    barrier = threading.Barrier(2)
    register(registry, "read", lambda args: barrier.wait(timeout=2) is not None and "ok")
    owner = threading.get_ident()
    class Manager:
        allowed = False
        def __init__(self): self.charges = []
        def pump(self): assert threading.get_ident() == owner
        def charge_parent_tool(self): self.charges.append(threading.get_ident())
        def acknowledge_tool_results(self, results): pass
    manager = Manager()
    overlay = DelegationRegistry(registry, manager)
    # Exercise shared scheduler directly: parent-loop preflight also calls manager methods.
    from app.agent.tool_batch import ToolCallBatch
    calls = [ToolCall(id=f"c{n}", name="read", arguments={"n": n}) for n in range(2)]
    batch = ToolCallBatch(overlay, calls)
    assert batch.execute(calls[0]) == ("ok", False)
    assert batch.execute(calls[1]) == ("ok", False)
    assert manager.charges == [owner, owner]


@pytest.mark.parametrize("path", ["session", "executor"])
def test_validation_error_is_a_barrier_and_never_executes_invalid_arguments(path):
    registry = ToolRegistry()
    calls = []
    register(registry, "read", lambda args: calls.append(args["n"]) or "ok")
    registry.register(Tool("invalid", "invalid", {"type": "object", "required": ["missing"]},
                           lambda args: pytest.fail("invalid arguments executed"), parallel_safe=True))
    invoke(path, registry, [("read", {"n": 0}), ("invalid", {}), ("read", {"n": 1})])
    assert calls == [0, 1]


def test_custom_registry_override_is_not_bypassed_by_parallel_dispatch():
    class ScopedRegistry(ToolRegistry):
        def execute(self, name, args, **kwargs):
            return "scope denied", True
    registry = ScopedRegistry()
    register(registry, "read", lambda args: pytest.fail("bypassed registry scope"))
    from app.agent.tool_batch import ToolCallBatch
    calls = [ToolCall(id=f"c{n}", name="read", arguments={"n": n}) for n in range(2)]
    batch = ToolCallBatch(registry, calls)
    assert batch.execute(calls[0]) == ("scope denied", True)
    assert batch.execute(calls[1]) == ("scope denied", True)


def test_batch_respects_remaining_parent_tool_budget_before_reserving_group():
    from app.agent.agent_manager import DelegationRegistry
    from app.agent.tool_batch import ToolCallBatch
    registry = ToolRegistry()
    executed = []
    register(registry, "read", lambda args: executed.append(args["n"]) or "ok")
    class Manager:
        allowed = False
        long_run = True
        tools_left = 1
        children = {}
        def pump(self): pass
        def charge_parent_tool(self):
            if self.tools_left <= 0:
                raise Cancelled("budget exhausted")
            self.tools_left -= 1
    overlay = DelegationRegistry(registry, Manager())
    calls = [ToolCall(id=f"c{n}", name="read", arguments={"n": n}) for n in range(3)]
    batch = ToolCallBatch(overlay, calls)
    assert batch.execute(calls[0]) == ("ok", False)
    with pytest.raises(Cancelled):
        batch.execute(calls[1])
    assert executed == [0]
