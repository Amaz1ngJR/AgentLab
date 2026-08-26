"""Protocol v1 的 JSONL 编解码与初始化握手测试。"""
from io import StringIO
import json

import pytest

from app.protocol.envelopes import EventEnvelope
from app.protocol.handshake import ClientInfo
from app.protocol.transport import (
    InitializeRequest,
    JsonlProtocolServer,
    ProtocolTransportError,
    decode_events,
    encode_event,
    encode_request,
    write_event,
)


def test_jsonl_round_trip():
    event = EventEnvelope.create(
        sequence=1, thread_id="s1", turn_id="r1", kind="turn.started",
        payload={"text": "你好"},
    )
    encoded = encode_event(event)
    decoded = list(decode_events([encoded]))[0]
    assert decoded == event


def test_write_event_flushes_jsonl():
    stream = StringIO()
    event = EventEnvelope.create(sequence=1, thread_id="s1", kind="thread.created")
    write_event(stream, event)
    assert stream.getvalue().endswith("\n")
    assert list(decode_events(stream.getvalue().splitlines()))[0].kind == "thread.created"


def _client():
    return ClientInfo.create("pytest", "1.0", ["jsonl", "event_replay"])


def test_initialize_request_round_trip_and_encoding():
    request = InitializeRequest(client=_client(), protocol_version=1)
    encoded = encode_request(request, request_id="init-1")
    parsed = InitializeRequest.from_dict(json.loads(encoded))
    assert parsed == request
    assert '"id":"init-1"' in encoded


def test_jsonl_server_rejects_non_initialize_before_handshake():
    class Runtime:
        def initialize_client(self, client):
            raise AssertionError("must not be called")

    server = JsonlProtocolServer(Runtime())
    with pytest.raises(ProtocolTransportError, match="first request must be initialize"):
        server.handle_line('{"id": 1, "method": "events.subscribe"}')


def test_jsonl_server_handshakes_once_and_rejects_second_initialize():
    class Runtime:
        def initialize_client(self, client):
            return type("Result", (), {
                "protocol_version": 1,
                "client_id": "client-1",
                "accepted_capabilities": ("jsonl",),
                "server_capabilities": ("jsonl", "event_replay"),
            })()

    server = JsonlProtocolServer(Runtime())
    response = server.handle_line(
        '{"id":"1","method":"initialize","params":'
        '{"protocol_version":1,"client":{"name":"pytest","version":"1",'
        '"capabilities":["jsonl","unknown"]}}}'
    )
    assert response["result"]["client_id"] == "client-1"
    assert server.initialized
    with pytest.raises(ProtocolTransportError, match="already initialized"):
        server.handle_line('{"method":"initialize","params":{}}')
    with pytest.raises(ProtocolTransportError, match="unsupported method"):
        server.handle_line('{"method":"events.subscribe"}')


def test_jsonl_server_rejects_wrong_version_and_bad_json():
    class Runtime:
        def initialize_client(self, client):
            raise AssertionError("must not be called")

    server = JsonlProtocolServer(Runtime())
    with pytest.raises(ProtocolTransportError, match="unsupported protocol version"):
        server.handle_line(
            '{"method":"initialize","params":{"protocol_version":99,'
            '"client":{"name":"pytest","version":"1"}}}'
        )
    with pytest.raises(ProtocolTransportError, match="invalid JSON"):
        server.handle_line("not-json")
