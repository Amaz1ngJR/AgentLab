"""Provider retry/backoff/circuit-breaker tests."""
import threading
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

import pytest

from app.agent.cancel import CancelToken, Cancelled
from app.models.provider_retry import (
    CircuitOpenError,
    ProviderCircuitBreaker,
    call_with_retry,
    classify_provider_error,
)
from app.models.router import ModelRouter


class HttpError(RuntimeError):
    def __init__(self, status_code, message="error", retry_after=None):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


def test_classify_only_transient_provider_errors_as_retryable():
    assert classify_provider_error(HttpError(503)).retryable
    assert classify_provider_error(HttpError(403, "account overdue")).retryable is False
    assert classify_provider_error(RuntimeError("connection reset by peer")).retryable
    assert classify_provider_error(HTTPError("http://ollama", 502, "bad gateway", {}, None)).retryable


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


def test_non_retryable_error_does_not_open_breaker():
    breaker = ProviderCircuitBreaker(failure_threshold=1, recovery_seconds=60)
    with pytest.raises(HttpError):
        call_with_retry(
            lambda: (_ for _ in ()).throw(HttpError(400, "bad request")),
            breaker=breaker,
        )
    breaker.before_call()


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


def test_open_circuit_reports_original_failure_and_remaining_delay():
    breaker = ProviderCircuitBreaker(failure_threshold=1, recovery_seconds=60)
    original = HttpError(503, "ollama runner unavailable")
    breaker.record_failure(original)
    with pytest.raises(CircuitOpenError) as caught:
        breaker.before_call()
    assert caught.value.last_error is original
    assert caught.value.retry_after > 0
    assert "ollama runner unavailable" in str(caught.value)


def test_model_router_gives_ollama_runner_a_longer_retry_window():
    adapter = SimpleNamespace(
        _cfg=SimpleNamespace(provider="ollama"),
        create_message=lambda *args, **kwargs: "ok",
    )
    router = ModelRouter(adapter)

    with patch("app.models.router.call_with_retry", return_value="ok") as retry:
        assert router.create_message([{"role": "user", "content": "hi"}]) == "ok"

    assert retry.call_args.kwargs["max_retries"] == 4
    assert retry.call_args.kwargs["max_delay"] == 10.0


def test_model_router_keeps_default_retry_window_for_cloud_providers():
    adapter = SimpleNamespace(
        _cfg=SimpleNamespace(provider="openai"),
        create_message=lambda *args, **kwargs: "ok",
    )
    router = ModelRouter(adapter)

    with patch("app.models.router.call_with_retry", return_value="ok") as retry:
        assert router.create_message([{"role": "user", "content": "hi"}]) == "ok"

    assert retry.call_args.kwargs["max_retries"] == 2
    assert retry.call_args.kwargs["max_delay"] == 30.0


def test_model_router_preloads_ollama_with_declared_context_size():
    adapter = SimpleNamespace(_cfg=SimpleNamespace(
        provider="ollama",
        model="qwen3:14b",
        base_url="http://127.0.0.1:11434/v1",
        context_size=8192,
        timeout_seconds=60,
    ))
    router = ModelRouter(adapter)
    response = MagicMock()
    response.__enter__.return_value.read.return_value = b"{}"

    with patch("app.models.router.urllib.request.urlopen", return_value=response) as open_url:
        router._warmup_ollama()
        router._warmup_ollama()  # 活跃窗口内不重复预热

    assert open_url.call_count == 1
    request = open_url.call_args.args[0]
    assert request.full_url == "http://127.0.0.1:11434/api/generate"
    payload = json.loads(request.data)
    assert payload["model"] == "qwen3:14b"
    assert payload["options"]["num_ctx"] == 8192
    assert payload["keep_alive"] == "5m"
