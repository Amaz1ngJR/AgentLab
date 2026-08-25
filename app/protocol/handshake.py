"""Runtime Protocol 初始化握手和客户端能力声明。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from app.protocol.envelopes import PROTOCOL_VERSION
from app.protocol.json_types import JsonValue

SUPPORTED_CAPABILITIES = frozenset({
    "event_replay",
    "turn_items",
    "approvals",
    "images",
    "jsonl",
})


@dataclass(frozen=True)
class ClientInfo:
    name: str
    version: str
    capabilities: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def create(
        cls, name: str, version: str, capabilities: Iterable[str] = (),
    ) -> "ClientInfo":
        if not name.strip() or not version.strip():
            raise ValueError("client name and version are required")
        return cls(name.strip(), version.strip(), tuple(sorted(set(capabilities))))

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "name": self.name,
            "version": self.version,
            "capabilities": list(self.capabilities),
        }


@dataclass(frozen=True)
class InitializeResult:
    protocol_version: int
    client_id: str
    accepted_capabilities: tuple[str, ...]
    server_capabilities: tuple[str, ...]

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "protocol_version": self.protocol_version,
            "client_id": self.client_id,
            "accepted_capabilities": list(self.accepted_capabilities),
            "server_capabilities": list(self.server_capabilities),
        }


def negotiate_capabilities(client: ClientInfo) -> tuple[str, ...]:
    return tuple(sorted(set(client.capabilities) & SUPPORTED_CAPABILITIES))
