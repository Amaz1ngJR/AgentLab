"""Provider retry/backoff/circuit-breaker tests."""
import threading

import pytest

from app.agent.cancel import CancelToken, Cancelled
from app.models.provider_retry import (
    CircuitOpenError,
    ProviderCircuitBreaker,
    call_with_retry,
    classify_provider_error,
)


class HttpError(RuntimeError):
    def __init__(self, status_code, message="error", retry_after=None):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


def test_classify_only_transient_provider_errors_as_retryable():
    assert classify_provider_error(HttpError(503)).retryable
    assert classify_provider_error(HttpError(403, "account overdue")).retryable is False
    assert classify_provider_error(RuntimeError("connection reset by peer")).retryable


def test_retry_uses_exponential_backoff_and_retry_after():
    failures = [HttpError(503), HttpError(503, retry_after=9)]
    calls = []
    delays = []

    def call():
        calls.append(1)
        if failures:
            raise failures.pop(0)
        return "ok"

    assert call_with_retry(
        call,
        max_retries=2,
        base_delay=2,
        random_fn=lambda: 0,
        sleep=delays.append,
    ) == "ok"
    assert len(calls) == 3
    assert delays == [2, 9]


def test_retry_does_not_repeat_non_retryable_error():
    calls = []

    def call():
        calls.append(1)
        raise HttpError(403, "account overdue")

    with pytest.raises(HttpError):
        call_with_retry(call, sleep=lambda _: calls.append("sleep"))
    assert calls == [1]


def test_retry_checks_cancel_before_sleep():
    token = CancelToken()
    sleeps = []

    def call():
        raise HttpError(503)

    def on_retry(*_):
        token.cancel()

    with pytest.raises(Cancelled):
        call_with_retry(
            call,
            max_retries=2,
            on_retry=on_retry,
            sleep=sleeps.append,
            cancel=token,
        )
    assert sleeps == []


def test_circuit_breaker_opens_after_threshold_and_recovers():
    breaker = ProviderCircuitBreaker(failure_threshold=2, recovery_seconds=0)
    breaker.record_failure()
    assert breaker.is_open is False
    breaker.record_failure()
    assert breaker.is_open is False  # zero-second recovery is immediately available

    breaker = ProviderCircuitBreaker(failure_threshold=2, recovery_seconds=60)
    breaker.record_failure()
    breaker.record_failure()
    with pytest.raises(CircuitOpenError):
        breaker.before_call()
    breaker.record_success()
    breaker.before_call()


def test_retry_opens_breaker_after_exhausting_transient_failures():
    breaker = ProviderCircuitBreaker(failure_threshold=2, recovery_seconds=60)

    with pytest.raises(HttpError):
        call_with_retry(
            lambda: (_ for _ in ()).throw(HttpError(503)),
            max_retries=1,
            breaker=breaker,
            sleep=lambda _: None,
        )
    with pytest.raises(CircuitOpenError):
        breaker.before_call()
