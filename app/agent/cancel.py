"""CancelToken —— 协作式取消信号。

Runtime 是同步单线程循环,无法被强行打断;取消采用"协作式":调用方(CLI 的
Ctrl-C 处理、Web 的 Stop 按钮)调用 token.cancel(),编排循环在每个安全检查点
(claim 下一个任务前、调用模型前、执行工具前)调用 token.raise_if_cancelled(),
抛出 Cancelled 让循环干净退出。

令牌同时支持取消回调。模型流、子进程等阻塞资源可以注册 close/terminate，Esc
置位时主动唤醒阻塞调用，而不是只能等到下一个检查点。
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Callable, Iterator


class Cancelled(Exception):
    """协作式取消被触发时抛出。编排循环捕获它并以 run_failed(已取消) 收尾。"""


class CancelToken:
    """一次性取消标志。cancel() 幂等;cancelled 只读当前状态。"""

    def __init__(self) -> None:
        self._cancelled = False
        self._lock = threading.Lock()
        self._callbacks: dict[int, Callable[[], None]] = {}
        self._next_callback_id = 0

    @property
    def cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    def cancel(self) -> None:
        with self._lock:
            if self._cancelled:
                return
            self._cancelled = True
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
    with cancel.callback(close):
        yield
