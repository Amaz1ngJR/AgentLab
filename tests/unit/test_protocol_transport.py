"""Protocol v1 的 JSONL 编解码测试。"""
from io import StringIO

from app.protocol.envelopes import EventEnvelope
from app.protocol.transport import decode_events, encode_event, write_event


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
