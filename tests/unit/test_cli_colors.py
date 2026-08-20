"""CLI 输出配色回归测试。"""
from unittest.mock import patch

from app.agent import events as run_events
from app.agent.events import RunEvent
from app.cli import (
    _ANSI_DIM,
    _ANSI_GREEN,
    _ANSI_YELLOW_BOLD,
    _ANSI_WHITE,
    _approval_text,
    _model_text,
    _print_run_event,
    _thinking_text,
)
from app.util.menu import _MENU_STYLE


class _TTY:
    def isatty(self):
        return True


def test_output_color_helpers_use_requested_palette():
    tty = _TTY()
    assert _model_text("answer", stream=tty).startswith(_ANSI_WHITE)
    assert _thinking_text("reasoning", stream=tty).startswith(_ANSI_DIM)
    assert _approval_text("tool_use", stream=tty).startswith(_ANSI_YELLOW_BOLD)


def test_output_color_helpers_disable_ansi_for_non_tty():
    with patch.dict("os.environ", {"NO_COLOR": "1"}):
        assert _model_text("answer", stream=_TTY()) == "answer"
        assert _thinking_text("reasoning", stream=_TTY()) == "reasoning"
        assert _approval_text("tool_use", stream=_TTY()) == "tool_use"


def test_approval_menu_keeps_blue_selection_and_yellow_content():
    selected = _MENU_STYLE.style_rules
    styles = dict(selected)
    assert "#5fafff" in styles["selected"]
    assert "#ffd75f" in styles["header"]
    assert "#ffd75f" in styles["title"]


def test_approval_required_event_uses_tool_use_label(capsys):
    event = RunEvent(
        kind=run_events.APPROVAL_REQUIRED,
        tool_name="shell",
        tool_input={"command": "pwd"},
        payload={"approval_action": "shell"},
    )
    _print_run_event(event)
    output = capsys.readouterr().out
    assert "tool_use shell" in output
    assert "pwd" in output
