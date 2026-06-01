"""离线测试：CLI spinner 行数计算与文本重绘逻辑。"""
import io
import re

from app.agent.tasks import COMPLETED, IN_PROGRESS, PENDING, Task, TaskStore
from app.cli import _Spinner, _count_visual_lines, _display_width, _format_task_lines, _strip_ansi


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


# ── spinner 流式渲染:append-only,长文本不重影 ────────────────────────────────


def _clean_ansi(s: str) -> str:
    """去掉光标移动 / 清屏等 ANSI 控制序列,只留可见字符,便于计数。"""
    s = re.sub(r"\033\[[0-9;]*[A-Za-z]", "", s)
    return s.replace("\r", "")


def _drive_spinner_text(deltas, monkeypatch):
    """实例化 _Spinner,把 stdout 换成 StringIO,逐段喂文本(不启动动画线程)。

    on_text / _erase_footer 等都直写 sys.stdout 模块属性,monkeypatch 即可拦截。
    不调用 __enter__(),避免后台线程的时序干扰;手动设好 _t0。
    """
    import app.cli as cli

    sink = io.StringIO()
    monkeypatch.setattr(cli.sys, "stdout", sink)
    sp = _Spinner("thinking", task_store=None)
    sp._t0 = 0.0
    for d in deltas:
        sp.on_text(d)
    return sink.getvalue()


def test_streamed_text_is_append_only_no_duplication(monkeypatch):
    """逐字流式一段超长多行文本:每行只出现一次,不因重绘叠成瀑布。

    这是旧 _redraw 的回归点 —— 旧实现每个 delta 重打整段缓冲,文本超过一屏
    滚动后擦不掉,导致同一行被打印几十上百次。
    """
    marker = "这个项目的目录结构如下："
    body = (marker + "\n") * 60  # 60 行,真实场景会滚屏
    out = _drive_spinner_text(list(body), monkeypatch)  # 逐字符喂
    clean = _clean_ansi(out)
    # marker 恰好出现 60 次,既不重影也不丢失
    assert clean.count(marker) == 60


def test_streamed_text_preserves_exact_content(monkeypatch):
    """append-only 必须原样保留文本内容(顺序 / 字符都不变)。"""
    out = _drive_spinner_text(["Hello, ", "本地", "模型!"], monkeypatch)
    assert _clean_ansi(out) == "Hello, 本地模型!"


def test_first_text_delta_erases_thinking_footer(monkeypatch):
    """thinking footer 画出后,第一段文本到达时应擦掉它(写过清屏序列 \\033[J)。"""
    import app.cli as cli

    sink = io.StringIO()
    monkeypatch.setattr(cli.sys, "stdout", sink)
    sp = _Spinner("thinking", task_store=None)
    sp._t0 = 0.0
    sp._draw_footer()              # 先画 thinking footer
    assert sp._footer_lines > 0
    sp.on_text("结果")            # 第一段文本应擦掉 footer
    assert sp._footer_lines == 0   # footer 已撤
    assert sp._text_started is True
    assert "\033[J" in sink.getvalue()  # 擦除用到了清屏到末尾


def test_task_panel_committed_once(monkeypatch):
    """任务面板一步内只提交一次,不随每个文本 delta 重打。"""
    import app.cli as cli

    sink = io.StringIO()
    monkeypatch.setattr(cli.sys, "stdout", sink)
    store = TaskStore()
    store.replace_all([Task("1", "读取项目结构", IN_PROGRESS)])
    sp = _Spinner("thinking", task_store=store)
    sp._t0 = 0.0
    for d in ["a", "b", "c"]:
        sp.on_text(d)
    # 任务内容只出现一次(不是每个 delta 一次)
    assert _clean_ansi(sink.getvalue()).count("读取项目结构") == 1
