"""Compaction retains evidence and user intent; failed candidates are atomic."""
import copy
import json

import pytest

from app.agent.cancel import Cancelled, CancelToken
from app.agent.context import ContextBudgetExceeded, ContextManager
from app.agent.context_budget import ContextBudget, estimate_tokens
from app.agent.context_compaction import ContextCompressor, _safe_split_point, _split_transcript
from app.models.protocol import ModelResponse


class SummaryModel:
    def __init__(self):
        self.calls = []

    def create_message(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        return ModelResponse(
            text=json.dumps({"user_goal": "继续目标", "handoff_note": "继续验证"}),
            tool_calls=[], provider_payload=[], usage={"input_tokens": 7, "output_tokens": 3},
        )


def history():
    return [{"role": "user", "content": "禁止部署，先运行测试。"}] + [
        {"role": "assistant", "content": "执行证据" * 300} for _ in range(12)
    ]


def test_transcript_includes_all_provider_tool_fields_and_redacts():
    messages = [
        {"type": "function_call", "call_id": "c1", "name": "shell", "arguments": '{"cmd":"pytest"}'},
        {"type": "function_call_output", "call_id": "c1", "output": "FAILED evidence"},
        {"role": "assistant", "tool_calls": [{"id": "c2", "function": {"name": "read_file", "arguments": "path"}}]},
        {"role": "tool", "tool_call_id": "c2", "content": "sk-ant-api03-SECRETSECRETSECRET"},
    ]
    text = ContextCompressor._render_transcript(messages)
    for expected in ("pytest", "FAILED evidence", "shell", "c1", "read_file", "c2"):
        assert expected in text
    assert "SECRETSECRETSECRET" not in text


def test_latest_user_survives_long_tool_loop_without_preserving_entire_loop():
    messages = history()
    original_user = copy.deepcopy(messages[0])
    model = SummaryModel()
    result = ContextCompressor(model).compact(messages, keep_recent=2)
    assert result.compacted
    assert messages[1] == original_user
    assert len(messages) == 4


def test_system_context_and_latest_correction_survive():
    messages = [{"role": "system", "content": "项目规则"}, *history()]
    messages.insert(6, {"role": "user", "content": "改用 SQLite，仍禁止部署"})
    result = ContextCompressor(SummaryModel()).compact(messages, keep_recent=2)
    assert result.compacted
    assert messages[1:3] == [
        {"role": "system", "content": "项目规则"},
        {"role": "user", "content": "改用 SQLite，仍禁止部署"},
    ]


@pytest.mark.parametrize("style", ["responses", "chat", "anthropic"])
def test_parallel_results_never_cross_split(style):
    if style == "responses":
        calls = [{"type": "function_call", "call_id": key} for key in ("a", "b")]
        results = [{"type": "function_call_output", "call_id": key} for key in ("a", "b")]
    elif style == "chat":
        calls = [{"role": "assistant", "tool_calls": [{"id": key}]} for key in ("a", "b")]
        results = [{"role": "tool", "tool_call_id": key} for key in ("a", "b")]
    else:
        calls = [{"role": "assistant", "content": [{"type": "tool_use", "id": key}]} for key in ("a", "b")]
        results = [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": key}]} for key in ("a", "b")]
    messages = [{"role": "user", "content": "goal"}, *calls, *results, {"role": "assistant", "content": "end"}]
    assert _safe_split_point(messages, keep_recent=2) == 1


def test_pending_tool_call_is_protected():
    messages = [{"role": "user", "content": "goal"},
                {"type": "function_call", "call_id": "pending"},
                {"role": "assistant", "content": "waiting"}]
    assert _safe_split_point(messages, keep_recent=1) == 1


def test_chunking_covers_middle_and_every_character_within_budget():
    text = "头部" * 1000 + "关键纠正：禁止发布" + "尾部" * 1000
    chunks = _split_transcript(text, 256)
    assert "".join(chunks) == text
    assert all(estimate_tokens(chunk) <= 256 for chunk in chunks)


def test_map_reduce_summarizes_every_chunk_and_counts_usage():
    model = SummaryModel()
    compressor = ContextCompressor(model, summary_input_tokens=512)
    messages = history()
    result = compressor.compact(messages, keep_recent=2)
    assert result.compacted
    assert len(model.calls) > 2
    initial_chunks = _split_transcript(compressor._render_transcript(history()[:-2]), 512)
    assert [call[0][0]["content"] for call in model.calls[:len(initial_chunks)]] == initial_chunks
    assert compressor.last_usage["input_tokens"] == len(model.calls) * 7


def test_overflow_retries_with_smaller_chunks_without_dropping_history():
    class OverflowOnce(SummaryModel):
        def create_message(self, messages, **kwargs):
            if not self.calls:
                self.calls.append((messages, kwargs))
                raise ValueError("context_length_exceeded")
            return super().create_message(messages, **kwargs)
    model = OverflowOnce()
    compressor = ContextCompressor(model, summary_input_tokens=1024)
    messages = history()
    assert compressor.compact(messages, keep_recent=2).compacted
    assert estimate_tokens(model.calls[1][0][0]["content"]) <= 512


def test_invalid_summary_retries_once_then_keeps_raw_user_constraints():
    class Invalid(SummaryModel):
        def create_message(self, messages, **kwargs):
            response = super().create_message(messages, **kwargs)
            response.text = "invalid"
            return response
    messages = history()
    model = Invalid()
    result = ContextCompressor(model).compact(messages, keep_recent=2)
    assert result.compacted and result.reason.startswith("local_fallback:")
    assert len(model.calls) == 2
    assert messages[1] == history()[0]
    assert "信息降级" in messages[0]["content"]


def test_rejected_candidate_preserves_original_history():
    messages = history()
    original = copy.deepcopy(messages)
    result = ContextCompressor(SummaryModel()).compact(
        messages, keep_recent=2, target_message_tokens=20,
    )
    assert not result.compacted
    assert messages == original


def test_cancel_does_not_fallback_or_mutate_history():
    messages = history()
    original = copy.deepcopy(messages)
    cancel = CancelToken()
    cancel.cancel()
    with pytest.raises(Cancelled):
        ContextCompressor(SummaryModel()).compact(messages, keep_recent=2, cancel=cancel)
    assert messages == original


def test_preflight_rejects_unshrinkable_tail_even_when_auto_disabled():
    manager = ContextManager(ContextBudget.from_model(declared_context_size=8192),
                             ContextCompressor(SummaryModel()), auto_compact=False)
    messages = [{"role": "user", "content": "不可截断的请求" * 2000}]
    with pytest.raises(ContextBudgetExceeded):
        manager.compact_before_model_call(messages)


def test_preflight_counts_system_tools_and_does_not_commit_oversize_candidate():
    manager = ContextManager(ContextBudget.from_model(declared_context_size=8192),
                             ContextCompressor(SummaryModel()), keep_recent=2)
    messages = history()
    original = copy.deepcopy(messages)
    with pytest.raises(ContextBudgetExceeded):
        manager.compact_before_model_call(messages, tools=[{"description": "工具" * 4000}])
    assert messages == original
    assert manager.last_summary is None
    assert manager.drain_records() == []


def test_preflight_success_fits_complete_request_budget():
    manager = ContextManager(ContextBudget.from_model(declared_context_size=8192),
                             ContextCompressor(SummaryModel()), keep_recent=2)
    messages = history()
    tools = [{"name": "shell", "description": "run command"}]
    assert manager.compact_before_model_call(messages, system="规则", tools=tools)
    assert manager.estimate(messages, system="规则", tools=tools) <= manager.budget.safe_input_limit


@pytest.mark.parametrize("path", ["executor", "orchestrator"])
def test_agent_paths_do_not_swallow_budget_error_or_send_model_request(path):
    from app.agent.executor import Executor
    from app.agent.orchestrator import Orchestrator
    from app.agent.tasks import Task
    from app.tools.registry import ToolRegistry

    model = SummaryModel()
    manager = ContextManager(ContextBudget.from_model(declared_context_size=8192),
                             ContextCompressor(model), auto_compact=False)
    messages = [{"role": "user", "content": "用户约束" * 3000}]
    with pytest.raises(ContextBudgetExceeded):
        if path == "executor":
            Executor(model, ToolRegistry(), context_manager=manager).run_task(
                Task(id="task1", content="继续"), messages, system="", max_steps=2,
            )
        else:
            Orchestrator(model, ToolRegistry(), context_manager=manager,
                         messages=messages).run("继续")
    assert model.calls == []


def test_cancelled_manager_preserves_cancellation_when_history_over_budget():
    cancel = CancelToken()
    cancel.cancel()
    manager = ContextManager(ContextBudget.from_model(declared_context_size=8192),
                             ContextCompressor(SummaryModel()), keep_recent=2)
    with pytest.raises(Cancelled):
        manager.compact_before_model_call(history(), cancel=cancel)


def test_compaction_deadline_interrupts_blocked_model_and_ignores_late_response():
    import threading
    import time

    release = threading.Event()
    finished = threading.Event()
    callbacks = []

    class BlockedModel(SummaryModel):
        def create_message(self, messages, **kwargs):
            release.wait(2)
            kwargs["on_progress"]({"input_tokens": 999})
            finished.set()
            return super().create_message(messages, **kwargs)

    messages = history()
    original = copy.deepcopy(messages)
    parent = CancelToken()
    compressor = ContextCompressor(BlockedModel(), timeout_seconds=0.05)
    started = time.monotonic()
    try:
        result = compressor.compact(messages, keep_recent=2, cancel=parent,
                                    on_progress=callbacks.append)
        assert result.code == "timeout"
        assert time.monotonic() - started < 1
        assert messages == original
        assert not parent.cancelled
    finally:
        release.set()
    assert finished.wait(1)
    assert callbacks == []
    assert messages == original


def test_compaction_respects_shared_deadline_without_starting_request():
    import time
    model = SummaryModel()
    messages = history()
    original = copy.deepcopy(messages)
    result = ContextCompressor(model).compact(
        messages, keep_recent=2, deadline=time.monotonic() - 1)
    assert result.code == "timeout"
    assert not model.calls
    assert messages == original


def test_compaction_user_cancel_does_not_become_timeout_or_fallback():
    parent = CancelToken()

    class CancellingModel(SummaryModel):
        def create_message(self, messages, **kwargs):
            parent.cancel("user")
            return super().create_message(messages, **kwargs)

    messages = history()
    original = copy.deepcopy(messages)
    with pytest.raises(Cancelled):
        ContextCompressor(CancellingModel()).compact(messages, keep_recent=2, cancel=parent)
    assert messages == original


def test_compaction_reports_map_reduce_progress_and_format_retry():
    class RetryModel(SummaryModel):
        def create_message(self, messages, **kwargs):
            if not self.calls:
                self.calls.append((messages, kwargs))
                return ModelResponse(text="invalid JSON", tool_calls=[], provider_payload=[], usage={})
            return super().create_message(messages, **kwargs)

    updates = []
    result = ContextCompressor(RetryModel(), summary_input_tokens=512, timeout_seconds=120).compact(
        history(), keep_recent=2, on_compaction_progress=updates.append)
    assert result.compacted
    assert any(p["attempt"] == 2 for p in updates)
    assert any(p["round"] > 1 for p in updates)
    assert updates[0]["chunk"] == 1
    assert updates[0]["chunks"] > 1
    assert all(p["remaining_seconds"] <= 120 for p in updates)


def test_compaction_wait_progress_is_throttled(monkeypatch):
    from app.agent import context_compaction as module
    clock = [10.0]
    updates = []
    original_call = module.call_create_message

    def call(*args, **kwargs):
        for delta in (1, 1, 3, 1):
            clock[0] += delta
            kwargs["on_wait"]()
        return original_call(*args, **kwargs)

    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(module, "call_create_message", call)
    ContextCompressor(SummaryModel()).compact(
        history(), keep_recent=2, on_compaction_progress=updates.append)
    first = [p for p in updates if p["round"] == 1 and p["chunk"] == 1]
    assert [p["elapsed_seconds"] for p in first] == [0, 5]


def test_compaction_progress_renders_units_and_retry(capsys):
    from app import cli
    from app.agent import events
    from app.agent.events import RunEvent
    cli._print_run_event(RunEvent(kind=events.CONTEXT_COMPACTION_PROGRESS, payload={
        "round": 2, "chunk": 3, "chunks": 8, "attempt": 1,
        "elapsed_seconds": 65, "remaining_seconds": 55, "phase": "retry", "retry": 2,
    }))
    output = capsys.readouterr().out
    assert "第 2 轮" in output and "分片 3/8" in output
    assert "1m 5s" in output and "网络重试 2" in output


def test_context_manager_shares_one_deadline_across_budget_attempts(monkeypatch):
    from app.agent.context_compaction import CompactionResult
    from app.agent import events
    compressor = ContextCompressor(SummaryModel())
    manager = ContextManager(ContextBudget.from_model(declared_context_size=32000), compressor, keep_recent=2)
    deadlines = []
    emitted = []
    monkeypatch.setattr(manager, "_emit", emitted.append)

    def compact(messages, **kwargs):
        deadlines.append(kwargs["deadline"])
        kwargs["on_compaction_progress"]({"round": 1, "chunk": 1, "chunks": 2})
        return CompactionResult(False, code="budget_exceeded")

    monkeypatch.setattr(compressor, "compact", compact)
    monkeypatch.setattr(manager.budget, "status_for", lambda estimate: "compact")
    manager.maybe_compact(history())
    assert len(deadlines) == 2 and deadlines[0] == deadlines[1]
    assert sum(e.kind == events.CONTEXT_COMPACTION_PROGRESS for e in emitted) == 2


@pytest.mark.parametrize("input_budget,expected_chunks", [(65536, 2), (131072, 2), (8192, 16)])
def test_automatic_chunk_size_uses_safe_budget_without_losing_transcript(input_budget, expected_chunks):
    # A long transcript needs only two map requests on a sufficiently large window.
    model = SummaryModel()
    compressor = ContextCompressor(model)
    transcript = "x" * (26 * 4096 * 4)
    updates = []
    compressor._summarize(
        transcript, 1024, None, CancelToken(), input_budget,
        on_compaction_progress=updates.append,
    )
    assert updates[0]["chunks"] == expected_chunks
    calls = model.calls[:expected_chunks]
    assert "".join(call[0][0]["content"] for call in calls) == transcript
    assert all(estimate_tokens(call[0][0]["content"]) <= input_budget for call in model.calls)
    assert len(model.calls) == expected_chunks + 1


def test_explicit_summary_chunk_limit_still_caps_large_model_window():
    model = SummaryModel()
    compressor = ContextCompressor(model, summary_input_tokens=512)
    updates = []
    compressor.compact(history(), keep_recent=2, input_token_budget=65536,
                       on_compaction_progress=updates.append)
    assert updates[0]["chunks"] > 1
    assert all(estimate_tokens(call[0][0]["content"]) <= 512 for call in model.calls)


@pytest.mark.parametrize("tokens,budget,expected", [
    (60000, 65536, 120),
    (100000, 65536, 360),
    (200000, 65536, 600),
    (1000000, 65536, 600),
    (100000, 8192, 600),
])
def test_compaction_time_budget_includes_merge_and_has_hard_cap(monkeypatch, tokens, budget, expected):
    from app.agent import context_compaction as module
    monkeypatch.setattr(module, "estimate_tokens", lambda text: tokens)
    assert ContextCompressor(SummaryModel()).timeout_for(history(), budget) == expected


def test_explicit_compaction_timeout_overrides_workload_budget():
    assert ContextCompressor(SummaryModel(), timeout_seconds=17).timeout_for(history(), 512) == 17
