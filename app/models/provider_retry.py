"""Provider 请求重试、退避和基础熔断策略。"""
from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from app.agent.cancel import CancelToken, Cancelled


@dataclass(frozen=True)
class RetryDecision:
    retryable: bool
    retry_after_seconds: float | None = None
    reason: str = ""


class CircuitOpenError(RuntimeError):
    """Provider 连续瞬时失败后进入短暂熔断，并保留最近一次原始异常。"""

    def __init__(
        self,
        retry_after_seconds: float,
        last_error: BaseException | None = None,
    ) -> None:
        self.retry_after = max(0.0, float(retry_after_seconds))
        self.last_error = last_error
        message = f"provider circuit open; retry after {self.retry_after:g}s"
        if last_error is not None:
            message += f"; last failure: {type(last_error).__name__}: {last_error}"
        super().__init__(message)


class ProviderCircuitBreaker:
    def __init__(self, *, failure_threshold: int = 3, recovery_seconds: float = 30.0):
        if failure_threshold <= 0 or recovery_seconds < 0:
            raise ValueError("invalid circuit breaker settings")
        self.failure_threshold = failure_threshold
        self.recovery_seconds = recovery_seconds
        self._failures = 0
        self._opened_at: float | None = None
        self._last_error: BaseException | None = None
        self._lock = threading.RLock()

    @property
    def is_open(self) -> bool:
        with self._lock:
            if self._opened_at is None:
                return False
            if time.monotonic() - self._opened_at >= self.recovery_seconds:
                self._opened_at = None
                self._failures = 0
                self._last_error = None
                return False
            return True

    def before_call(self) -> None:
        if self.is_open:
            with self._lock:
                elapsed = (
                    time.monotonic() - self._opened_at
                    if self._opened_at is not None else 0.0
                )
                remaining = max(0.0, self.recovery_seconds - elapsed)
                error = CircuitOpenError(remaining, self._last_error)
            if error.last_error is not None:
                raise error from error.last_error
            raise error

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._opened_at = None
            self._last_error = None

    def record_failure(self, exc: BaseException | None = None) -> None:
        with self._lock:
            self._failures += 1
            if exc is not None:
                self._last_error = exc
            if self._failures >= self.failure_threshold:
                # 已经打开的窗口不做任何改动：重置 _opened_at 会让恢复时间无限顺延，
                # 让 provider 永远等不到"窗口过去后放一次探测"的机会。
                if self._opened_at is None:
                    self._opened_at = time.monotonic()


def _retry_after(exc: BaseException) -> float | None:
    value = getattr(exc, "retry_after", None)
    if value is None:
        response = getattr(exc, "response", None)
        headers = getattr(response, "headers", None)
        if headers is not None:
            try:
                value = headers.get("retry-after") or headers.get("Retry-After")
            except (AttributeError, TypeError):
                value = None
    try:
        return max(0.0, float(value)) if value is not None else None
    except (TypeError, ValueError):
        return None


def _status_code(exc: BaseException) -> int | None:
    """取出异常携带的 HTTP 状态码，取不到就回 None。

    openai/httpx 异常通常暴露 status_code/response，stdlib urllib.HTTPError 用
    code，Ollama 原生预热走 urllib —— 所以三种都要看。但 ``response`` 可能是任意
    对象或容器，直接拿去 `in {429, ...}` 会抛 TypeError（把真实错误盖掉），因此这里
    统一收口成 int，并兼容 ``response.status_code`` / ``response.status``。
    """
    for candidate in (
        getattr(exc, "status_code", None),
        getattr(exc, "code", None),
        getattr(exc, "response", None),
    ):
        if isinstance(candidate, bool):  # bool 是 int 的子类，必须排除
            continue
        if isinstance(candidate, int):
            return candidate
        for attr in ("status_code", "status"):
            value = getattr(candidate, attr, None)
            if isinstance(value, int) and not isinstance(value, bool):
                return value
    return None


# 可重试的异常类名。按类名而不是 import 具体类型判断，一是避免为 httpx /
# openai / anthropic 这些可选依赖引入硬导入，二是 SDK 会把底层网络异常包进
# 自己的异常类，只有顺着异常链按名字匹配才抓得全。
#
# 代价：这是按名字匹配，任何第三方库的同名类都会被一起捕获（urllib3 的
# ProtocolError 就包含"响应体过大"这类不该重试的语义）。所以下面的判定一律要求
# 异常自身没有携带 HTTP 状态码 —— 带状态码说明服务端已经明确回应，按状态码分类
# 比按类名猜测更可信。
_RETRYABLE_EXCEPTION_NAMES = frozenset({
    # httpx / httpcore：连接失败、各类超时、连接中途断开
    "ConnectError", "ConnectTimeout", "ReadTimeout", "WriteTimeout",
    "PoolTimeout", "TimeoutException", "ReadError", "WriteError",
    "RemoteProtocolError", "ProtocolError", "NetworkError",
    "LocalProtocolError", "ClosedResourceError",
    # anthropic / openai SDK 包装后的连接类异常
    "APIConnectionError", "APITimeoutError",
    # stdlib socket / ssl：ConnectionResetError 等均是 ConnectionError 子类
    "ConnectionError", "ConnectionResetError", "ConnectionAbortedError",
    "ConnectionRefusedError", "BrokenPipeError", "TimeoutError",
    "IncompleteReadError", "SSLError", "SSLEOFError", "gaierror",
})


def _definitive_status(status: int | None) -> bool:
    """状态码是否代表"服务端已明确给出错误回应"。

    只有 4xx/5xx 才算。2xx/3xx 出现在异常上意味着"HTTP 层已经成功、失败发生在读
    body 的过程中"（SSE 中途断流就是这种形态），此时不能拿它当"服务端已回应、
    不必重试"的证据 —— 那会把真正可重试的中途断流判死。
    """
    return status is not None and status >= 400


def _is_network_exception(exc: BaseException) -> bool:
    """沿异常链判断是否为可重试的网络/连接类异常。

    SDK（anthropic/openai）用 ``raise APIConnectionError(...) from inner`` 包装
    httpx 异常，所以既要看异常自身的 MRO，也要看 ``__cause__``。

    链上遇到**带 4xx/5xx 状态码**的异常时停止下探：服务端已明确给出错误回应，失败
    原因就是那个响应，而不是更底层的连接异常。否则
    ``APIStatusError(401) → HTTPStatusError(401) → ConnectError`` 这种链（同一个
    请求先连不上、重试后又收到 401）会把该 401 判成"网络抖动、可以重试"。

    带 2xx 状态码的异常不停止下探：那是中途断流（响应头已到、body 读到一半失败），
    底层的 ReadError/ConnectionResetError 才是真正原因。
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        for klass in type(current).__mro__:
            if klass.__name__ in _RETRYABLE_EXCEPTION_NAMES:
                return True
        if _definitive_status(_status_code(current)):
            return False
        current = current.__cause__ or current.__context__
    return False


def classify_provider_error(exc: BaseException) -> RetryDecision:
    """按 HTTP 状态/网络异常给出是否可安全重试的判断。"""
    # 熔断器已经替当前调用做出快速失败决策。调用方不应在这里等待整个恢复窗口，
    # 否则每个请求都会占住执行线程几十秒。
    if isinstance(exc, CircuitOpenError):
        return RetryDecision(False, retry_after_seconds=exc.retry_after,
                             reason="provider_circuit_open")

    # 异常自带的可重试标记优先。adapter 在解析网关流式失败（response.failed）时
    # 比这里更清楚病因：参数/协议类失败是确定性的，重试只会重复；只有服务端
    # 瞬时故障才该退避重试。按类名 import 会形成 adapter→retry 的循环依赖，
    # 因此用鸭子类型读这个属性。
    explicit = getattr(exc, "retryable", None)
    if explicit is True:
        return RetryDecision(True, retry_after_seconds=_retry_after(exc),
                             reason="provider_stream_retryable")
    if explicit is False:
        return RetryDecision(False, reason="provider_stream_terminal")

    # 状态码优先：拿到 int 状态码说明服务端已经明确回应，按状态码分类比顺着异常链
    # 或消息文本猜类型更可信（也避免 `status in {...}` 因 status 是容器而抛 TypeError）。
    status = _status_code(exc)
    if _definitive_status(status):
        if status in _RETRYABLE_STATUS:
            return RetryDecision(True, retry_after_seconds=_retry_after(exc),
                                 reason="transient_provider_error")
        return RetryDecision(False, reason="non_retryable_provider_error")

    # 网络层异常优先按类型判定：SDK 的连接异常消息往往是泛化的
    # "Connection error."，httpx.ReadTimeout 的 str() 甚至是空串，
    # 只靠下面的关键字匹配会把最常见的"网络断开"漏判成不可重试。
    if _is_network_exception(exc):
        return RetryDecision(True, retry_after_seconds=_retry_after(exc),
                             reason="transient_provider_error")

    # 走到这里说明既没有 4xx/5xx 状态码、也不在网络异常白名单（含 __cause__ 链）里。
    # 消息文本可以放心参考：带 4xx/5xx 的异常已在上面返回。2xx 状态码是"响应头已到、
    # 读 body 中途失败"，属于可重试的中途断流，不能因为看到状态码就不看消息。
    if any(
        marker in str(exc).lower() for marker in (
            "rate limit", "rate_limit", "overloaded", "timed out",
            "timeout", "connection reset", "connection refused",
            "connection error", "server disconnected", "incomplete read",
            "eof occurred", "eof", "temporarily unavailable", "bad gateway",
            "service unavailable", "gateway timeout", "network is unreachable",
        )
    ):
        return RetryDecision(True, retry_after_seconds=_retry_after(exc),
                             reason="transient_provider_error")
    return RetryDecision(False, reason="non_retryable_provider_error")


# 可重试的 HTTP 状态码。4xx 里只有 429（限流）值得重试；500/529 覆盖面很广，
# 包含服务端真实 bug 与 Anthropic 的 overloaded（529），但对用户来说都表现为
# "provider 暂时不可用"，重试成本远低于让用户手动重发。
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504, 529})


def _interruptible_sleep(
    delay: float,
    *,
    cancel: CancelToken | None,
    sleep: Callable[[float], None],
    slice_seconds: float = 0.2,
) -> None:
    """睡眠期间持续检查取消，避免长退避（最长 30s）让 Esc/Ctrl-C 失灵。

    用注入的 ``sleep`` 分片等待，而不是一次性长睡：测试传入的假 sleep 仍能
    精确记录各段时长，生产环境则是真实 time.sleep。

    分片数按次数累加（而不是 ``remaining -= step`` 反复相减），避免浮点误差让
    30s 退避变成 29.999996s 这种脏数字。
    """
    if cancel is None:
        sleep(delay)
        return
    if delay <= 0:
        cancel.raise_if_cancelled()
        return
    slices = max(1, int(delay / slice_seconds + 0.999999))
    step = delay / slices
    for _ in range(slices):
        cancel.raise_if_cancelled()
        sleep(step)


def call_with_retry(
    call: Callable[[], Any],
    *,
    max_retries: int | None = 2,
    base_delay: float = 2.0,
    max_delay: float = 30.0,
    cancel: CancelToken | None = None,
    on_retry: Callable[[int, float, BaseException], None] | None = None,
    breaker: ProviderCircuitBreaker | None = None,
    sleep: Callable[[float], None] = time.sleep,
    random_fn: Callable[[], float] = random.random,
    discard_partial: Callable[[BaseException], None] | None = None,
) -> Any:
    """带退避重试地调用 ``call``。

    ``max_retries=None`` 表示持续重试瞬时 provider 故障，供显式全自动运行使用；
    确定性 4xx/参数错误仍立即抛出。调用方应同时传入 CancelToken，让用户可随时停止。

    ``discard_partial`` 在一次尝试失败时被调用（携带该次异常）。流式调用里
    ``call`` 可能在失败前已经通过回调把半个答案推给了 UI，重试会把同一段内容再推
    一遍（屏幕重复），更糟的是"残文"会被当成最终答案。调用方在这里把自己的
    增量缓冲作废，重试才是干净的。

    回调安全：``discard_partial`` / ``on_retry`` 触碰 UI 与终端（清屏、写 stdout），
    可能抛异常（终端状态异常、BrokenPipe、锁竞争…）。它们抛错绝不能打断重试循环，
    否则一次网络抖动就会直接变成"任务中断"。
    """
    if (max_retries is not None and max_retries < 0) or base_delay < 0 or max_delay < 0:
        raise ValueError("invalid retry settings")
    breaker = breaker or ProviderCircuitBreaker(
        failure_threshold=(max_retries + 1) if max_retries is not None else 3,
    )
    attempt = 0

    def _safe(callback: Callable[..., None] | None, *args: Any) -> None:
        if callback is None:
            return
        try:
            callback(*args)
        except Exception:
            pass  # 通知/清理失败不能连累重试本身

    while True:
        if cancel is not None:
            cancel.raise_if_cancelled()
        # 熔断属于调用前的快速失败，不消耗本次重试预算，也不在这里等待恢复窗口。
        try:
            breaker.before_call()
        except CircuitOpenError as exc:
            if max_retries is not None:
                raise
            attempt += 1
            delay = max(0.2, exc.retry_after)
            _safe(on_retry, attempt, delay, exc)
            _interruptible_sleep(delay, cancel=cancel, sleep=sleep)
            continue
        try:
            result = call()
            if cancel is not None:
                cancel.raise_if_cancelled()
            breaker.record_success()
            return result
        except Cancelled:
            raise
        except Exception as exc:
            if cancel is not None:
                # 关闭流通常会让 SDK 抛连接/流异常；若关闭来自用户取消，应统一
                # 转成 Cancelled，不能再进入网络重试。
                cancel.raise_if_cancelled()
            decision = classify_provider_error(exc)
            if decision.retryable:
                breaker.record_failure(exc)
            elif not decision.retryable:
                # 4xx/参数错误等说明 provider 可达，不应累计到网络/过载熔断。
                breaker.record_success()
            if not decision.retryable or (
                max_retries is not None and attempt >= max_retries
            ):
                raise
            attempt += 1
            # 失败即作废本次尝试已泄漏给 UI 的增量，重试才不会把同一段内容推两遍。
            _safe(discard_partial, exc)
            delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
            delay += delay * 0.25 * random_fn()
            retry_after = decision.retry_after_seconds
            if retry_after is not None:
                delay = max(delay, retry_after)
            _safe(on_retry, attempt, delay, exc)
            _interruptible_sleep(delay, cancel=cancel, sleep=sleep)


__all__ = [
    "CircuitOpenError",
    "ProviderCircuitBreaker",
    "RetryDecision",
    "call_with_retry",
    "classify_provider_error",
]
