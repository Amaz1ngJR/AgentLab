"""Protocol v1 的 JSONL 请求/响应与事件传输辅助函数。"""
from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any, TextIO

from app.protocol.envelopes import EventEnvelope
from app.protocol.handshake import ClientInfo, InitializeResult, negotiate_capabilities
from app.protocol.json_types import JsonValue, to_json_value


class ProtocolTransportError(ValueError):
    """JSONL transport 收到无法处理的请求。"""


@dataclass(frozen=True)
class InitializeRequest:
    """客户端建立 JSONL 连接时必须发送的第一条请求。"""

    client: ClientInfo
    protocol_version: int

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "InitializeRequest":
        if value.get("method") != "initialize":
            raise ProtocolTransportError("first request must be initialize")
        params = value.get("params")
        if not isinstance(params, dict):
            raise ProtocolTransportError("initialize.params must be an object")
        protocol_version = params.get("protocol_version")
        if not isinstance(protocol_version, int):
            raise ProtocolTransportError("initialize.protocol_version must be an integer")
        client_value = params.get("client")
        if not isinstance(client_value, dict):
            raise ProtocolTransportError("initialize.client must be an object")
        capabilities = client_value.get("capabilities", ())
        if isinstance(capabilities, str) or not isinstance(capabilities, (list, tuple)):
            raise ProtocolTransportError("initialize.client.capabilities must be an array")
        try:
            client = ClientInfo.create(
                str(client_value.get("name", "")),
                str(client_value.get("version", "")),
                (str(item) for item in capabilities),
            )
        except (TypeError, ValueError) as exc:
            raise ProtocolTransportError(str(exc)) from exc
        return cls(client=client, protocol_version=protocol_version)

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "method": "initialize",
            "params": {
                "protocol_version": self.protocol_version,
                "client": self.client.to_dict(),
            },
        }


def initialize_request(
    client: ClientInfo, *, protocol_version: int,
) -> InitializeRequest:
    return InitializeRequest(client=client, protocol_version=protocol_version)


def initialized_response(result: InitializeResult, *, request_id: Any = None) -> dict[str, JsonValue]:
    return {
        "id": to_json_value(request_id),
        "result": {
            "protocol_version": result.protocol_version,
            "client_id": result.client_id,
            "accepted_capabilities": list(result.accepted_capabilities),
            "server_capabilities": list(result.server_capabilities),
        },
    }


class JsonlProtocolServer:
    """最小进程内 JSONL 握手门面。

    Server 在 initialize 成功前拒绝所有其它请求；事件仍由 RuntimeService 的
    EventSubscription 消费，transport 只负责协议边界和响应编码。
    """

    def __init__(self, runtime):
        self.runtime = runtime
        self.client_id: str | None = None
        self.initialized = False

    def handle_line(self, line: str) -> dict[str, JsonValue]:
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ProtocolTransportError(f"invalid JSON: {exc.msg}") from exc
        if not isinstance(value, dict):
            raise ProtocolTransportError("request must be a JSON object")
        request_id = value.get("id")
        if not self.initialized:
            request = InitializeRequest.from_dict(value)
            if request.protocol_version != self.runtime_protocol_version:
                raise ProtocolTransportError(
                    f"unsupported protocol version: {request.protocol_version}"
                )
            result = self.runtime.initialize_client(request.client)
            self.client_id = result.client_id
            self.initialized = True
            return initialized_response(result, request_id=request_id)
        if value.get("method") == "initialize":
            raise ProtocolTransportError("client is already initialized")
        # Transport MVP 只承载握手；未知业务请求明确报错而不是静默丢弃。
        raise ProtocolTransportError(f"unsupported method: {value.get('method', '')}")

    @property
    def runtime_protocol_version(self) -> int:
        from app.protocol.envelopes import PROTOCOL_VERSION
        return PROTOCOL_VERSION


def encode_request(request: InitializeRequest, *, request_id: Any = None) -> str:
    value = request.to_dict()
    if request_id is not None:
        value["id"] = to_json_value(request_id)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


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
        if not isinstance(value, dict):
            raise ProtocolTransportError("event must be a JSON object")
        yield EventEnvelope(**value)
