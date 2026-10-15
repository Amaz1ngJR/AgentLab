"""Keep a prompt_toolkit composer active while a synchronous turn runs."""
from __future__ import annotations

import asyncio
import contextvars
import sys
import threading
from contextlib import contextmanager
from typing import Callable

from prompt_toolkit.application import in_terminal
from prompt_toolkit.application.current import set_app
from prompt_toolkit.key_binding import KeyBindings, merge_key_bindings
from prompt_toolkit.patch_stdout import StdoutProxy

from app.agent.cancel import CancelToken
from app.util.input_arbiter import clear_background_reader, set_background_reader


class _LineBufferedStream:
    """Commit complete lines above the composer; flush must not expose a tail."""

    def __init__(self, output):
        self._output = output
        self._pending = ""
        self._lock = threading.RLock()

    def write(self, text: str) -> int:
        with self._lock:
            self._pending += text
            end = self._pending.rfind("\n") + 1
            if end:
                self._output.write(self._pending[:end])
                self._pending = self._pending[end:]
        return len(text)

    def flush(self) -> None:
        # Complete lines are already queued by StdoutProxy.write. A flush of
        # partial text lets prompt_toolkit redraw over the output's first row.
        pass

    def finish(self) -> None:
        with self._lock:
            if self._pending:
                self._output.write(self._pending + "\n")
                self._pending = ""

    def __getattr__(self, name):
        return getattr(self._output, name)


@contextmanager
def _line_buffered_stdout():
    with StdoutProxy(raw=True) as proxy:
        stream = _LineBufferedStream(proxy)
        stdout, stderr = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = stream
        try:
            yield
        finally:
            try:
                stream.finish()
            finally:
                sys.stdout, sys.stderr = stdout, stderr


class LiveTurnInput:
    def __init__(self, prompt, *, message, style, prepare_input: Callable[[str], str]):
        self.prompt = prompt
        self.message = message
        self.style = style
        self.prepare_input = prepare_input
        self.draft = ""
        self.next_input: str | None = None
        self.status = ""
        self._loop = None
        self._token = None
        self._resume = None
        self._suspension = None
        self._worker_thread = None
        self.on_steer = None

    def invalidate(self) -> None:
        self.prompt.app.invalidate()

    def cancel(self) -> None:
        if self._token is not None:
            self._token.cancel()
            self.invalidate()

    def pause(self) -> None:
        """Yield stdin and rendering to the existing approval menu."""
        ready = threading.Event()
        self._suspension = asyncio.run_coroutine_threadsafe(self._suspend(ready), self._loop)
        ready.wait()
        if self._suspension.done():
            self._suspension.result()

    async def _suspend(self, ready: threading.Event) -> None:
        try:
            self._resume = asyncio.get_running_loop().create_future()
            with set_app(self.prompt.app):
                async with in_terminal():
                    ready.set()
                    await self._resume
        finally:
            ready.set()

    def resume(self) -> None:
        def release():
            if self._resume is not None and not self._resume.done():
                self._resume.set_result(None)
        self._loop.call_soon_threadsafe(release)
        # Do not let the next menu race with input/render restoration.
        if self._suspension is not None:
            self._suspension.result()

    def run(self, work: Callable[[], str], token: CancelToken) -> str:
        return asyncio.run(self._run(work, token))

    async def _run(self, work: Callable[[], str], token: CancelToken) -> str:
        try:
            return await self._run_turn(work, token)
        finally:
            # StdoutProxy.close joins its writer, whose final writes are scheduled
            # on this loop. Render them before asyncio.run closes the loop.
            await asyncio.sleep(0)
            await asyncio.sleep(0)

    async def _run_turn(self, work: Callable[[], str], token: CancelToken) -> str:
        self._loop = asyncio.get_running_loop()
        self._token = token
        done = self._loop.create_future()
        previous_bindings = self.prompt.key_bindings
        previous_toolbar = self.prompt.bottom_toolbar
        previous_refresh = self.prompt.refresh_interval
        previous_timeout = self.prompt.app.ttimeoutlen
        self.prompt.app.ttimeoutlen = 0.05
        bindings = KeyBindings()

        @bindings.add("escape", eager=True)
        @bindings.add("c-c", eager=True)
        def stop(event):
            self.cancel()

        def toolbar():
            if token.cancelled:
                return "已请求停止，可继续编辑输入；收尾完成后发送 · Esc/Ctrl+C 停止"
            return f"{self.status or '执行中'} · Enter 补充方向 · Esc/Ctrl+C 停止"

        combined_bindings = merge_key_bindings([previous_bindings or KeyBindings(), bindings])

        async def compose():
            while not done.done():
                try:
                    text = await self.prompt.prompt_async(
                        self.message, style=self.style, bottom_toolbar=toolbar,
                        key_bindings=combined_bindings,
                        refresh_interval=0.1,
                    )
                except KeyboardInterrupt:
                    self.cancel()
                    continue
                except EOFError:
                    self.cancel()
                    self.next_input = "exit"
                    return
                if not text.strip():
                    continue
                if token.cancelled or done.done():
                    self.next_input = "\n\n".join(filter(None, [self.next_input, text]))
                    continue
                if text.startswith("/"):
                    print("执行中可输入补充方向；管理命令请先按 Esc 停止。", flush=True)
                    continue
                text = self.prepare_input(text)
                if not text.strip():
                    print("图片已保留，将随下一轮消息发送；当前可输入文字补充方向。", flush=True)
                    continue
                accepted = self.on_steer(text) if self.on_steer is not None else token.steer(text)
                if accepted:
                    print("已收到补充指令，将在当前调用收尾后调整方向。", flush=True)
                else:
                    self.next_input = text
                    return

        def finish(outcome):
            if not done.done():
                done.set_result(outcome)

        def execute():
            try:
                outcome = (work(), None)
            except BaseException as exc:
                outcome = (None, exc)
            self._loop.call_soon_threadsafe(finish, outcome)

        input_task = None
        with _line_buffered_stdout():
            try:
                set_background_reader(self)
                input_task = asyncio.create_task(compose())
                # Install the terminal reader before execution can request approval.
                await asyncio.sleep(0)
                context = contextvars.copy_context()
                self._worker_thread = threading.Thread(
                    target=lambda: context.run(execute), name="agentlab-cli-turn", daemon=True,
                )
                self._worker_thread.start()
                result, error = await done
                self.draft = self.prompt.default_buffer.text if not input_task.done() else ""
                if error is not None:
                    raise error
                return result
            except BaseException:
                token.cancel()
                raise
            finally:
                clear_background_reader(self)
                if input_task is not None:
                    input_task.cancel()
                    await asyncio.gather(input_task, return_exceptions=True)
                self._token = None
                self._worker_thread = None
                self.prompt.key_bindings = previous_bindings
                self.prompt.bottom_toolbar = previous_toolbar
                self.prompt.refresh_interval = previous_refresh
                self.prompt.app.ttimeoutlen = previous_timeout
