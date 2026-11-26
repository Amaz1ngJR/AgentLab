"""CLI 统计数字的紧凑格式测试。"""
from unittest.mock import MagicMock
from types import SimpleNamespace

import pytest

from app.cli import _chat_with_cancel, _fmt_duration, _fmt_tokens, _format_request_usage, _print_stats


def test_duration_formats_seconds_minutes_and_hours():
    assert _fmt_duration(12.34) == "12.3s"
    assert _fmt_duration(751.0) == "12m 31s"
    assert _fmt_duration(23405.1) == "6h 30m 5s"


def test_tokens_format_k_and_m():
    assert _fmt_tokens(999) == "999"
    assert _fmt_tokens(9_270) == "9.3k"
    assert _fmt_tokens(126_143) == "126k"
    assert _fmt_tokens(11_220_137) == "11.22m"
    assert _fmt_tokens(122_437_064) == "122.4m"


def test_print_stats_uses_compact_values(capsys):
    session = MagicMock()
    session.last_turn_seconds = 751.0
    session.cumulative_seconds = 23405.1
    session.last_turn_usage = {"input_tokens": 11_220_137, "output_tokens": 9_270}
    session.cumulative_usage = {"input_tokens": 122_437_064, "output_tokens": 126_143}
    session.last_actual_model = None
    _print_stats(session)
    output = capsys.readouterr().out
    assert "turn 12m 31s in=11.22m out=9.3k" in output
    assert "session 6h 30m 5s in=122.4m out=126k" in output


def test_request_usage_distinguishes_cache_read_write_and_missing_usage():
    line = _format_request_usage(1, {"provider": "openai", "input_tokens": 1000,
                                    "cached_tokens": 0, "output_tokens": 30})
    assert "in=1.0k out=30" in line and "缓存命中=0 (0.0%)" in line

    line = _format_request_usage(2, {"provider": "anthropic", "input_tokens": 100,
                                    "cached_tokens": 800, "cache_creation_input_tokens": 100,
                                    "output_tokens": 25, "attempt": 2})
    assert "in=1.0k out=25" in line and "缓存命中=800 (80.0%)" in line
    assert "未命中=200" in line and "写入缓存=100" in line and "第 2 次尝试" in line

    line = _format_request_usage(3, {"status": "failed", "input_tokens": 1000})
    assert "out=未知" in line and "缓存命中=未知 (未知)" in line and "失败" in line

    line = _format_request_usage(4, {"provider": "openai_chatgpt", "input_tokens": 1000,
                                    "cached_tokens": 800, "output_tokens": 25, "sent_input_items": 1,
                                    "transport": "websocket", "continued_response_id": "resp_1"})
    assert "缓存命中=800 (80.0%)" in line and "WS 新增 1 项" in line


@pytest.mark.parametrize("interactive", [False, True])
def test_cli_prints_usage_during_current_turn_only(interactive, monkeypatch):
    from app import cli

    lines = []
    monkeypatch.setattr(cli, "_emit_progress_notice", lambda text, marker: lines.append((marker, text)))
    service = MagicMock()
    unsubscribe = service.subscribe_protocol.return_value

    def send_message(line, *, run_id, **kwargs):
        callback = service.subscribe_protocol.call_args.args[0]
        callback(SimpleNamespace(turn_id="other", kind="model.request.finished", payload={}))
        callback(SimpleNamespace(turn_id=run_id, kind="model.request.retry", payload={}))
        for cached in (0, 980):
            callback(SimpleNamespace(turn_id=run_id, kind="model.request.finished", payload={
                "input_tokens": 1000, "cached_tokens": cached, "output_tokens": 10,
            }))
        assert len(lines) == 2  # Printed before send_message returns.
        return "完成"

    service.send_message.side_effect = send_message
    live = SimpleNamespace(run=lambda execute, token: execute()) if interactive else None
    assert _chat_with_cancel(SimpleNamespace(), "读取", service=service, live_input=live) == "完成"
    assert lines[0][0] == lines[1][0] == "[request]"
    assert "#1" in lines[0][1] and "0.0%" in lines[0][1]
    assert "#2" in lines[1][1] and "98.0%" in lines[1][1]
    unsubscribe.assert_called_once_with()
