"""Large windows should not turn the recent-history budget into half a million tokens."""
import pytest
from app.agent.context_budget import ContextBudget, MAX_RECENT_MESSAGES_TOKENS
from app.agent.context import ContextManager
from app.agent.context_compaction import ContextCompressor
from app.agent import events
from tests.unit.test_context_safety import SummaryModel


@pytest.mark.parametrize('window', [8192, 64000, 256000, 1000000])
def test_recent_history_budget_is_bounded_and_scales_down_for_small_models(window):
    budget = ContextBudget.from_context_size(window)
    assert budget.recent_messages_budget == min(MAX_RECENT_MESSAGES_TOKENS, int(budget.input_budget * .5))


def test_million_token_window_compacts_old_history_and_reports_exact_breakdown():
    messages = [{'role': 'user', 'content': '保留目标：只调查，不部署'}] + [
        {'role': 'assistant', 'content': '证据' * 1000} for _ in range(480)]
    budget = ContextBudget.from_context_size(1000000)
    emitted = []
    manager = ContextManager(budget, ContextCompressor(SummaryModel()), on_event=emitted.append)
    before = manager.estimate(messages, system='项目规则', tools=[{'name': 'read_file'}])
    assert before > budget.compact_threshold
    assert manager.compact_before_model_call(messages, system='项目规则', tools=[{'name': 'read_file'}])
    event = next(ev for ev in emitted if ev.kind == events.CONTEXT_COMPACTION_COMPLETED)
    p = event.payload
    assert p['token_after'] < 60000
    assert p['recent_tokens'] <= MAX_RECENT_MESSAGES_TOKENS
    assert sum(p[key] for key in ('summary_tokens', 'protected_tokens', 'recent_tokens', 'fixed_tokens')) == p['token_after']
    assert p['summarized_before'] > 900000
    assert messages[1]['content'] == '保留目标：只调查，不部署'


def test_recent_cap_does_not_cut_protected_large_messages_or_tool_pairs():
    from app.agent.context_compaction import _safe_split_point
    from app.agent.context_budget import estimate_messages_tokens
    messages = [{'role': 'assistant', 'content': '旧历史' * 5000} for _ in range(20)]
    tail = [{'role': 'assistant', 'tool_calls': [{'id': 'c', 'function': {'name': 'read_file', 'arguments': '{}'}}]},
            {'role': 'tool', 'tool_call_id': 'c', 'content': '大结果' * 20000}]
    messages += tail
    split = _safe_split_point(messages, keep_recent=2, recent_budget=MAX_RECENT_MESSAGES_TOKENS)
    assert messages[split:] == tail
    assert estimate_messages_tokens(messages[split:]) > MAX_RECENT_MESSAGES_TOKENS


def test_cli_displays_retained_content_separately(capsys):
    from app import cli
    from app.agent.events import RunEvent
    cli._print_run_event(RunEvent(kind=events.CONTEXT_COMPACTION_COMPLETED, payload={
        'token_before': 982884, 'token_after': 40000, 'summary_tokens': 4000,
        'protected_tokens': 1000, 'recent_tokens': 32000, 'fixed_tokens': 3000}))
    text = capsys.readouterr().out
    assert '982,884 → 40,000' in text
    assert '摘要 4,000' in text and '近期原文 32,000' in text and 'system/工具 3,000' in text
