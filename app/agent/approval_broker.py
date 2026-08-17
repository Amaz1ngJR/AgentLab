"""异步审批协调器。

这个模块把“是否允许执行危险动作”的决策过程从 CLI 菜单中抽离出来：

- Executor 仍通过同步 ``ApprovalPolicy`` 接口请求审批，避免大规模修改现有执行链。
- ``ApprovalBroker`` 为每次审批生成稳定 request_id，并允许 CLI、HTTP 或 TUI
  订阅请求后异步提交决定。
- 当前 CLI 可通过 ``fallback`` 同步展示原有方向键菜单；未来 Web UI 可以不提供
  fallback，让执行线程挂起，直到 HTTP API 调用 ``resolve``。

Broker 只协调请求生命周期，不自行判断工具风险；风险判断仍由 ToolDescriptor、
Executor 和上层 Policy 负责。
"""
from __future__ import annotations

import asyncio
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from app.agent.approval import ApprovalPolicy, ApprovalResult, request_tool_approval


ApprovalDecision = Literal["approve", "deny"]


@dataclass(frozen=True)
class ApprovalRequest:
    """供前端展示和回应的结构化审批请求。

    ``tool`` 暂时保留 ToolDescriptor 对象，供进程内 CLI/TUI 展示风险元数据；
    HTTP 层后续应将其转换为可序列化字段，不能直接暴露任意 Python 对象。
    """

    request_id: str
    action: str
    tool_input: dict[str, Any]
    created_at: float
    timeout: float | None
    tool: Any = None


@dataclass
class _PendingApproval:
    """Broker 内部状态，不对前端暴露。"""

    request: ApprovalRequest
    # Executor 当前是同步执行链，因此用线程 Event 挂起，不在此处绑定 event loop。
    event: threading.Event = field(default_factory=threading.Event)
    result: ApprovalResult | None = None


class ApprovalBroker:
    """统一协调来自多个 run、多个前端的审批请求。

    所有共享状态都由 RLock 保护。回调和阻塞等待均在锁外执行，避免前端在回调中
    调用 ``resolve`` 时形成死锁。
    """

    def __init__(self, default_timeout: float | None = 300.0):
        # None 表示无限等待；产品入口建议保留有限超时，避免无人回应永久占用线程。
        self.default_timeout = default_timeout
        self._lock = threading.RLock()
        self._pending: dict[str, _PendingApproval] = {}
        self._subscribers: list[Callable[[ApprovalRequest], None]] = []
        self._closed = False

    def subscribe(self, callback: Callable[[ApprovalRequest], None]) -> Callable[[], None]:
        """订阅新审批请求，并返回幂等的取消订阅函数。"""
        with self._lock:
            if self._closed:
                raise RuntimeError("approval broker is closed")
            self._subscribers.append(callback)

        def unsubscribe() -> None:
            with self._lock:
                if callback in self._subscribers:
                    self._subscribers.remove(callback)

        return unsubscribe

    def pending(self) -> list[ApprovalRequest]:
        """返回当前未决请求快照，供断线重连后的前端重新获取。"""
        with self._lock:
            return [item.request for item in self._pending.values()]

    def request(
        self,
        action: str,
        tool_input: dict[str, Any],
        *,
        tool: Any = None,
        timeout: float | None = None,
        resolver: Callable[[], ApprovalResult | bool] | None = None,
    ) -> ApprovalResult:
        """登记请求并同步等待决定。

        ``resolver`` 是兼容现有 CLI 的桥：请求先发布给订阅者，再由 resolver 展示
        方向键菜单并把结果写回同一 pending request。Web/TUI 模式不传 resolver，
        由其他线程或请求处理器调用 ``resolve``。
        """
        effective_timeout = self.default_timeout if timeout is None else timeout
        request = ApprovalRequest(
            request_id=f"approval-{uuid.uuid4().hex}",
            action=action,
            # 复制最外层 dict，避免调用方随后修改参数导致审批界面和实际动作不一致。
            tool_input=dict(tool_input),
            created_at=time.time(),
            timeout=effective_timeout,
            tool=tool,
        )
        pending = _PendingApproval(request=request)
        with self._lock:
            if self._closed:
                return ApprovalResult(False, feedback="approval broker is closed", cancelled=True)
            self._pending[request.request_id] = pending
            subscribers = list(self._subscribers)

        # 订阅者异常不能破坏执行线程；前端可通过日志/事件系统单独观测自身失败。
        for callback in subscribers:
            try:
                callback(request)
            except Exception:
                pass

        if resolver is not None:
            try:
                resolved = resolver()
                result = (
                    resolved
                    if isinstance(resolved, ApprovalResult)
                    else ApprovalResult(bool(resolved))
                )
            except (KeyboardInterrupt, EOFError):
                result = ApprovalResult(False, cancelled=True)
            except Exception as exc:
                # UI 解析失败时安全拒绝，绝不能因为审批组件异常而默认放行。
                result = ApprovalResult(False, feedback=str(exc))
            self.resolve(request.request_id, result=result)

        signalled = pending.event.wait(effective_timeout)
        with self._lock:
            self._pending.pop(request.request_id, None)
        if not signalled or pending.result is None:
            return ApprovalResult(
                False,
                feedback="approval request timed out",
                cancelled=True,
            )
        return pending.result

    async def request_async(self, *args: Any, **kwargs: Any) -> ApprovalResult:
        """asyncio 入口：把同步等待放进工作线程，避免阻塞 Server event loop。"""
        return await asyncio.to_thread(self.request, *args, **kwargs)

    def resolve(
        self,
        request_id: str,
        decision: ApprovalDecision | None = None,
        *,
        feedback: str | None = None,
        result: ApprovalResult | None = None,
    ) -> bool:
        """回应一个未决请求。

        返回 False 表示请求不存在、已经超时或已被其他前端抢先回应。首个决定获胜，
        后续重复提交不会覆盖结果，避免两个浏览器窗口产生竞态。
        """
        with self._lock:
            pending = self._pending.get(request_id)
            if pending is None or pending.event.is_set():
                return False
            if result is None:
                if decision not in ("approve", "deny"):
                    raise ValueError("decision must be 'approve' or 'deny'")
                result = ApprovalResult(decision == "approve", feedback=feedback)
            pending.result = result
            pending.event.set()
            return True

    def close(self) -> None:
        """关闭 Broker，并拒绝/唤醒全部等待者，确保应用退出时不残留线程。"""
        with self._lock:
            self._closed = True
            pending_items = list(self._pending.values())
            self._pending.clear()
            self._subscribers.clear()
        for item in pending_items:
            item.result = ApprovalResult(
                False,
                feedback="approval broker closed",
                cancelled=True,
            )
            item.event.set()


class BrokerApprovalPolicy:
    """把 ApprovalBroker 适配为现有 Executor 使用的同步 ApprovalPolicy。

    ``fallback`` 用于兼容 CLI 的 InteractivePolicy/AutoApprove。未来 HTTP Server
    不设置 fallback，审批请求就会保持 pending，等待 API 调用 Broker.resolve。
    """

    def __init__(
        self,
        broker: ApprovalBroker,
        fallback: ApprovalPolicy | None = None,
        timeout: float | None = None,
    ):
        self.broker = broker
        self.fallback = fallback
        self.timeout = timeout

    def request(self, action: str, tool_input: dict[str, Any]) -> bool:
        resolver = None
        if self.fallback is not None:
            resolver = lambda: ApprovalResult(
                bool(self.fallback.request(action, tool_input))
            )
        return self.broker.request(
            action,
            tool_input,
            timeout=self.timeout,
            resolver=resolver,
        ).approved

    def request_tool(
        self,
        tool: Any,
        action: str,
        tool_input: dict[str, Any],
    ) -> ApprovalResult:
        # 使用 request_tool_approval 保留结构化 ToolDescriptor 和旧 Policy 兼容逻辑。
        resolver = None
        if self.fallback is not None:
            resolver = lambda: request_tool_approval(
                self.fallback,
                tool,
                action,
                tool_input,
            )
        return self.broker.request(
            action,
            tool_input,
            tool=tool,
            timeout=self.timeout,
            resolver=resolver,
        )
