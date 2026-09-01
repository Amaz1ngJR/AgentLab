"""跨 provider 切换时，把历史转换为各家都能接受的纯文本消息。"""
from __future__ import annotations

import json
from typing import Any


def _json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(value)


def _content_parts(content: Any) -> list[str]:
    if isinstance(content, str):
        return [content] if content else []
    if not isinstance(content, list):
        return [str(content)] if content is not None else []

    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            if block:
                parts.append(block)
            continue
        if not isinstance(block, dict):
            parts.append(str(block))
            continue
        block_type = block.get("type")
        if block_type in {"text", "input_text", "output_text"}:
            text = block.get("text")
            if text:
                parts.append(str(text))
        elif block_type in {"image", "image_url", "input_image"}:
            parts.append("[历史图片；切换模型时未重新发送图片数据]")
        elif block_type == "tool_use":
            parts.append(
                f"[历史工具调用 {block.get('name', '')}] "
                f"{_json(block.get('input') or {})}"
            )
        elif block_type == "tool_result":
            parts.append(
                f"[历史工具结果 {block.get('tool_use_id', '')}] "
                f"{block.get('content', '')}"
            )
    return parts


def _message_to_portable(message: dict[str, Any]) -> tuple[str, str] | None:
    item_type = message.get("type")
    if item_type == "function_call":
        return (
            "assistant",
            f"[历史工具调用 {message.get('name', '')}] "
            f"{message.get('arguments') or '{}'}",
        )
    if item_type == "function_call_output":
        return (
            "user",
            f"[历史工具结果 {message.get('call_id', '')}] "
            f"{message.get('output', '')}",
        )

    role = message.get("role")
    if role == "system" or role not in {"user", "assistant", "tool"}:
        return None
    portable_role = "user" if role == "tool" else role
    parts = _content_parts(message.get("content"))

    if role == "tool":
        prefix = f"[历史工具结果 {message.get('tool_call_id', '')}]"
        parts.insert(0, prefix)

    for call in message.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        function = call.get("function") or {}
        parts.append(
            f"[历史工具调用 {function.get('name', '')}] "
            f"{function.get('arguments') or '{}'}"
        )

    text = "\n".join(part for part in parts if part).strip()
    return (portable_role, text) if text else None


def make_history_portable(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    """保留对话语义，移除 OpenAI/Anthropic/Chat 专属的结构化消息格式。

    模型切换只允许发生在空闲会话中，因此工具调用已经结束。把已完成工具历史
    降级成带标签的文本，能避免新 provider 因 tool-call 配对协议不同而拒绝请求。
    """
    portable: list[dict[str, str]] = []
    for message in messages:
        converted = _message_to_portable(message)
        if converted is None:
            continue
        role, text = converted
        if portable and portable[-1]["role"] == role:
            portable[-1]["content"] += f"\n\n{text}"
        else:
            portable.append({"role": role, "content": text})
    return portable


__all__ = ["make_history_portable"]
