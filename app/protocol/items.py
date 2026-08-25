"""Turn 内 append-only Item 模型。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from app.protocol.envelopes import utc_now
from app.protocol.json_types import JsonValue, to_json_value

ItemStatus = Literal["started", "completed", "failed", "cancelled", "waiting"]


@dataclass(frozen=True)
class TurnItem:
    item_id: str
    thread_id: str
    turn_id: str
    sequence: int
    kind: str
    status: ItemStatus
    payload: dict[str, JsonValue] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not self.item_id or not self.thread_id or not self.turn_id:
            raise ValueError("item_id, thread_id and turn_id are required")
        if self.sequence <= 0:
            raise ValueError("sequence must be positive")
        normalized = to_json_value(self.payload)
        if not isinstance(normalized, dict):
            raise TypeError("payload must be a JSON object")
        object.__setattr__(self, "payload", normalized)

    @classmethod
    def create(
        cls,
        *,
        item_id: str,
        thread_id: str,
        turn_id: str,
        sequence: int,
        kind: str,
        status: ItemStatus,
        payload: dict[str, Any] | None = None,
    ) -> "TurnItem":
        return cls(
            item_id=item_id,
            thread_id=thread_id,
            turn_id=turn_id,
            sequence=sequence,
            kind=kind,
            status=status,
            payload=payload or {},
        )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "item_id": self.item_id,
            "thread_id": self.thread_id,
            "turn_id": self.turn_id,
            "sequence": self.sequence,
            "kind": self.kind,
            "status": self.status,
            "payload": self.payload,
            "created_at": self.created_at,
        }
