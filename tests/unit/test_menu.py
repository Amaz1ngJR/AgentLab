"""离线测试：select_menu 的非 TTY 退化路径。

select_menu 在 TTY 下会弹方向键交互界面,无法在 pytest 里直接测;
但非 TTY 会走 _select_menu_fallback,从 stdin 读数字,可以 mock 测试。
"""
from unittest.mock import patch

from app.util.menu import _select_menu_fallback, select_menu


def test_fallback_returns_choice_value():
    with patch("builtins.input", return_value="2"):
        result = _select_menu_fallback(
            choices=[("a", "VAL_A"), ("b", "VAL_B"), ("c", "VAL_C")],
            header_lines=None,
            title="pick one",
        )
    assert result == "VAL_B"


def test_fallback_invalid_index_returns_none():
    with patch("builtins.input", return_value="9"):
        result = _select_menu_fallback(
            choices=[("a", "VAL_A"), ("b", "VAL_B")],
            header_lines=None,
            title="pick",
        )
    assert result is None


def test_fallback_non_numeric_returns_none():
    with patch("builtins.input", return_value="hello"):
        result = _select_menu_fallback(
            choices=[("a", "VAL_A")],
            header_lines=None,
            title="pick",
        )
    assert result is None


def test_fallback_eof_returns_none():
    with patch("builtins.input", side_effect=EOFError):
        result = _select_menu_fallback(
            choices=[("a", "VAL_A")],
            header_lines=None,
            title="pick",
        )
    assert result is None


def test_select_menu_empty_choices_returns_none():
    """空 choices 列表直接返回 None,不弹 UI。"""
    assert select_menu(choices=[]) is None


def test_select_menu_routes_to_fallback_when_not_tty():
    """sys.stdout 不是 tty 时走 _select_menu_fallback,不会启动 prompt_toolkit。"""
    with patch("sys.stdout") as mock_stdout, patch("sys.stdin") as mock_stdin, \
         patch("builtins.input", return_value="1"):
        mock_stdout.isatty.return_value = False
        mock_stdin.isatty.return_value = False
        result = select_menu(
            choices=[("yes", True), ("no", False)],
            title="pick",
        )
    assert result is True
