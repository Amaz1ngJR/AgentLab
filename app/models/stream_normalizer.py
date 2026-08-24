"""规范化 Provider 流事件：同时兼容真正 delta 与累计 snapshot。

部分兼容网关把字段命名为 ``delta``，实际每次返回“截至目前的完整文本”。若调用方
直接 append，就会出现 A、A+B、A+B+C 三段重复输出。本模块只在连续长 chunk 呈
严格前缀扩展时判定为 snapshot；短 chunk（包括连续相同字符）始终按真实 delta
保留，避免破坏 JSON、代码中的 ``pp``、反引号等合法重复内容。
"""
from __future__ import annotations

_MIN_SNAPSHOT_PREFIX_CHARS = 8


class StreamDeltaNormalizer:
    """按通道记录累计内容，将累计快照规范化为真正增量。"""

    def __init__(self) -> None:
        self._emitted: dict[str, str] = {}
        self._last_raw: dict[str, str] = {}

    def normalize(self, channel: str, raw: str | None) -> str:
        if not raw:
            return ""
        raw = str(raw)
        emitted = self._emitted.get(channel, "")
        previous_raw = self._last_raw.get(channel, "")
        self._last_raw[channel] = raw

        # 累计快照：当前长 payload 以前一个 payload 开头，只输出新增后缀。
        # 长度门槛用于消除不可判定歧义：真实字符流中的 "p", "p" 必须保留为 pp。
        if (
            len(previous_raw) >= _MIN_SNAPSHOT_PREFIX_CHARS
            and len(raw) > len(previous_raw)
            and raw.startswith(previous_raw)
        ):
            delta = raw[len(previous_raw):]
            self._emitted[channel] = emitted + delta
            return delta

        # 网关偶尔会重放同一个较长事件；短的相同 delta 仍视为合法重复字符。
        if raw == previous_raw and len(raw) >= _MIN_SNAPSHOT_PREFIX_CHARS:
            return ""

        # 真正增量：直接追加。不要做“见过相同行就删除”的语义去重，因为模型可能
        # 合法重复文本（代码、日志、列表项）。
        self._emitted[channel] = emitted + raw
        return raw

    def emitted(self, channel: str) -> str:
        return self._emitted.get(channel, "")
