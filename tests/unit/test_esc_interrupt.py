"""测试 Esc 中断检测 —— cli.py 的 _scan_for_esc / _EscWatcher。"""
import sys
from unittest.mock import patch

from app.cli import _EscWatcher, _scan_for_esc


# ── _scan_for_esc:单独 Esc vs 转义序列 ────────────────────────────────────────


def test_scan_lone_esc():
    """单独的 Esc 字节(0x1b)被识别。"""
    assert _scan_for_esc(b"\x1b") is True


def test_scan_esc_at_end():
    """Esc 在数据末尾(后面无字节)算单独 Esc。"""
    assert _scan_for_esc(b"abc\x1b") is True


def test_scan_arrow_key_not_esc():
    """方向键 ESC [ A 是转义序列,不算 Esc。"""
    assert _scan_for_esc(b"\x1b[A") is False  # 上
    assert _scan_for_esc(b"\x1b[B") is False  # 下
    assert _scan_for_esc(b"\x1b[C") is False  # 右
    assert _scan_for_esc(b"\x1b[D") is False  # 左


def test_scan_esc_O_sequence_not_esc():
    """ESC O 开头的功能键序列(如 F1=ESC O P)不算 Esc。"""
    assert _scan_for_esc(b"\x1bOP") is False


def test_scan_no_esc():
    """普通字节里没有 Esc。"""
    assert _scan_for_esc(b"hello") is False
    assert _scan_for_esc(b"") is False


def test_scan_esc_then_plain_char():
    """Esc 后面跟普通字符(非 [ / O)算单独 Esc(用户按了 Esc 又按别的)。"""
    assert _scan_for_esc(b"\x1bx") is True


def test_scan_esc_among_other_bytes():
    """一堆字节中间夹一个单独 Esc 也能识别。"""
    assert _scan_for_esc(b"ab\x1bcd") is True


def test_scan_arrow_then_esc():
    """先方向键(转义序列)再单独 Esc:仍能识别出后面的 Esc。"""
    assert _scan_for_esc(b"\x1b[A\x1b") is True


# ── _EscWatcher:降级行为 ──────────────────────────────────────────────────────


def test_esc_watcher_disabled_on_windows():
    """Windows 上不启用(无 termios)。"""
    with patch("platform.system", return_value="Windows"):
        w = _EscWatcher(on_esc=lambda: None)
        assert w._enabled is False


def test_esc_watcher_disabled_when_not_tty():
    """stdin 非 TTY(管道/重定向)时不启用。"""
    with patch("platform.system", return_value="Darwin"), \
         patch.object(sys.stdin, "isatty", return_value=False):
        w = _EscWatcher(on_esc=lambda: None)
        assert w._enabled is False


def test_esc_watcher_noop_context_when_disabled():
    """禁用时作为上下文管理器是空操作,不抛异常、不动终端。"""
    with patch("platform.system", return_value="Windows"):
        w = _EscWatcher(on_esc=lambda: None)
        with w as ctx:
            assert ctx is w
        # 退出不报错即可
