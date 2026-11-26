"""Turn-scoped request events without request bodies or credentials."""
from contextlib import contextmanager
from contextvars import Context, ContextVar
from dataclasses import dataclass, field
from functools import wraps
import hashlib
import json
from threading import Lock
from uuid import uuid4


@dataclass
class RequestGroup:
    call_id: str = field(default_factory=lambda: uuid4().hex)
    attempts: int = 0


_observer = ContextVar("model_request_observer", default=None)
_group: ContextVar[RequestGroup | None] = ContextVar("model_request_group", default=None)
_attempt: ContextVar[dict | None] = ContextVar("model_request_attempt", default=None)


@contextmanager
def observe_model_requests(observer):
    token = _observer.set(observer)
    try:
        yield
    finally:
        _observer.reset(token)


def copy_request_observer() -> Context:
    context = Context()
    context.run(_observer.set, _observer.get())
    return context


def emit_request_event(kind, payload):
    observer = _observer.get()
    if observer is not None:
        try:
            observer(kind, payload)
        except Exception:
            # A diagnostics sink must not change provider retry or tool behavior.
            pass


@contextmanager
def request_group(on_retry, *, enabled):
    if not enabled or _observer.get() is None:
        yield on_retry
        return
    group = RequestGroup()
    token = _group.set(group)

    def report_retry(attempt, delay, error):
        emit_request_event("model.request.retry", {
            "call_id": group.call_id, "after_attempt": group.attempts,
            "delay_seconds": delay,
        })
        if on_retry is not None:
            on_retry(attempt, delay, error)

    try:
        yield report_retry
    finally:
        _group.reset(token)


def start_request():
    if _observer.get() is None:
        return None
    group = _group.get() or RequestGroup()
    group.attempts += 1
    return {"call_id": group.call_id, "attempt": group.attempts}


def record_attempt(**fields):
    attempt = _attempt.get()
    if attempt is not None:
        attempt.update({key: value for key, value in fields.items()
                        if isinstance(value, (str, int))})


class ResponsesRequestPrefix:
    """Compare sanitized outgoing requests while retaining only per-item hashes."""

    def __init__(self):
        self._lock = Lock()
        self._previous = ()

    def describe(self, params):
        def encode(value):
            return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

        inputs = params["input"]
        fixed_count = next((i for i, item in enumerate(inputs)
                            if item.get("role") not in {"developer", "system"}), len(inputs))
        fixed = {key: params[key] for key in (
            "model", "instructions", "tools", "reasoning", "tool_choice", "prompt_cache_key",
        ) if key in params}
        fixed["input"] = inputs[:fixed_count]
        encoded = [encode(item) for item in inputs]
        digests = tuple(hashlib.sha256(item).digest() for item in encoded)
        with self._lock:
            previous, self._previous = self._previous, digests
        common = next((i for i, (before, after) in enumerate(zip(previous, digests))
                       if before != after), min(len(previous), len(digests)))
        return {
            "input_items": len(inputs),
            "input_bytes": 2 + sum(map(len, encoded)) + max(0, len(encoded) - 1),
            "fixed_prefix_items": fixed_count,
            "fixed_prefix_sha256": hashlib.sha256(encode(fixed)).hexdigest(),
            "previous_input_items": len(previous) if previous else None,
            "common_input_prefix_items": common if previous else None,
        }

    def record(self, params):
        attempt = _attempt.get()
        if attempt is not None:
            attempt.update(self.describe(params))


def audited_request(method):
    """Audit a single adapter invocation, including recursive gateway fallbacks."""
    @wraps(method)
    def invoke(adapter, *args, **kwargs):
        audit = start_request()
        if audit is None:
            return method(adapter, *args, **kwargs)
        cfg = adapter._cfg
        audit.update(provider=cfg.provider, model=adapter.model,
                     reasoning_effort=kwargs.get("reasoning_effort_override") or cfg.reasoning_effort,
                     status="failed", request_id=None, response_id=None,
                     input_tokens=None, cached_tokens=None, output_tokens=None)
        token = _attempt.set(audit)
        try:
            response = method(adapter, *args, **kwargs)
            if audit["status"] == "failed":
                record_attempt(status="completed", actual_model=response.actual_model)
            return response
        except Exception as error:
            from app.agent.cancel import Cancelled
            record_attempt(status="cancelled" if isinstance(error, Cancelled) else "failed",
                           request_id=getattr(error, "request_id", None),
                           http_status=getattr(error, "status_code", None),
                           error_code=getattr(error, "code", None) or type(error).__name__)
            raise
        finally:
            _attempt.reset(token)
            emit_request_event("model.request.finished", audit)
    return invoke


def record_usage(usage, *, provider, response=None):
    if _attempt.get() is None:
        return
    from app.models.cache_usage import cache_usage
    cache = cache_usage(usage, provider=provider)
    compatible = provider == "compatible"
    record_attempt(
        response_id=getattr(response, "id", None),
        actual_model=getattr(response, "model", None),
        input_tokens=getattr(usage, "prompt_tokens" if compatible else "input_tokens", None),
        output_tokens=getattr(usage, "completion_tokens" if compatible else "output_tokens", None),
        cached_tokens=cache.get("cache_read_input_tokens"),
        cache_creation_input_tokens=cache.get("cache_creation_input_tokens"),
    )


def record_stream(stream):
    if _attempt.get() is None:
        return
    raw = getattr(stream, "_raw_stream", None)
    response = getattr(stream, "response", None) or getattr(raw, "response", None)
    headers = getattr(response, "headers", None) or {}
    record_attempt(request_id=getattr(stream, "request_id", None) or headers.get("x-request-id"),
                   http_status=getattr(response, "status_code", None))
