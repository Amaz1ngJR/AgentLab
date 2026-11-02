"""A bottom composer using native terminal output, selection and scrollback."""
from contextlib import contextmanager
import sys
import threading
import time

from prompt_toolkit.formatted_text import to_formatted_text
from prompt_toolkit.filters import to_filter
from prompt_toolkit.layout.containers import VerticalAlign
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.patch_stdout import StdoutProxy
from prompt_toolkit.application.current import set_app
from prompt_toolkit.application import run_in_terminal
from app.util.progress_format import SPINNER_FRAMES, format_duration


class NativeStdoutProxy(StdoutProxy):
    """Bind terminal writes to the owner app even on a Python 3.14 flush thread."""
    def __init__(self, application):
        self._application = application
        super().__init__(raw=True)

    def _get_app_loop(self):
        return self._application.loop if self._application.is_running else None

    def _write_and_flush(self, loop, text):
        if loop is None:
            return super()._write_and_flush(None, text)

        async def emit():
            with set_app(self._application):
                await run_in_terminal(
                    lambda: super(NativeStdoutProxy, self)._write_and_flush(None, text),
                )

        loop.call_soon_threadsafe(lambda: loop.create_task(emit()))


class ComposerLayout:
    def __init__(self, prompt):
        self.prompt = prompt
        self.status = "Ready · 等待输入"
        self.mode = lambda: False
        self.running = False
        self.started = None
        self.cancelled = False
        self.activity = ""
        self.usage_summary = ""
        self.phase = "启动中"
        self._native_sink = sys.stdout
        self.suspended = False
        self._lock = threading.RLock()
        self.reset_usage()
        prompt.layout.container.align = VerticalAlign.BOTTOM
        for window in prompt.layout.find_all_windows():
            if getattr(window.content, "buffer", None) is prompt.default_buffer:
                window.dont_extend_height = to_filter(True)
        prompt.reserve_space_for_menu = 0
        prompt.mouse_support = False
        prompt.erase_when_done = False
        prompt.app.full_screen = False
        prompt.app.renderer.full_screen = False
        prompt.app.min_redraw_interval = .05
        self.navigation_bindings = KeyBindings()

    def reset_usage(self):
        with self._lock:
            self._usage_sources = {}
            self.usage_totals = {"input_tokens": 0, "output_tokens": 0}
            self._usage_known = {"input_tokens": 0, "output_tokens": 0}
            self._usage_authoritative = False
            self._refresh_usage_summary()

    def record_usage(self, source, metrics, *, final=False):
        """同一次调用覆盖快照，不同调用累加；刷新次数不会重复计费。"""
        with self._lock:
            if self._usage_authoritative:
                return
            old = self._usage_sources.get(source, {})
            current = {}
            for key in self.usage_totals:
                value = max(0, int(metrics.get(key, 0) or 0))
                known = final or value > 0 or old.get(key, (0, False))[1]
                before, was_known = old.get(key, (0, False))
                self.usage_totals[key] += value - before
                self._usage_known[key] += int(known) - int(was_known)
                current[key] = (value, known)
            self._usage_sources[source] = current
            self._refresh_usage_summary()

    def set_total_usage(self, usage):
        """轮次结束后，用运行层的实际累计值校准，包括未流式展示的调用。"""
        with self._lock:
            for key in self.usage_totals:
                self.usage_totals[key] = max(0, int(usage.get(key, 0) or 0))
                self._usage_known[key] = 1
            self._usage_authoritative = True
            self._refresh_usage_summary()

    def _refresh_usage_summary(self):
        def formatted(n):
            if n < 1000:
                return str(n)
            if n < 1_000_000:
                return f"{n / 1000:.1f}k" if n < 100_000 else f"{n / 1000:.0f}k"
            return f"{n / 1_000_000:.2f}m" if n < 100_000_000 else f"{n / 1_000_000:.1f}m"
        self.usage_summary = " · ".join(
            f"累计 {label} {formatted(self.usage_totals[key])} tokens" if self._usage_known[key]
            else f"累计 {label} 等待统计"
            for label, key in (("in", "input_tokens"), ("out", "output_tokens"))
        )

    def message(self, message):
        def formatted():
            width = max(1, self.prompt.app.output.get_size().columns - 1)
            fragments = self.status_fragments() + [("", "\n"), ("class:composer.rule", "─" * width + "\n")]
            fragments += list(to_formatted_text(message() if callable(message) else message))
            return fragments
        return formatted

    def finish_turn(self, *, cancelled=False, failed=False):
        details = []
        if self.started is not None:
            details.append(format_duration(time.monotonic() - self.started))
        if self.usage_summary:
            details.append(self.usage_summary)
        title, phase = (("Failed", "执行失败") if failed else
                        ("Stopped", "已停止") if cancelled else ("Done", "已完成"))
        details.append(phase)
        self.status = f"{title} ({' · '.join(details)})"
        self.started = None
        self.activity = ""
        self.running = False
        self.cancelled = False

    def status_fragments(self):
        status = self.status
        frame = "✻"
        if self.running:
            details = []
            elapsed = max(0, time.monotonic() - self.started) if self.started is not None else 0
            frame = SPINNER_FRAMES[int(elapsed * 10) % len(SPINNER_FRAMES)]
            if self.started is not None:
                details.append(format_duration(elapsed))
            if self.usage_summary:
                details.append(self.usage_summary)
            activity = self.activity() if callable(self.activity) else self.activity
            details.append("正在收尾" if self.cancelled else activity or self.phase)
            title = "Stopping…" if self.cancelled else "Thinking…"
            status = f"{title} ({' · '.join(details)})"
        return [("class:composer.status", frame + " " + status)]

    def append_output(self, text):
        # 正文由终端原生滚动缓冲保留，不再在全屏输出面板里重画/截断。
        self._native_sink.write(text)
        if not isinstance(self._native_sink, StdoutProxy):
            self._native_sink.flush()

    def hints(self):
        state = "auto mode on" if self.mode() else "auto mode off"
        action = "Enter 补充方向 · Esc/Ctrl+C 停止" if self.running else "Enter 发送 · / 命令 · Ctrl+D 退出"
        if self.cancelled:
            action = "正在停止，可继续编辑 · Enter 留待下一轮发送"
        width = max(1, self.prompt.app.output.get_size().columns - 1)
        return [("class:composer.rule", "─" * width + "\n"),
                ("class:composer.mode", "  » " + state),
                ("class:composer.hint", " · Ctrl+A 切换 · " + action)]


class _ComposerStream:
    def __init__(self, composer, original):
        self.composer, self.original = composer, original
        self._parts = []
        self._lock = threading.RLock()

    def write(self, text):
        if self.composer.suspended:
            return self.original.write(text)
        with self._lock:
            for part in text.splitlines(keepends=True):
                self._parts.append(part)
                if part.endswith("\n"):
                    self.composer.append_output("".join(self._parts))
                    self._parts.clear()
        return len(text)

    def flush(self):
        if self.composer.suspended:
            self.original.flush()

    def finish(self):
        with self._lock:
            if self._parts:
                self.composer.append_output("".join(self._parts) + "\n")
                self._parts.clear()

    def __getattr__(self, name):
        return getattr(self.original, name)


@contextmanager
def composer_output(composer):
    if composer is None:
        yield
        return
    stdout, stderr = sys.stdout, sys.stderr
    previous_sink = composer._native_sink
    # StdoutProxy 在输入区上方提交完整行，正文仍写入普通终端及其 scrollback。
    with NativeStdoutProxy(composer.prompt.app) as proxy:
        composer._native_sink = proxy
        stream = _ComposerStream(composer, stdout)
        sys.stdout = sys.stderr = stream
        try:
            yield
        finally:
            stream.finish()
            sys.stdout, sys.stderr = stdout, stderr
            composer._native_sink = previous_sink


def configure_composer(prompt):
    view = getattr(prompt, "_agentlab_composer", None)
    if view is None:
        view = ComposerLayout(prompt)
        prompt._agentlab_composer = view
    return view
