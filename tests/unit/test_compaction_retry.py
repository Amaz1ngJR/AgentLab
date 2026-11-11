"""A timed-out soft-limit compaction must not run again after every tool result."""
from types import SimpleNamespace
import pytest
from app.agent.context import ContextManager, ContextBudgetExceeded
from app.agent.context_budget import ContextBudget
from app.agent.context_compaction import CompactionResult
from app.agent.cancel import CancelToken, Cancelled
from app.agent import events


def setup(monkeypatch):
    from app.agent import context
    clock, estimate, calls, emitted = [100.0], [7000], [], []
    monkeypatch.setattr(context.time, "monotonic", lambda: clock[0])
    def compact(messages, **kwargs):
        calls.append(kwargs)
        return CompactionResult(False, reason="压缩达到整体耗时上限，保留原始历史", code="timeout")
    compressor = SimpleNamespace(summary_system_tokens=100, timeout_for=lambda *a: 120, compact=compact)
    manager = ContextManager(ContextBudget.from_context_size(10000), compressor, on_event=emitted.append)
    monkeypatch.setattr(manager, "estimate", lambda *a, **kw: estimate[0])
    return manager, clock, estimate, calls, emitted


def test_timeout_then_todo_result_does_not_start_compaction_again(monkeypatch):
    manager, clock, estimate, calls, emitted = setup(monkeypatch)
    messages = [{"role": "user", "content": "goal"}]
    assert not manager.compact_before_model_call(messages)
    messages.append({"role": "tool", "content": "updated 3 tasks"})
    estimate[0] += 50
    for _ in range(5):
        assert not manager.compact_before_model_call(messages)
    assert len(calls) == 1
    assert sum(ev.kind == events.CONTEXT_COMPACTION_STARTED for ev in emitted) == 1
    assert messages[0]["content"] == "goal"


def test_retries_require_both_cooldown_and_material_input_change(monkeypatch):
    manager, clock, estimate, calls, emitted = setup(monkeypatch)
    manager.maybe_compact([])
    estimate[0] += 1100
    clock[0] += 299
    assert not manager.maybe_compact([])
    assert len(calls) == 1
    clock[0] += 2
    manager.maybe_compact([])
    assert len(calls) == 2


def test_cooldown_expiring_alone_does_not_retry_same_input(monkeypatch):
    manager, clock, estimate, calls, emitted = setup(monkeypatch)
    manager.maybe_compact([])
    clock[0] += 1000
    manager.maybe_compact([])
    assert len(calls) == 1
    assert manager.retry_status(estimate[0])["blocked"]
    manager.maybe_compact([], force=True)
    assert len(calls) == 2


def test_small_schema_fluctuation_does_not_reset_failure(monkeypatch):
    manager, clock, estimate, calls, emitted = setup(monkeypatch)
    manager.maybe_compact([])
    estimate[0] = 6800  # below compact threshold but still in warning zone
    manager.maybe_compact([])
    estimate[0] = 7000
    manager.maybe_compact([])
    assert len(calls) == 1


def test_hard_limit_is_enforced_without_another_slow_attempt(monkeypatch):
    manager, clock, estimate, calls, emitted = setup(monkeypatch)
    manager.compact_before_model_call([])
    estimate[0] = 7400
    with pytest.raises(ContextBudgetExceeded, match="已阻止重复压缩和超限请求"):
        manager.compact_before_model_call([])
    assert len(calls) == 1


def test_budget_change_and_success_clear_retry_state(monkeypatch):
    manager, clock, estimate, calls, emitted = setup(monkeypatch)
    manager.maybe_compact([])
    manager.budget = ContextBudget.from_context_size(9000)
    manager.maybe_compact([])
    assert len(calls) == 2
    summary = SimpleNamespace(summary={}, source_message_range=(0, 1), token_count_before=0, token_count_after=0)
    manager._compressor.compact = lambda *a, **kw: CompactionResult(True, summary=summary)
    assert manager.maybe_compact([], force=True)
    assert manager.report([])["compaction_retry"] == {}


def test_cancel_is_never_swallowed_by_retry_suppression(monkeypatch):
    manager, clock, estimate, calls, emitted = setup(monkeypatch)
    manager.maybe_compact([])
    cancel = CancelToken()
    cancel.cancel()
    with pytest.raises(Cancelled):
        manager.maybe_compact([], cancel=cancel)
