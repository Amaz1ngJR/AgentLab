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
    StdioJsonlServer,
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


def test_jsonl_server_dispatches_commands_and_polls_events():
    class Runtime:
        def __init__(self):
            self.calls = []
            self.subscription = type("Subscription", (), {
                "get": lambda self, timeout=0: EventEnvelope.create(
                    sequence=1, thread_id="s1", turn_id="t1", kind="turn.started",
                ),
            })()

        def initialize_client(self, client):
            return type("Result", (), {
                "protocol_version": 1, "client_id": "client-1",
                "accepted_capabilities": (), "server_capabilities": (),
            })()

        def open_event_subscription(self, *args, **kwargs):
            self.calls.append(("subscribe", args, kwargs))
            return self.subscription

        def start_turn(self, *args, **kwargs):
            self.calls.append(("start", args, kwargs))
            return "turn-1"

        def interrupt_turn(self, turn_id):
            self.calls.append(("interrupt", turn_id))
            return True

        def resume_turn(self, *args, **kwargs):
            self.calls.append(("resume", args, kwargs))
            return "turn-2"

        def steer_turn(self, *args):
            self.calls.append(("steer", args))
            return True

        def answer_request(self, *args):
            self.calls.append(("answer", args))
            return True

    runtime = Runtime()
    server = JsonlProtocolServer(runtime)
    server.handle_line(json.dumps({"id": 1, "method": "initialize", "params": {
        "protocol_version": 1, "client": {"name": "pytest", "version": "1"},
    }}))
    assert server.handle_line(json.dumps({"id": 2, "method": "events.subscribe", "params": {
        "thread_id": "s1", "after_sequence": 4,
    }}))["result"]["subscribed"] is True
    assert server.handle_line(json.dumps({"id": 3, "method": "turn.start", "params": {
        "thread_id": "s1", "input": "hello",
    }}))["result"]["turn_id"] == "turn-1"
    assert server.handle_line(json.dumps({"id": 4, "method": "events.poll", "params": {}}))["result"]["event"]["kind"] == "turn.started"
    assert server.handle_line(json.dumps({"id": 5, "method": "turn.interrupt", "params": {"turn_id": "turn-1"}}))["result"]["accepted"]
    assert server.handle_line(json.dumps({"method": "turn.steer", "params": {"turn_id": "turn-1", "input": "adjust"}})) is None
    assert [call[0] for call in runtime.calls] == ["subscribe", "start", "interrupt", "steer"]


def test_stdio_server_writes_responses_and_errors_as_jsonl():
    class Runtime:
        def initialize_client(self, client):
            return type("Result", (), {
                "protocol_version": 1, "client_id": "client-1",
                "accepted_capabilities": (), "server_capabilities": (),
            })()

    input_stream = StringIO(
        '{"id":"init","method":"initialize","params":{"protocol_version":1,'
        '"client":{"name":"pytest","version":"1"}}}\n'
        '{"id":"bad","method":"unknown"}\n'
    )
    output_stream = StringIO()
    StdioJsonlServer(Runtime()).serve(input_stream, output_stream)
    lines = [json.loads(line) for line in output_stream.getvalue().splitlines()]
    assert lines[0]["result"]["client_id"] == "client-1"
    assert lines[1]["error"]["code"] == "invalid_request"

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
