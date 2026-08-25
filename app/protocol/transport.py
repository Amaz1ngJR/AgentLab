"""Protocol v1 事件的 JSONL 传输辅助函数。"""
from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from typing import TextIO

from app.protocol.envelopes import EventEnvelope


def encode_event(event: EventEnvelope) -> str:
    return json.dumps(event.to_dict(), ensure_ascii=False, separators=(",", ":"))


def write_event(stream: TextIO, event: EventEnvelope) -> None:
    stream.write(encode_event(event) + "\n")
    stream.flush()


def decode_events(lines: Iterable[str]) -> Iterator[EventEnvelope]:
    for line in lines:
        if not line.strip():
            continue
        value = json.loads(line)
        yield EventEnvelope(**value)
