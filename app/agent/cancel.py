"""Per-run cancellation and steering mailbox.

Execution owns conversation mutations. Input can request cancellation or queue
steering from another thread; messages are consumed only at tool-safe boundaries.
Blocking reads can be isolated, but writes must implement cooperative cancellation.
"""
from __future__ import annotations

import threading
import contextvars
from contextlib import contextmanager
from typing import Callable, Iterator, TypeVar

T = TypeVar("T")


class Cancelled(Exception):
    """协作式取消被触发时抛出，由执行循环和 Service 以 cancelled 收尾。"""


class CancelToken:
    """一次性取消标志。cancel() 幂等;cancelled 只读当前状态。"""

    def __init__(self) -> None:
        self._cancelled = False
        self._lock = threading.RLock()
        self._callbacks: dict[int, Callable[[], None]] = {}
        self._next_callback_id = 0
        self._event = threading.Event()
        self._steering: list[str] = []
        self._accepting_steering = True

    @property
    def cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    def cancel(self) -> None:
        with self._lock:
            if self._cancelled:
                return
            self._cancelled = True
            self._event.set()
            callbacks = list(self._callbacks.values())
            self._callbacks.clear()
        for callback in callbacks:
            try:
                callback()
            except Exception:
                # 取消必须尽力通知所有资源；单个 close 失败不能阻断其他回调。
                pass

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise Cancelled()

    def wait(self, timeout: float | None = None) -> bool:
        return self._event.wait(timeout)

    def steer(self, text: str) -> bool:
        """Queue user input; only the execution thread may mutate conversation history."""
        if not text.strip():
            raise ValueError("steer input must not be empty")
        with self._lock:
            if self._cancelled or not self._accepting_steering:
                return False
            self._steering.append(text)
            return True

    @property
    def has_steering(self) -> bool:
        with self._lock:
            return bool(self._steering)

    def drain_steering(self) -> list[str]:
        with self._lock:
            pending, self._steering = self._steering, []
            return pending

    def finish_or_drain_steering(self) -> list[str]:
        """Atomically finish an idle mailbox or return input racing with completion."""
        with self._lock:
            pending, self._steering = self._steering, []
            if not pending or self._cancelled:
                self._accepting_steering = False
            return pending

    def add_callback(self, callback: Callable[[], None]) -> Callable[[], None]:
        """注册即时取消回调，返回幂等的退订函数。

        若令牌已经取消，回调会在注册线程中立即执行，避免检查与注册之间的竞态。
        """
        with self._lock:
            if self._cancelled:
                callback_id = None
            else:
                callback_id = self._next_callback_id
                self._next_callback_id += 1
                self._callbacks[callback_id] = callback

        if callback_id is None:
            try:
                callback()
            except Exception:
                pass

        def unsubscribe() -> None:
            if callback_id is None:
                return
            with self._lock:
                self._callbacks.pop(callback_id, None)

        return unsubscribe

    @contextmanager
    def callback(self, callback: Callable[[], None]) -> Iterator[None]:
        """在阻塞操作存活期间注册回调，退出后自动退订。"""
        unsubscribe = self.add_callback(callback)
        try:
            yield
        finally:
            unsubscribe()


@contextmanager
def close_on_cancel(cancel: CancelToken | None, resource: object) -> Iterator[None]:
    """取消时关闭流式资源；资源不支持 close 时退化为普通上下文。"""
    close = getattr(resource, "close", None)
    if cancel is None or not callable(close):
        yield
        return
    def request_close() -> None:
        # SDK close may wait on a blocked socket. Never run it on the input thread.
        def cleanup() -> None:
            try:
                close()
            except Exception:
                pass
        threading.Thread(target=cleanup, name="agentlab-stream-close", daemon=True).start()

    with cancel.callback(request_close):
        yield


def apply_steering(cancel: CancelToken | None, messages: list[dict]) -> list[str]:
    """Call only when all outstanding tool calls have matching results."""
    if cancel is None:
        return []
    pending = cancel.drain_steering()
    for text in pending:
        messages.append({"role": "user", "content": text})
    return pending


def run_read_only_cancellable(work: Callable[[], T], cancel: CancelToken) -> T:
    """Detach a blocked read only; callers must not pass writes or session mutations."""
    cancel.raise_if_cancelled()
    done = threading.Event()
    outcome: list = []

    def invoke() -> None:
        try:
            outcome.append((work(), None))
        except BaseException as exc:
            outcome.append((None, exc))
        finally:
            done.set()

    context = contextvars.copy_context()
    threading.Thread(
        target=lambda: context.run(invoke), name="agentlab-read-request", daemon=True,
    ).start()
    while not done.wait(0.02):
        cancel.raise_if_cancelled()
    cancel.raise_if_cancelled()
    value, error = outcome[0]
    if error is not None:
        raise error
    return value
