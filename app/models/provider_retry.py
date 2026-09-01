"""Provider 请求重试、退避和基础熔断策略。"""
from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from app.agent.cancel import CancelToken


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
                self._opened_at = time.monotonic()


def _retry_after(exc: BaseException) -> float | None:
    value = getattr(exc, "retry_after", None)
    if value is None:
        response = getattr(exc, "response", None)
        headers = getattr(response, "headers", None)
        if headers is not None:
            value = headers.get("retry-after") or headers.get("Retry-After")
    try:
        return max(0.0, float(value)) if value is not None else None
    except (TypeError, ValueError):
        return None


def classify_provider_error(exc: BaseException) -> RetryDecision:
    """按 HTTP 状态/网络异常给出是否可安全重试的判断。"""
    # openai/httpx 异常通常暴露 status_code/response；stdlib urllib.HTTPError
    # 使用 code。Ollama 原生预热走 urllib，因此三种都要识别。
    status = (
        getattr(exc, "status_code", None)
        or getattr(exc, "code", None)
        or getattr(exc, "response", None)
    )
    if hasattr(status, "status_code"):
        status = status.status_code
    text = str(exc).lower()
    if status in {429, 502, 503, 504} or any(
        marker in text for marker in (
            "rate limit", "rate_limit", "overloaded", "timed out",
            "timeout", "connection reset", "connection refused",
        )
    ):
        return RetryDecision(True, retry_after_seconds=_retry_after(exc), reason="transient_provider_error")
    return RetryDecision(False, reason="non_retryable_provider_error")


def call_with_retry(
    call: Callable[[], Any],
    *,
    max_retries: int = 2,
    base_delay: float = 2.0,
    max_delay: float = 30.0,
    cancel: CancelToken | None = None,
    on_retry: Callable[[int, float, BaseException], None] | None = None,
    breaker: ProviderCircuitBreaker | None = None,
    sleep: Callable[[float], None] = time.sleep,
    random_fn: Callable[[], float] = random.random,
) -> Any:
    if max_retries < 0 or base_delay < 0 or max_delay < 0:
        raise ValueError("invalid retry settings")
    breaker = breaker or ProviderCircuitBreaker(failure_threshold=max_retries + 1)
    attempt = 0
    while True:
        if cancel is not None:
            cancel.raise_if_cancelled()
        breaker.before_call()
        try:
            result = call()
            breaker.record_success()
            return result
        except BaseException as exc:
            decision = classify_provider_error(exc)
            if decision.retryable:
                breaker.record_failure(exc)
            else:
                # 4xx/参数错误等说明 provider 可达，不应累计到网络/过载熔断。
                breaker.record_success()
            if not decision.retryable or attempt >= max_retries:
                raise
            attempt += 1
            delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
            delay += delay * 0.25 * random_fn()
            retry_after = decision.retry_after_seconds
            if retry_after is not None:
                delay = max(delay, retry_after)
            if on_retry is not None:
                on_retry(attempt, delay, exc)
            if cancel is not None:
                cancel.raise_if_cancelled()
            sleep(delay)


__all__ = [
    "CircuitOpenError",
    "ProviderCircuitBreaker",
    "RetryDecision",
    "call_with_retry",
    "classify_provider_error",
]
