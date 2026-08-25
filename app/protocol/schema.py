"""P0 Runtime Protocol v1 的显式 JSON Schema 草案。

运行时校验仍由 dataclass 完成；该 schema 供未来 FastAPI/App Server 生成 OpenAPI、
TypeScript 类型和前端契约测试使用。
"""
from __future__ import annotations

from typing import Any

PROTOCOL_SCHEMA_VERSION = 1

EVENT_ENVELOPE_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "AgentLab EventEnvelope",
    "type": "object",
    "required": [
        "schema_version", "sequence", "thread_id", "turn_id", "item_id",
        "kind", "timestamp", "payload",
    ],
    "properties": {
        "schema_version": {"const": PROTOCOL_SCHEMA_VERSION},
        "sequence": {"type": "integer", "minimum": 1},
        "thread_id": {"type": "string", "minLength": 1},
        "turn_id": {"type": ["string", "null"]},
        "item_id": {"type": ["string", "null"]},
        "kind": {"type": "string", "pattern": "^[a-z][a-z0-9_.-]*\\.[a-z][a-z0-9_.-]*$"},
        "timestamp": {"type": "string", "minLength": 1},
        "payload": {"type": "object"},
    },
    "additionalProperties": False,
}

TURN_ITEM_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "AgentLab TurnItem",
    "type": "object",
    "required": [
        "item_id", "thread_id", "turn_id", "sequence", "kind", "status",
        "payload", "created_at",
    ],
    "properties": {
        "item_id": {"type": "string", "minLength": 1},
        "thread_id": {"type": "string", "minLength": 1},
        "turn_id": {"type": "string", "minLength": 1},
        "sequence": {"type": "integer", "minimum": 1},
        "kind": {"type": "string", "minLength": 1},
        "status": {"enum": ["started", "completed", "failed", "cancelled", "waiting"]},
        "payload": {"type": "object"},
        "created_at": {"type": "string", "minLength": 1},
    },
    "additionalProperties": False,
}


def protocol_schemas() -> dict[str, dict[str, Any]]:
    return {
        "EventEnvelope": EVENT_ENVELOPE_SCHEMA,
        "TurnItem": TURN_ITEM_SCHEMA,
    }
