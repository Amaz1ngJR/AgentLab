"""Runtime 结构化失败协议。"""
from __future__ import annotations

from dataclasses import dataclass, field
import traceback
from typing import Any

from app.protocol.json_types import JsonValue, to_json_value
from app.util.redact import redact


@dataclass(frozen=True)
class RuntimeFailure:
    code: str
    category: str
    message: str
    retryable: bool = False
    user_action_required: bool = False
    retry_after_seconds: float | None = None
    details: dict[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        normalized = to_json_value(self.details)
        if not isinstance(normalized, dict):
            raise TypeError("details must be a JSON object")
        object.__setattr__(self, "details", normalized)

    @classmethod
    def from_exception(cls, exc: BaseException) -> "RuntimeFailure":
        # Keep locations, never frame locals or source lines that may contain credentials.
        frames = [
            {"file": redact(frame.f_code.co_filename), "line": lineno,
             "function": frame.f_code.co_name}
            for frame, lineno in traceback.walk_tb(exc.__traceback__)
        ][-24:]
        return cls(
            code="runtime_error",
            category="runtime",
            message=redact(f"{type(exc).__name__}: {exc}"),
            details={"exception_type": type(exc).__name__, "traceback": frames},
        )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "code": self.code,
            "category": self.category,
            "message": self.message,
            "retryable": self.retryable,
            "user_action_required": self.user_action_required,
            "retry_after_seconds": self.retry_after_seconds,
            "details": self.details,
        }
