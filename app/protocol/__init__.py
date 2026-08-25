"""AgentLab Runtime Protocol v1。

协议层只包含可 JSON 序列化的数据模型，不依赖 AgentSession、CLI 或 Provider SDK。
"""

from app.protocol.envelopes import EventEnvelope, PROTOCOL_VERSION
from app.protocol.errors import RuntimeFailure
from app.protocol.items import TurnItem
from app.protocol.models import ThreadRecord, TurnRecord
from app.protocol.schema import protocol_schemas

__all__ = [
    "EventEnvelope",
    "PROTOCOL_VERSION",
    "RuntimeFailure",
    "ThreadRecord",
    "TurnItem",
    "TurnRecord",
]
