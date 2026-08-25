"""Runtime 结构化失败协议。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.protocol.json_types import JsonValue, to_json_value


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
        return cls(
            code="runtime_error",
            category="runtime",
            message=f"{type(exc).__name__}: {exc}",
            details={"exception_type": type(exc).__name__},
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
