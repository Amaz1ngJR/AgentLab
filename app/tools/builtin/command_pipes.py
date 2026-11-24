"""Persistent stdin and output pipes for command sessions without a PTY."""
from __future__ import annotations

import codecs
import locale
import os
import queue
import subprocess
import threading
import time

from app.util.process import terminate_process_tree


class PipeCommandSession:
    def __init__(self, session_id: str, argv: list[str], cwd: str | None = None):
        self.id = session_id
        self.argv = argv
        self.wait_for_exit = True
        options = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
                   if os.name == "nt" else {"start_new_session": True})
        self._proc = subprocess.Popen(
            argv, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, bufsize=0, **options,
        )
        self._encoding = locale.getpreferredencoding(False)
        self._closed = threading.Event()
        self._output: queue.Queue[str | None] = queue.Queue(maxsize=256)
        self._eof = False
        threading.Thread(target=self._drain_output, daemon=True, name="agentlab-command-output").start()

    def _drain_output(self) -> None:
        decoder = codecs.getincrementaldecoder(self._encoding)(errors="replace")
        assert self._proc.stdout is not None
        with self._proc.stdout as stream:
            try:
                while not self._closed.is_set():
                    data = stream.read(4096)
                    if not data:
                        break
                    self._queue_output(decoder.decode(data))
            except OSError:
                pass
            finally:
                tail = decoder.decode(b"", final=True)
                if tail:
                    self._queue_output(tail)
                self._queue_output(None)

    def _queue_output(self, text: str | None) -> None:
        while not self._closed.is_set():
            try:
                self._output.put(text, timeout=0.1)
                return
            except queue.Full:
                continue

    @property
    def exit_code(self) -> int | None:
        return self._proc.poll()

    @property
    def alive(self) -> bool:
        return not self._closed.is_set() and self.exit_code is None

    @property
    def accepts_input(self) -> bool:
        return self.alive

    def read_until_idle(self, idle: float | None = None, timeout: float = 30, cancel=None) -> str:
        deadline = time.monotonic() + timeout
        parts = []
        last_data = time.monotonic()
        while (remaining := deadline - time.monotonic()) > 0:
            if cancel is not None:
                cancel.raise_if_cancelled()
            if self._eof or self._closed.is_set():
                if not self.alive or idle is not None:
                    break
                self._closed.wait(min(0.1, remaining))
                continue
            try:
                text = self._output.get(timeout=min(0.1, remaining))
            except queue.Empty:
                if parts and idle is not None and time.monotonic() - last_data >= idle:
                    break
                continue
            if text is None:
                self._eof = True
                continue
            parts.append(text)
            last_data = time.monotonic()
        return "".join(parts).replace("\r\n", "\n")

    def send(self, data: str, append_newline: bool = True, *, cancel=None) -> None:
        if not self.accepts_input:
            raise OSError("session closed")
        if cancel is not None:
            cancel.raise_if_cancelled()
        if append_newline and not data.endswith("\n"):
            data += "\n"
        payload = data.encode(self._encoding)
        finished = threading.Event()
        errors = []

        def write():
            try:
                assert self._proc.stdin is not None
                pending = memoryview(payload)
                while pending:
                    count = self._proc.stdin.write(pending)
                    if not count:
                        raise OSError("stdin pipe closed")
                    pending = pending[count:]
            except (OSError, ValueError) as exc:
                errors.append(exc)
            finally:
                finished.set()

        threading.Thread(target=write, daemon=True, name="agentlab-command-input").start()
        deadline = time.monotonic() + 1
        while not finished.wait(0.05):
            if cancel is not None:
                cancel.raise_if_cancelled()
            if time.monotonic() >= deadline:
                raise OSError("stdin write timed out; input may have been partially written")
        if errors:
            raise OSError(str(errors[0]))

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        if self._proc.poll() is None:
            terminate_process_tree(self._proc)
        if self._proc.stdin is not None:
            self._proc.stdin.close()
        try:
            self._proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            threading.Thread(target=self._proc.wait, daemon=True, name="agentlab-command-reap").start()
