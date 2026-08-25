"""版本化 Runtime 事件信封。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.protocol.json_types import JsonValue, to_json_value

PROTOCOL_VERSION = 1


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class EventEnvelope:
    schema_version: int
    sequence: int
    thread_id: str
    turn_id: str | None
    item_id: str | None
    kind: str
    timestamp: str
    payload: dict[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.schema_version <= 0:
            raise ValueError("schema_version must be positive")
        if self.sequence <= 0:
            raise ValueError("sequence must be positive")
        if not self.thread_id:
            raise ValueError("thread_id is required")
        if not self.kind or "." not in self.kind:
            raise ValueError("kind must be a namespaced value such as turn.started")
        normalized = to_json_value(self.payload)
        if not isinstance(normalized, dict):
            raise TypeError("payload must be a JSON object")
        object.__setattr__(self, "payload", normalized)

    @classmethod
    def create(
        cls,
        *,
        sequence: int,
        thread_id: str,
        kind: str,
        turn_id: str | None = None,
        item_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> "EventEnvelope":
        return cls(
            schema_version=PROTOCOL_VERSION,
            sequence=sequence,
            thread_id=thread_id,
            turn_id=turn_id,
            item_id=item_id,
            kind=kind,
            timestamp=utc_now(),
            payload=payload or {},
        )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "sequence": self.sequence,
            "thread_id": self.thread_id,
            "turn_id": self.turn_id,
            "item_id": self.item_id,
            "kind": self.kind,
            "timestamp": self.timestamp,
            "payload": self.payload,
        }
