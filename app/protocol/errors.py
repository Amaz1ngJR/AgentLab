"""Runtime 结构化失败协议。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.protocol.json_types import JsonValue, to_json_value
from app.models.provider_retry import CircuitOpenError, classify_provider_error
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
        is_circuit = isinstance(exc, CircuitOpenError)
        decision = classify_provider_error(exc)
        is_transient_provider = not is_circuit and decision.retryable
        retry_after = exc.retry_after if is_circuit else None
        last_error = exc.last_error if is_circuit else None
        details: dict[str, JsonValue] = {"exception_type": type(exc).__name__}
        if last_error is not None:
            details["last_failure"] = redact(
                f"{type(last_error).__name__}: {last_error}"
            )
        return cls(
            code=(
                "provider_circuit_open" if is_circuit
                else "transient_provider_error" if is_transient_provider
                else "runtime_error"
            ),
            category="provider" if (is_circuit or is_transient_provider) else "runtime",
            message=redact(f"{type(exc).__name__}: {exc}"),
            retryable=is_circuit or is_transient_provider,
            retry_after_seconds=(
                float(retry_after) if is_circuit
                else decision.retry_after_seconds if is_transient_provider
                else None
            ),
            details=details,
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
