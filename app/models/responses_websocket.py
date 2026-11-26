"""Connection-local continuations for the public stateless Responses API."""
from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager

from app.agent.cancel import close_on_cancel
from app.models.openai_adapter import ProviderStreamError

_TERMINAL = {"response.completed", "response.failed", "response.incomplete", "response.error", "error"}


class ResponsesWebSocket:
    def __init__(self):
        self.connection = None
        self.anchor = None

    def open(self, client, timeout):
        if self.connection is None:
            self.connection = client.responses.connect(
                max_retries=0, websocket_connection_options={"open_timeout": timeout, "close_timeout": 1},
            ).__enter__()

    def close(self):
        connection, self.connection = self.connection, None
        self.anchor = None
        if connection is not None:
            connection.close()

    @contextmanager
    def stream(self, params, *, cancel, timeout, encode_output, audit):
        connection = self.connection
        inputs = params["input"]
        settings = {key: value for key, value in params.items()
                    if key not in {"input", "stream", "extra_headers"}}
        payload = {"type": "response.create", **settings, "input": inputs}
        anchor, self.anchor = self.anchor, None
        if anchor is not None:
            response_id, old_settings, previous = anchor
            if settings == old_settings and len(inputs) > len(previous) and inputs[:len(previous)] == previous:
                payload.update(previous_response_id=response_id, input=inputs[len(previous):])

        if audit is not None:
            audit.update(transport="websocket", sent_input_items=len(payload["input"]),
                         continued_response_id=payload.get("previous_response_id"))

        terminal = None
        last_activity = time.monotonic()

        def events():
            nonlocal terminal, last_activity
            while True:
                try:
                    event = connection.recv()
                except Exception as exc:
                    if timed_out.is_set():
                        raise ProviderStreamError("Responses WebSocket timed out waiting for a stream event") from exc
                    raise ProviderStreamError("Responses WebSocket interrupted after submission") from exc
                last_activity = time.monotonic()
                if event.type in _TERMINAL:
                    terminal = event
                yield event
                if terminal is not None:
                    return

        finished = threading.Event()
        timed_out = threading.Event()

        def timeout_close():
            remaining = timeout
            while not finished.wait(remaining):
                remaining = last_activity + timeout - time.monotonic()
                if remaining > 0:
                    continue
                timed_out.set()
                try:
                    connection.close()
                except Exception:
                    pass
                return

        watchdog = threading.Thread(target=timeout_close, name="agentlab-websocket-timeout", daemon=True)
        watchdog.start()
        try:
            with close_on_cancel(cancel, connection):
                if cancel is not None:
                    cancel.raise_if_cancelled()
                try:
                    connection.send_raw(json.dumps(payload, ensure_ascii=False))
                except Exception as exc:
                    # A broken connection can leave admission unknown. Do not
                    # automatically resend the same billable input over HTTP.
                    raise ProviderStreamError("Responses WebSocket send interrupted") from exc
                yield events()
            response = getattr(terminal, "response", None)
            if (getattr(terminal, "type", None) == "response.completed"
                    and getattr(response, "status", None) == "completed"
                    and getattr(response, "id", None) and getattr(response, "output", None)):
                output = encode_output([item.model_dump(exclude_none=True) for item in response.output])
                if output and self.connection is connection:
                    self.anchor = (response.id, settings, inputs + output)
        finally:
            finished.set()
            if terminal is None or timed_out.is_set() or (cancel is not None and cancel.cancelled):
                try:
                    self.close()
                except Exception:
                    pass
