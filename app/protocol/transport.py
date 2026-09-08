"""Protocol v1 的 JSONL 请求/响应与事件传输辅助函数。"""
from __future__ import annotations

import json
import queue
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
        self._subscription = None

    def handle_line(self, line: str) -> dict[str, JsonValue] | None:
        """处理一条 JSONL 请求；事件通过 ``poll_event`` 单独读取。"""
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
        result = self._dispatch(value)
        if request_id is None:
            return None
        return {"id": to_json_value(request_id), "result": to_json_value(result)}

    def _dispatch(self, value: dict[str, Any]) -> dict[str, Any]:
        method = value.get("method")
        params = value.get("params") or {}
        if not isinstance(method, str) or not isinstance(params, dict):
            raise ProtocolTransportError("method and params must be valid")
        if method == "events.subscribe":
            if not hasattr(self.runtime, "open_event_subscription"):
                raise ProtocolTransportError("unsupported method: events.subscribe")
            self._subscription = self.runtime.open_event_subscription(
                self.client_id,
                thread_id=params.get("thread_id"),
                after_sequence=int(params.get("after_sequence", 0)),
                max_queue_size=int(params.get("max_queue_size", 256)),
            )
            return {"subscribed": True}
        if method in ("turn.start", "start_turn"):
            return {"turn_id": self.runtime.start_turn(
                str(params["thread_id"]), str(params.get("input", "")),
                images=params.get("images"), resume=bool(params.get("resume", False)),
                turn_id=params.get("turn_id"),
            )}
        if method in ("turn.interrupt", "interrupt_turn"):
            return {"accepted": self.runtime.interrupt_turn(str(params["turn_id"]))}
        if method in ("turn.resume", "resume_turn"):
            return {"turn_id": self.runtime.resume_turn(
                str(params["thread_id"]), str(params.get("input", "继续上一轮任务")),
                turn_id=params.get("turn_id"),
            )}
        if method in ("turn.steer", "steer_turn"):
            return {"accepted": self.runtime.steer_turn(
                str(params["turn_id"]), str(params["input"]),
            )}
        if method in ("request.answer", "answer_request"):
            return {"accepted": self.runtime.answer_request(
                str(params["request_id"]), params.get("response", params.get("decision")),
            )}
        if method == "events.poll":
            if self._subscription is None:
                raise ProtocolTransportError("events.subscribe is required")
            timeout = params.get("timeout")
            try:
                event = self._subscription.get(timeout=float(timeout) if timeout is not None else 0)
            except (StopIteration, queue.Empty):
                return {"event": None}
            return {"event": event.to_dict()}
        raise ProtocolTransportError(f"unsupported method: {method or ''}")

    def poll_event(self, timeout: float | None = None) -> EventEnvelope | None:
        """从当前客户端事件队列取一条事件，供 stdio writer 使用。"""
        if self._subscription is None:
            return None
        try:
            return self._subscription.get(timeout=timeout)
        except (StopIteration, queue.Empty):
            return None

    @property
    def runtime_protocol_version(self) -> int:
        from app.protocol.envelopes import PROTOCOL_VERSION
        return PROTOCOL_VERSION


class StdioJsonlServer(JsonlProtocolServer):
    """独立 stdio 传输适配器：请求从 stdin 读取，响应和事件写入 stdout。"""

    def serve(self, input_stream: TextIO, output_stream: TextIO) -> None:
        for line in input_stream:
            if not line.strip():
                continue
            try:
                response = self.handle_line(line)
                if response is not None:
                    output_stream.write(json.dumps(
                        response, ensure_ascii=False, separators=(",", ":"),
                    ) + "\n")
                    output_stream.flush()
                while True:
                    event = self.poll_event(timeout=0)
                    if event is None:
                        break
                    write_event(output_stream, event)
            except ProtocolTransportError as exc:
                output_stream.write(json.dumps(
                    {"id": None, "error": {"code": "invalid_request", "message": str(exc)}},
                    ensure_ascii=False, separators=(",", ":"),
                ) + "\n")
                output_stream.flush()

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
