"""Provider retry/backoff/circuit-breaker tests."""
import threading

import pytest

from app.agent.cancel import CancelToken, Cancelled
from app.models.provider_retry import (
    CircuitOpenError,
    ProviderCircuitBreaker,
    _interruptible_sleep,
    call_with_retry,
    classify_provider_error,
)


class HttpError(RuntimeError):
    def __init__(self, status_code, message="error", retry_after=None):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


class ReadError(Exception):
    """同名模拟 httpx.ReadError（类名匹配即可，无需真实依赖）。"""


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


def test_continuous_retry_survives_transient_failures_until_success():
    failures = [HttpError(503), HttpError(503), HttpError(503)]
    calls = []

    def call():
        calls.append(1)
        if failures:
            raise failures.pop(0)
        return "ok"

    assert call_with_retry(
        call,
        max_retries=None,
        base_delay=0,
        random_fn=lambda: 0,
        sleep=lambda _: None,
        breaker=ProviderCircuitBreaker(failure_threshold=3, recovery_seconds=0),
    ) == "ok"
    assert len(calls) == 4


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


def test_cancelled_call_does_not_change_circuit_breaker_state():
    """用户取消不是 provider 成功或失败，不应改写熔断器状态。"""
    breaker = ProviderCircuitBreaker(failure_threshold=2, recovery_seconds=60)
    breaker.record_failure(ConnectionResetError(54, "reset"))

    with pytest.raises(Cancelled):
        call_with_retry(
            lambda: (_ for _ in ()).throw(Cancelled("stopped")),
            breaker=breaker,
            sleep=lambda _: None,
        )

    assert breaker._failures == 1


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


# ── 网络异常分类：回归测试 ─────────────────────────────────────────────────────
# 这些异常以前全被判为"不可重试"，是"网络一断就要用户手动发继续"的根因。

def test_sdk_connection_errors_are_retryable_by_type():
    """SDK 的连接异常消息是泛化的 'Connection error.'，只能靠类型识别。"""
    class APIError(Exception):
        pass

    class APIConnectionError(APIError):
        pass

    class APITimeoutError(APIError):
        pass

    assert classify_provider_error(APIConnectionError("Connection error.")).retryable
    assert classify_provider_error(APITimeoutError("Request timed out.")).retryable


def test_httpx_style_errors_are_retryable_by_type():
    """httpx 的 ReadTimeout 等 str() 为空串，关键字匹配抓不到。"""

    class TransportError(Exception):
        pass

    class ReadTimeout(TransportError):
        pass

    class ConnectError(TransportError):
        pass

    class RemoteProtocolError(TransportError):
        pass

    class ReadError(TransportError):
        pass

    for exc in (
        ReadTimeout(""),
        ConnectError(""),
        RemoteProtocolError("Server disconnected"),
        ReadError(""),
    ):
        assert classify_provider_error(exc).retryable, type(exc).__name__


def test_wrapped_network_exception_follows_cause_chain():
    """SDK 会用 `raise APIConnectionError(...) from inner` 包装底层异常。"""
    class APIConnectionError(Exception):
        pass

    class ConnectError(Exception):
        pass

    wrapped = APIConnectionError("Connection error.")
    wrapped.__cause__ = ConnectError("underlying")
    assert classify_provider_error(wrapped).retryable


def test_stdlib_network_errors_are_retryable():
    import socket
    import ssl

    assert classify_provider_error(ConnectionResetError(54, "reset")).retryable
    assert classify_provider_error(BrokenPipeError(32, "broken pipe")).retryable
    assert classify_provider_error(socket.gaierror(-2, "dns")).retryable
    assert classify_provider_error(ssl.SSLError("EOF occurred")).retryable
    assert classify_provider_error(TimeoutError("timed out")).retryable


def test_http_5xx_and_overload_status_still_retryable():
    for status in (429, 500, 502, 503, 504, 529):
        assert classify_provider_error(HttpError(status)).retryable, status


def test_genuine_client_errors_stay_non_retryable():
    """4xx 参数/鉴权错误重试没有意义，不能退化成无脑重试。"""
    for status in (400, 401, 403, 404, 422):
        assert classify_provider_error(HttpError(status, "bad")).retryable is False, status


def test_circuit_open_error_fails_fast_without_resetting_window():
    """熔断中的新请求应快速失败，不能占住执行线程等待整个恢复窗口。"""
    breaker = ProviderCircuitBreaker(failure_threshold=1, recovery_seconds=60)
    breaker.record_failure(ConnectionResetError(54, "reset"))
    opened_at = breaker._opened_at
    sleeps = []
    notices = []

    assert classify_provider_error(CircuitOpenError(5.0)).retryable is False

    with pytest.raises(CircuitOpenError):
        call_with_retry(
            lambda: "unreachable",
            max_retries=5,
            breaker=breaker,
            sleep=sleeps.append,
            on_retry=lambda *args: notices.append(args),
        )
    assert sleeps == []
    assert notices == []
    assert breaker.is_open is True
    assert breaker._opened_at == opened_at


def test_long_backoff_is_interruptible_by_cancel():
    """最长 30s 的退避必须能被 Esc/Ctrl-C 打断，不能睡死。"""
    token = CancelToken()
    slept = []

    def call():
        raise ConnectionResetError(54, "reset")

    def on_retry(*_):
        token.cancel()

    with pytest.raises(Cancelled):
        call_with_retry(
            call,
            max_retries=3,
            base_delay=30,
            random_fn=lambda: 0,
            on_retry=on_retry,
            sleep=slept.append,
            cancel=token,
        )
    # 取消发生在退避开始前，不该真的睡满 30s
    assert sum(slept) == 0


def test_retry_notice_reports_attempt_and_delay():
    """重试要能上报给 UI，避免用户以为卡死而手动发'继续'。"""
    seen = []

    def call():
        raise ConnectionResetError(54, "reset")

    with pytest.raises(ConnectionResetError):
        call_with_retry(
            call,
            max_retries=2,
            base_delay=1,
            random_fn=lambda: 0,
            on_retry=lambda attempt, delay, exc: seen.append((attempt, delay, type(exc).__name__)),
            sleep=lambda _: None,
        )
    assert [s[0] for s in seen] == [1, 2]
    assert [s[1] for s in seen] == [1, 2]
    assert all(s[2] == "ConnectionResetError" for s in seen)


# ── 部分输出作废：流式重试不能把同一段内容推两遍 ────────────────────────────────

def test_discard_partial_called_before_each_retry():
    """每次重试前都要作废已流出的半截输出，否则 spinner 会把答案推第二遍。"""
    discarded = []

    def call():
        raise ConnectionResetError(54, "reset")

    with pytest.raises(ConnectionResetError):
        call_with_retry(
            call,
            max_retries=2,
            base_delay=1,
            random_fn=lambda: 0,
            discard_partial=lambda exc: discarded.append(type(exc).__name__),
            sleep=lambda _: None,
        )
    assert discarded == ["ConnectionResetError", "ConnectionResetError"]


def test_discard_partial_runs_before_notice():
    """先作废残文、再通知 UI，用户看到的提示才和屏幕状态一致。"""
    order = []

    def call():
        raise ConnectionResetError(54, "reset")

    with pytest.raises(ConnectionResetError):
        call_with_retry(
            call,
            max_retries=1,
            base_delay=1,
            random_fn=lambda: 0,
            discard_partial=lambda _e: order.append("discard"),
            on_retry=lambda *_: order.append("notice"),
            sleep=lambda _: None,
        )
    assert order == ["discard", "notice"]


def test_discard_partial_not_called_for_non_retryable_error():
    """不可重试的错误没有下一次尝试，不该白白收掉用户的输出行。"""
    discarded = []

    def call():
        raise HttpError(403, "account overdue")

    with pytest.raises(HttpError):
        call_with_retry(
            call,
            discard_partial=lambda exc: discarded.append(exc),
            sleep=lambda _: None,
        )
    assert discarded == []


# ── 状态码 vs 异常链：永久性错误不能被链上的网络异常翻案 ────────────────────────

def test_http_status_wins_over_network_cause_chain():
    """401 恰好包着 ConnectionResetError 时仍不可重试（链要停在带状态码的异常）。"""
    for status in (400, 401, 403, 404, 422):
        exc = HttpError(status, "bad request")
        exc.__cause__ = ConnectionResetError(54, "reset")
        assert classify_provider_error(exc).retryable is False, status


def test_implicit_exception_context_does_not_make_error_retryable():
    """Python 在 except 块里 raise 会自动挂 __context__，不能被它带偏。"""
    def raise_with_implicit_context():
        try:
            raise ConnectionResetError(54, "reset")
        except ConnectionResetError:
            raise HttpError(400, "bad request")

    with pytest.raises(HttpError) as caught:
        raise_with_implicit_context()
    assert classify_provider_error(caught.value).retryable is False


def test_retryable_status_still_wins_when_network_cause_present():
    """反过来：503 带着网络 cause 时，仍按 503 判为可重试。"""
    exc = HttpError(503, "unavailable")
    exc.__cause__ = ConnectionResetError(54, "reset")
    assert classify_provider_error(exc).retryable is True


def test_network_error_wrapped_by_status_wrapper_is_retryable_without_status():
    """SDK 的连接异常本身没有状态码，应当继续沿链判定为可重试。"""
    wrapped = RuntimeError("Connection error.")
    wrapped.__cause__ = ConnectionResetError(54, "reset")
    assert classify_provider_error(wrapped).retryable is True


# ── 状态码取值兜底：容器/怪对象不能把分类器打挂 ────────────────────────────────

def test_container_status_does_not_raise_type_error():
    """`response` 可能是 dict/list；旧写法 `status in {...}` 会抛 TypeError 盖掉真实错误。"""
    class WeirdError(RuntimeError):
        def __init__(self, response):
            super().__init__("boom")
            self.response = response

    for response in ({"error": {"code": "x"}}, ["a"], object()):
        decision = classify_provider_error(WeirdError(response))
        assert decision.retryable is False


def test_status_extracted_from_response_object():
    class Resp:
        status_code = 503

    class Err(RuntimeError):
        def __init__(self):
            super().__init__("x")
            self.response = Resp()

    assert classify_provider_error(Err()).retryable is True


def test_added_status_message_beats_definitive_status():
    """带 4xx 状态码时按状态码分类，不再被消息里的网络关键字顶掉。"""
    for status in (400, 401, 404):
        exc = HttpError(status, "Rate limit reached")
        assert classify_provider_error(exc).retryable is False, status


def test_midstream_failure_with_2xx_response_still_retryable():
    """流式中途断连的特征：HTTP 已 2xx，失败发生在读 body。

    不能因为异常上挂着 2xx 状态码就判死 —— 这正是"自己断掉"的形态。
    """
    for status in (200, 201, 206):
        exc = HttpError(status, "Server disconnected without sending a response")
        assert classify_provider_error(exc).retryable is True, status
        # 底层挂着网络异常时同理
        wrapped = HttpError(status, "x")
        wrapped.__cause__ = ConnectionResetError(54, "reset")
        assert classify_provider_error(wrapped).retryable is True, status


def test_2xx_status_does_not_block_network_cause_chain():
    """2xx 状态码不应中止异常链下探（它不代表服务端给出了错误回应）。"""
    exc = HttpError(200, "no keywords here")
    exc.__cause__ = ReadError("peer closed connection")
    assert classify_provider_error(exc).retryable is True


def test_ui_callback_failure_does_not_break_retry_loop():
    """UI 回调（清屏/打印）抛异常不能打断重试循环。

    回归点：回调裸调用时，一次 BrokenPipeError/终端异常会让整个自动重试中断，
    表现同样是"自动执行着就不往下走了"。
    """
    calls = []

    def provider():
        calls.append(1)
        if len(calls) < 3:
            raise ConnectionResetError(54, "reset")
        return "OK"

    def boom(*_a, **_k):
        raise BrokenPipeError("终端管道已断开")

    for kwargs in ({"on_retry": boom}, {"discard_partial": boom},
                   {"on_retry": boom, "discard_partial": boom}):
        calls.clear()
        out = call_with_retry(
            provider,
            max_retries=3,
            base_delay=1.5,
            random_fn=lambda: 0,
            sleep=lambda _: None,
            **kwargs,
        )
        assert out == "OK", f"回调 {kwargs} 抛异常打断了重试"
        assert len(calls) == 3


def test_failed_probe_does_not_extend_recovery_window():
    """恢复窗口内再次失败不能重置 _opened_at，否则永远等不到恢复。"""
    breaker = ProviderCircuitBreaker(failure_threshold=1, recovery_seconds=10.0)
    breaker.record_failure(ConnectionResetError(54, "reset"))
    opened_at = breaker._opened_at
    for _ in range(5):
        assert classify_provider_error(CircuitOpenError(1.0)).retryable is False
        breaker.record_failure(ConnectionResetError(54, "reset"))
    assert breaker._opened_at == opened_at


def test_interruptible_sleep_slices_without_float_drift():
    """30s 长退避按次数切片，不能因反复相减出现 29.999996 这种脏数字。"""
    cancel = CancelToken()
    slept = []

    _interruptible_sleep(30.0, cancel=cancel, sleep=slept.append)

    assert abs(sum(slept) - 30.0) < 1e-9
    assert len(slept) > 1, "应当分片等待，才来得及响应 Esc/Ctrl-C"


def test_interruptible_sleep_without_cancel_does_not_slice():
    """没有 cancel token 时不该引入额外调用（保持旧的单次 sleep 语义）。"""
    slept = []
    _interruptible_sleep(2.5, cancel=None, sleep=slept.append)
    assert slept == [2.5]
