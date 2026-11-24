"""Turn-scoped request events without request bodies or credentials."""
from contextlib import contextmanager
from contextvars import Context, ContextVar
from dataclasses import dataclass, field
from uuid import uuid4


@dataclass
class RequestGroup:
    call_id: str = field(default_factory=lambda: uuid4().hex)
    attempts: int = 0


_observer = ContextVar("model_request_observer", default=None)
_group: ContextVar[RequestGroup | None] = ContextVar("model_request_group", default=None)


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
