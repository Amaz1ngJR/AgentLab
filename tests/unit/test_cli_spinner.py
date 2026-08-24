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


def test_failed_and_blocked_tasks_show_reason():
    from app.agent.tasks import BLOCKED, FAILED

    lines = _format_task_lines([
        Task("1", "同步配置", FAILED, error="任务在 10 步内未完成"),
        Task("2", "等待审批", BLOCKED, error="用户拒绝执行 shell"),
    ])
    plain = [_strip_ansi(line) for line in lines]
    assert "✗ 同步配置" in plain[1]
    assert plain[2] == "    原因: 任务在 10 步内未完成"
    assert "⊘ 等待审批" in plain[3]
    assert plain[4] == "    原因: 用户拒绝执行 shell"


def test_failed_reason_is_single_line_bounded_and_has_fallback():
    from app.agent.tasks import FAILED

    long_reason = "第一行\n" + "x" * 300
    lines = [_strip_ansi(line) for line in _format_task_lines([
        Task("1", "失败任务", FAILED, error=long_reason),
        Task("2", "无原因任务", FAILED),
    ])]
    reason_lines = [line for line in lines if "原因:" in line]
    assert "\n" not in reason_lines[0]
    assert reason_lines[0].endswith("…")
    assert len(reason_lines[0]) < 180
    assert reason_lines[1] == "    原因: 未记录失败原因"


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


class _FakeTerminal:
    """最小终端模拟器:解释 _Spinner 用到的控制序列,还原屏幕最终可见内容。

    新版 _Spinner 的渲染策略是"擦掉 footer → append 正文 → 重画 footer",擦除靠
    \\033[J(清屏到末尾)真正回收旧 footer + 当前未换行尾行,所以重画 _line_buf
    在真实终端上不产生重影。要验证这一点,测试必须 *应用* 控制序列而不是只删码。

    支持:\\r(回车到行首)、\\033[nA(光标上移 n 行)、\\033[J(清光标到屏幕末尾)、
    其余 ANSI(颜色等)忽略。简化假设:每个逻辑行不超过终端宽度(这些测试满足),
    故 1 逻辑行 = 1 视觉行,与实现的行数计算一致。
    """

    def __init__(self):
        self.rows = [""]
        self.cy = 0   # 光标行
        self.cx = 0   # 光标列(按字符数,测试输入无需 display-width 精度)

    def feed(self, s: str) -> None:
        i = 0
        while i < len(s):
            ch = s[i]
            if ch == "\033":
                m = re.match(r"\033\[([0-9;?]*)([A-Za-z])", s[i:])
                if not m:
                    i += 1
                    continue
                arg, cmd = m.group(1), m.group(2)
                if cmd == "A":  # 光标上移
                    n = int(arg) if arg.isdigit() else 1
                    self.cy = max(0, self.cy - max(1, n))
                    self.cx = min(self.cx, len(self.rows[self.cy]))
                elif cmd == "J":  # 清除光标到屏幕末尾
                    self.rows[self.cy] = self.rows[self.cy][:self.cx]
                    del self.rows[self.cy + 1:]
                # 其它(颜色 m 等)忽略
                i += m.end()
                continue
            if ch == "\r":
                self.cx = 0
            elif ch == "\n":
                self.cy += 1
                if self.cy >= len(self.rows):
                    self.rows.append("")
                self.cx = 0
            else:
                row = self.rows[self.cy]
                self.rows[self.cy] = row[:self.cx] + ch + row[self.cx + 1:]
                self.cx += 1
            i += 1

    def screen(self) -> str:
        return "\n".join(self.rows)


def _drive_spinner_text(deltas, monkeypatch) -> str:
    """实例化 _Spinner,把 stdout 接到终端模拟器,逐段喂文本(不启动动画线程)。

    末尾调一次 _erase_footer() 模拟 turn 收尾撤掉常驻 footer,返回屏幕可见内容。
    不调用 __enter__(),避免后台线程时序干扰;手动设好 _t0。
    """
    import app.cli as cli

    term = _FakeTerminal()
    sink = io.StringIO()
    # 同时喂模拟器(看屏幕)与 StringIO(看原始字节,供个别断言)
    orig_write = sink.write

    def _write(s):
        term.feed(s)
        return orig_write(s)

    sink.write = _write  # type: ignore[method-assign]
    monkeypatch.setattr(cli.sys, "stdout", sink)
    sp = _Spinner("thinking", task_store=None)
    sp._t0 = 0.0
    for d in deltas:
        sp.on_text(d)
    sp._erase_footer()  # turn 收尾:撤掉常驻 footer
    return term.screen()


def test_spinner_status_distinguishes_unknown_input_and_live_output(monkeypatch):
    import app.cli as cli

    sink = io.StringIO()
    monkeypatch.setattr(cli.sys, "stdout", sink)
    spinner = _Spinner("planning")
    spinner._t0 = 0.0
    assert "token 等待中" in spinner._fmt_status()
    spinner.update({"input_tokens": 0, "output_tokens": 9})
    assert "out ~9 tokens" in spinner._fmt_status()
    spinner.update({"input_tokens": 3900, "output_tokens": 12, "final": True})
    status = spinner._fmt_status()
    assert "in 3.9k tokens" in status
    assert "out 12 tokens" in status


    """迟到的中间 usage 不能让实时 token 倒退；final 可用真值校准。"""
    import app.cli as cli

    sink = io.StringIO()
    monkeypatch.setattr(cli.sys, "stdout", sink)
    spinner = _Spinner("thinking")
    spinner._t0 = 0.0
    spinner.update({"input_tokens": 100, "output_tokens": 8})
    spinner.update({"input_tokens": 100, "output_tokens": 3})
    assert spinner._metrics["output_tokens"] == 8
    spinner.update({"input_tokens": 120, "output_tokens": 5, "final": True})
    assert spinner._metrics == {"input_tokens": 120, "output_tokens": 5}


    """逐字流式一段超长多行文本:每行只出现一次,不因重绘叠成瀑布。

    这是 _erase_footer 有界擦除的回归点 —— footer 与未换行尾行靠 \\033[J 真正
    清掉,已换行滚走的正文从不触碰,所以同一行不会被重复打印。
    """
    marker = "这个项目的目录结构如下："
    body = (marker + "\n") * 60  # 60 行,真实场景会滚屏
    screen = _drive_spinner_text(list(body), monkeypatch)  # 逐字符喂
    # marker 恰好出现 60 次,既不重影也不丢失
    assert screen.count(marker) == 60


def test_streamed_text_preserves_exact_content(monkeypatch):
    """append-only 必须原样保留文本内容(顺序 / 字符都不变)。"""
    screen = _drive_spinner_text(["Hello, ", "本地", "模型!"], monkeypatch)
    # 收尾擦掉 footer 后,屏幕只剩正文(可能带尾随空行)
    assert screen.rstrip("\n") == "Hello, 本地模型!"


def test_thinking_renderer_preserves_legitimate_repeated_lines(monkeypatch):
    """终端层不做语义去重；合法重复由 Provider 增量规范化保证不被误删。"""
    import app.cli as cli

    sink = io.StringIO()
    monkeypatch.setattr(cli.sys, "stdout", sink)
    progress = cli._PlainProgress("thinking")
    progress.on_thinking("same\n")
    progress.on_thinking("other\n")
    progress.on_thinking("same\n")
    plain = _strip_ansi(sink.getvalue())
    assert plain.count("same") == 2
    assert plain.count("other") == 1


    """thinking footer 画出后,第一段文本到达时应擦掉它(写过清屏序列 \\033[J)。"""
    import app.cli as cli

    sink = io.StringIO()
    monkeypatch.setattr(cli.sys, "stdout", sink)
    sp = _Spinner("thinking", task_store=None)
    sp._t0 = 0.0
    sp._draw_footer()                   # 先画 thinking footer
    assert sp._footer_rows > 0
    sink.truncate(0)
    sink.seek(0)
    sp.on_text("结果")                  # 第一段文本到达
    assert sp._any_text is True
    # 擦除 footer 用到了清屏到末尾;之后会在正文下方重画 footer(常驻策略)
    assert "\033[J" in sink.getvalue()
    assert sp._footer_rows > 0          # 重画后 footer 仍常驻


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
    assert _strip_ansi(sink.getvalue()).count("读取项目结构") == 1


def test_plan_created_then_spinner_same_state_dedup(monkeypatch):
    """PLAN_CREATED 打印面板后,紧跟的同状态 spinner 不重复打印(跨路径去重)。

    回归点:orchestrator 在 PLAN_CREATED 后立刻 _maybe_compact() 开一个
    "compacting" spinner,此时还没 claim 任务,task_store 仍全 pending,与
    PLAN_CREATED 面板状态完全相同。两者共享 panel_state 才能去重。
    """
    import app.cli as cli
    from app.agent import events as run_events
    from app.agent.events import RunEvent
    from app.agent.tasks import PENDING

    sink = io.StringIO()
    monkeypatch.setattr(cli.sys, "stdout", sink)

    # 共享的 panel_state(模拟 _make_progress 返回的那个)
    panel_state = {"last": None}
    store = TaskStore()
    store.replace_all([Task("t1", "任务一", PENDING), Task("t2", "任务二", PENDING)])
    snapshot = store.snapshot()

    # 1. PLAN_CREATED 事件打印面板(全 pending)
    cli._print_run_event(
        RunEvent(kind=run_events.PLAN_CREATED, payload={"tasks": snapshot}),
        panel_state=panel_state,
    )
    # 2. 紧跟的 compacting spinner 读同一全 pending 状态
    sp = _Spinner("compacting", task_store=store, panel_state=panel_state)
    sp._t0 = 0.0
    sp._commit_tasks()

    out = _strip_ansi(sink.getvalue())
    # 任务内容只出现一次(PLAN_CREATED 打了,spinner 去重跳过)
    assert out.count("任务一") == 1
    assert out.count("任务二") == 1
