"""跨 Provider 的实时 token 估算与进度合并工具。"""
from __future__ import annotations

from typing import Callable


def _count_stream_chars(text: str) -> tuple[int, int]:
    """按流式文本累计估算 token；CJK 约 1 token/字，其余约 4 char/token。"""
    cjk = 0
    other = 0
    for char in text:
        code = ord(char)
        if (0x3000 <= code <= 0x9FFF) or (0xF900 <= code <= 0xFAFF):
            cjk += 1
        else:
            other += 1
    return cjk, other


def estimate_stream_tokens(text: str) -> int:
    """CJK 约 1 token/字，其余字符每四个估算一个 token。"""
    cjk, other = _count_stream_chars(text)
    return cjk + (other + 3) // 4


class StreamingTokenProgress:
    """维护输入 token、可见输出及推理输出的实时估算。

    Provider 通常只在请求结束时返回真实 usage。流式过程中根据文本增量估算并
    单调更新；最终 usage 到达后用真实值覆盖，确保 spinner 与最终统计一致。
    """

    def __init__(
        self,
        callback: Callable[[dict[str, int]], None] | None,
        input_tokens: int = 0,
    ):
        self.callback = callback
        self.input_tokens = max(0, int(input_tokens))
        self.output_tokens = 0
        self._counts = {"text": [0, 0], "reasoning": [0, 0]}
        self._last_emitted: tuple[int, int] | None = None

    def emit(self, *, force: bool = False, final: bool = False) -> None:
        current = (self.input_tokens, self.output_tokens)
        if self.callback and (force or final or current != self._last_emitted):
            payload = {
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
            }
            if final:
                payload["final"] = True
            self.callback(payload)
            self._last_emitted = current

    def add_text(self, delta: str) -> None:
        if not delta:
            return
        self._add_delta("text", delta)

    def add_reasoning(self, delta: str) -> None:
        if not delta:
            return
        self._add_delta("reasoning", delta)

    def set_usage(
        self,
        *,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        force: bool = False,
        final: bool = False,
    ) -> None:
        if input_tokens is not None:
            self.input_tokens = max(0, int(input_tokens))
        if output_tokens is not None:
            self.output_tokens = max(0, int(output_tokens))
        self.emit(force=force, final=final)

    def _add_delta(self, channel: str, delta: str) -> None:
        cjk, other = _count_stream_chars(delta)
        counts = self._counts[channel]
        counts[0] += cjk
        counts[1] += other
        # 两个通道分别取整，不能对每个分片取整或把正文与推理混为一条流。
        estimate = sum(c + (o + 3) // 4 for c, o in self._counts.values())
        if estimate > self.output_tokens:
            self.output_tokens = estimate
            self.emit()
