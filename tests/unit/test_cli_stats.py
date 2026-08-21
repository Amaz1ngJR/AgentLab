"""CLI 统计数字的紧凑格式测试。"""
from unittest.mock import MagicMock

from app.cli import _fmt_duration, _fmt_tokens, _print_stats


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
