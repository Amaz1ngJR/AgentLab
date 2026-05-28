"""离线测试：CLI spinner 行数计算与文本重绘逻辑。"""
from app.agent.tasks import COMPLETED, IN_PROGRESS, PENDING, Task
from app.cli import _count_visual_lines, _display_width, _format_task_lines, _strip_ansi


def test_display_width_ascii():
    assert _display_width("hello") == 5


def test_display_width_chinese():
    # 中文字符显示宽度为 2
    assert _display_width("你好") == 4


def test_display_width_mixed():
    assert _display_width("hi 你好") == 7   # 2 + 1 + 4


def test_count_lines_empty():
    assert _count_visual_lines("", 80) == 0


def test_count_lines_single_short():
    assert _count_visual_lines("hello", 80) == 1


def test_count_lines_multiple_explicit():
    assert _count_visual_lines("a\nb\nc", 80) == 3


def test_count_lines_trailing_newline():
    # rstrip("\n") 后变 "abc\ndef"，2 行
    assert _count_visual_lines("abc\ndef\n", 80) == 2


def test_count_lines_soft_wrap_ascii():
    # 100 字符在 80 列宽下占 2 行
    text = "a" * 100
    assert _count_visual_lines(text, 80) == 2


def test_count_lines_soft_wrap_chinese():
    # 50 个中文 = 100 显示宽度，80 列下占 2 行
    text = "你" * 50
    assert _count_visual_lines(text, 80) == 2


def test_count_lines_long_paragraph():
    # 模拟实际场景：长文本在窄终端下软换行
    text = "你" * 200  # 200 中文 = 400 显示宽度
    # 80 列下应该占 5 行
    assert _count_visual_lines(text, 80) == 5
    # 40 列下应该占 10 行
    assert _count_visual_lines(text, 40) == 10


def test_count_lines_mixed_explicit_and_soft():
    # 第一行 100 ASCII (80 列下占 2 行) + 第二行 5 字 = 1 行 + 第三行空 = 1 行
    text = "a" * 100 + "\nhello\n"
    # rstrip("\n") = "aaa...\nhello"
    # 第一行 ceil(100/80) = 2
    # 第二行 ceil(5/80) = 1
    assert _count_visual_lines(text, 80) == 3


# ── 任务面板渲染 ──────────────────────────────────────────────────────────────


def test_format_task_lines_empty():
    assert _format_task_lines([]) == []


def test_format_task_lines_summary_and_items():
    tasks = [
        Task("1", "first", COMPLETED),
        Task("2", "second", IN_PROGRESS),
        Task("3", "third", PENDING),
    ]
    lines = _format_task_lines(tasks)
    # 第一行是汇总
    assert "3 tasks" in _strip_ansi(lines[0])
    assert "1 done" in _strip_ansi(lines[0])
    assert "1 in progress" in _strip_ansi(lines[0])
    assert "1 open" in _strip_ansi(lines[0])
    # 后续是各任务,1 个 + 3 个 = 4 行
    assert len(lines) == 4
    assert "✓ first" in _strip_ansi(lines[1])
    assert "❯ second" in _strip_ansi(lines[2])
    assert "○ third" in _strip_ansi(lines[3])


def test_strip_ansi_removes_color_codes():
    raw = "\033[1;34mhello\033[0m world"
    assert _strip_ansi(raw) == "hello world"


def test_format_task_lines_uses_distinct_styles():
    """不同状态的任务行带不同 ANSI(具体颜色字符不重要,但应该有差异)。"""
    tasks = [
        Task("1", "a", COMPLETED),
        Task("2", "b", IN_PROGRESS),
        Task("3", "c", PENDING),
    ]
    lines = _format_task_lines(tasks)
    # completed 和 in_progress 必须有 ANSI 转义,pending 可以没有
    assert "\033[" in lines[1]   # completed 有颜色
    assert "\033[" in lines[2]   # in_progress 有颜色
    # 内容字符
    assert "✓ a" in lines[1]
    assert "❯ b" in lines[2]
    assert "○ c" in lines[3]
