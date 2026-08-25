"""Thread 和 Turn 协议模型。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from app.protocol.envelopes import utc_now
from app.protocol.json_types import JsonValue

TurnStatus = Literal[
    "queued", "running", "waiting_approval", "waiting_user_input",
    "suspended", "completed", "failed", "cancelled",
]


@dataclass(frozen=True)
class ThreadRecord:
    thread_id: str
    agent_id: str
    title: str
    model_profile: str
    created_at: str = field(default_factory=utc_now)
    archived: bool = False

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "thread_id": self.thread_id,
            "agent_id": self.agent_id,
            "title": self.title,
            "model_profile": self.model_profile,
            "created_at": self.created_at,
            "archived": self.archived,
        }


@dataclass(frozen=True)
class TurnRecord:
    turn_id: str
    thread_id: str
    status: TurnStatus
    input_text: str
    started_at: str = field(default_factory=utc_now)
    finished_at: str | None = None
    failure_code: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "turn_id": self.turn_id,
            "thread_id": self.thread_id,
            "status": self.status,
            "input_text": self.input_text,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "failure_code": self.failure_code,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
        }
