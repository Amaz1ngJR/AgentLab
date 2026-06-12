"""CancelToken —— 协作式取消信号。

Runtime 是同步单线程循环,无法被强行打断;取消采用"协作式":调用方(CLI 的
Ctrl-C 处理、Web 的 Stop 按钮)调用 token.cancel(),编排循环在每个安全检查点
(claim 下一个任务前、调用模型前、执行工具前)调用 token.raise_if_cancelled(),
抛出 Cancelled 让循环干净退出。

不依赖线程/asyncio,纯标志位,便于离线测试:测试里直接 token.cancel() 再跑一步
即可验证取消路径。
"""
from __future__ import annotations


class Cancelled(Exception):
    """协作式取消被触发时抛出。编排循环捕获它并以 run_failed(已取消) 收尾。"""


class CancelToken:
    """一次性取消标志。cancel() 幂等;cancelled 只读当前状态。"""

    def __init__(self) -> None:
        self._cancelled = False

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def cancel(self) -> None:
        self._cancelled = True

    def raise_if_cancelled(self) -> None:
        if self._cancelled:
            raise Cancelled()
