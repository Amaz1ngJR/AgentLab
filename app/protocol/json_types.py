"""Protocol JSON 值校验与规范化。"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import Enum
from pathlib import Path
from typing import Any, TypeAlias

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


class ProtocolSerializationError(TypeError):
    """公共协议遇到不可序列化 Python 对象时抛出。"""


def to_json_value(value: Any, *, path: str = "payload") -> JsonValue:
    """递归转换为 JSON 值；不允许异常对象和任意 SDK/Python 实例泄漏到协议。"""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return to_json_value(value.value, path=path)
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return to_json_value(value.to_dict(), path=path)
    if hasattr(value, "__dataclass_fields__"):
        from dataclasses import asdict
        return to_json_value(asdict(value), path=path)
    if isinstance(value, Mapping):
        result: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ProtocolSerializationError(f"{path} key must be str: {key!r}")
            result[key] = to_json_value(item, path=f"{path}.{key}")
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [to_json_value(item, path=f"{path}[{index}]") for index, item in enumerate(value)]
    raise ProtocolSerializationError(
        f"{path} contains non-JSON value {type(value).__name__}"
    )
