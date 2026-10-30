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
