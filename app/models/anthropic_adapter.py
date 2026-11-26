"""Anthropic Claude API adapter。

认证优先级: auth_token (Bearer) > api_key (x-api-key)。
auth_token 用于自建代理 / Claude Code 网关。
"""
from __future__ import annotations
from functools import wraps

import json
from typing import Any, Optional

from app.agent.cancel import CancelToken, close_on_cancel
from app.config.schemas import LLMConfig
from app.attachments import materialize_image_block
from app.models.token_progress import StreamingTokenProgress
from app.models.stream_normalizer import StreamDeltaNormalizer
from app.models.openai_adapter import ProviderStreamError
from app.models.request_audit import audited_request, record_attempt, record_stream, record_usage
from app.models.protocol import (
    ModelResponse,
    ProgressCallback,
    TextDeltaCallback,
    ThinkingDeltaCallback,
    ToolCall,
    ToolResult,
)


def _content_blocks(content: Any) -> list[dict]:
    """把各 provider 的 content 表达统一成 Anthropic block 列表。

    入参可能是纯字符串、Anthropic block 列表、OpenAI Responses 的
    ``input_text``/``output_text`` 列表。图片保留原 block，交给
    ``_materialize_anthropic_messages`` 在发请求前物化。未知类型直接丢弃，
    避免把网关不认识的字段透传出去。
    """
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if not isinstance(content, list):
        return [{"type": "text", "text": str(content)}]

    blocks: list[dict] = []
    for item in content:
        if isinstance(item, str):
            if item:
                blocks.append({"type": "text", "text": item})
            continue
        if not isinstance(item, dict):
            continue
        block_type = item.get("type")
        if block_type in {"text", "input_text", "output_text"}:
            text = item.get("text")
            if isinstance(text, str) and text:
                blocks.append({"type": "text", "text": text})
        elif block_type in {"image", "tool_use", "tool_result"}:
            blocks.append(item)
    return blocks


def _text_of(content: Any) -> str:
    """把任意 content 压成纯文本，用于 tool_result 的 content 字段。"""
    return "\n".join(
        block["text"] for block in _content_blocks(content) if block.get("type") == "text"
    )


def _tool_use_block(call_id: str, name: str, arguments: Any) -> dict:
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments) if arguments.strip() else {}
        except json.JSONDecodeError:
            arguments = {}
    if not isinstance(arguments, dict):
        arguments = {}
    return {"type": "tool_use", "id": call_id, "name": name, "input": arguments}


def _normalize_anthropic_messages(
    messages: list[dict],
) -> tuple[Optional[str], list[dict]]:
    """把混合 provider 历史改写成 Anthropic Messages 能接受的形状。

    会话可能在 OpenAI Responses / ChatGPT OAuth / Ollama 之间迁移，恢复历史时
    里面会残留这些 provider 的私有条目：``function_call``、``function_call_output``、
    ``reasoning``、没有 role 的 item，以及 ``role="developer"`` 的追加指令。
    Anthropic 只认 ``user``/``assistant``/``system``，任何一个非法条目都会让整次
    请求 400（``Input should be 'user', 'assistant', 'system'``）——即使它排在很后面。
    因此这里在 provider 边界做一次归一化，而不是指望上游历史永远干净：

      - ``system``/``developer`` → 提升为顶层 system 参数
      - ``function_call`` → assistant 的 ``tool_use`` block
      - ``function_call_output`` → user 的 ``tool_result`` block
      - ``role="tool"`` → user 的 ``tool_result`` block
      - ``reasoning`` → 丢弃（推理过程本就不应回放进输入）
      - 其它无法识别的条目 → 丢弃，绝不把非法 role 发给网关

    相邻同角色消息会被合并，因为 Anthropic 要求 user/assistant 交替出现。
    """
    system_parts: list[str] = []
    normalized: list[dict] = []
    pending_calls: list[dict] = []

    def append(role: str, blocks: list[dict]) -> None:
        if not blocks:
            return
        if normalized and normalized[-1]["role"] == role:
            normalized[-1]["content"].extend(blocks)
        else:
            normalized.append({"role": role, "content": list(blocks)})

    def flush_calls() -> None:
        if pending_calls:
            append("assistant", pending_calls)
            pending_calls.clear()

    for message in messages:
        if not isinstance(message, dict):
            continue
        kind = message.get("type")
        if kind == "reasoning":
            continue
        if kind == "function_call":
            pending_calls.append(_tool_use_block(
                str(message.get("call_id") or message.get("id") or ""),
                str(message.get("name") or ""),
                message.get("arguments"),
            ))
            continue
        flush_calls()

        if kind == "function_call_output":
            append("user", [{
                "type": "tool_result",
                "tool_use_id": str(message.get("call_id") or ""),
                "content": _text_of(message.get("output")),
            }])
            continue
        if kind == "web_search_call":
            append("assistant", [{"type": "text", "text": "[历史联网搜索记录已省略]"}])
            continue

        role = message.get("role")
        if role in {"system", "developer"}:
            text = "\n".join(b["text"] for b in _content_blocks(message.get("content"))
                             if b.get("type") == "text")
            if text:
                system_parts.append(text)
            continue
        if role == "tool":
            append("user", [{
                "type": "tool_result",
                "tool_use_id": str(message.get("tool_call_id") or ""),
                "content": "\n".join(b["text"] for b in _content_blocks(message.get("content"))
                                     if b.get("type") == "text"),
            }])
            continue
        if role not in {"user", "assistant"}:
            # 没有 role 且不是已知 Responses item 的条目无法安全表达，丢弃。
            continue

        blocks = _content_blocks(message.get("content"))
        for call in message.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            function = call.get("function") or {}
            blocks.append(_tool_use_block(
                str(call.get("id") or ""),
                str(function.get("name") or ""),
                function.get("arguments"),
            ))
        append(role, blocks)

    flush_calls()
    return ("\n\n".join(system_parts) if system_parts else None), normalized


def _placeholder_block_for(delta_type: Any):
    """按增量类型构造占位块。

    补齐缺失的 content_block_start 时，占位块必须和后面要推的增量种类一致，
    否则 SDK 的 ``build_events`` 会因为 ``content_block.type`` 不匹配而把增量
    静默丢弃（例如给 input_json_delta 补 text 块，工具参数就整段丢失）。
    """
    from anthropic.types import TextBlock, ThinkingBlock, ToolUseBlock

    if delta_type == "input_json_delta":
        return ToolUseBlock(id="", type="tool_use", name="", input={})
    if delta_type == "thinking_delta" or delta_type == "signature_delta":
        return ThinkingBlock(type="thinking", thinking="", signature="")
    return TextBlock(type="text", text="")


def _iter_events_with_repaired_snapshot(stream, cancel=None):
    """遍历 Anthropic 原始流事件，并按 index 修复 SDK 的快照。

    SDK 的 ``MessageStream.__stream__`` 是「先 accumulate_event 再 yield」，
    而 ``accumulate_event`` 用 ``current_snapshot.content[event.index]``
    直接下标记账。网关若少发 ``content_block_start`` 或 index 跳号，就会
    在 SDK 内部抛 IndexError/AttributeError，整轮请求失败。

    这里改成自己消费 ``_raw_stream``：先把快照 content 按事件 index 补齐，
    再交给 SDK 的 ``accumulate_event`` 更新（保持 usage/stop_reason 等字段
    仍由 SDK 维护），最后把事件原样交给调用方。这样畸变序列也能继续跑完。
    """
    from anthropic.lib.streaming._messages import accumulate_event
    from anthropic import NOT_GIVEN

    raw_stream = getattr(stream, "_raw_stream", None)
    if raw_stream is None:
        # 结构不符预期时退回 SDK 自己的迭代器，行为与改动前一致。
        yield from stream
        return

    output_format = getattr(stream, "_MessageStream__output_format", NOT_GIVEN)
    # 直接读写私有字段：current_message_snapshot 属性在快照为空时会 assert。
    snapshot_attr = "_MessageStream__final_message_snapshot"
    for event in raw_stream:
        if cancel is not None:
            cancel.raise_if_cancelled()
        index = getattr(event, "index", None)
        snapshot = getattr(stream, snapshot_attr, None)
        etype = getattr(event, "type", None)
        # 部分网关把 message_start 的 content 发成 null 而不是空数组。
        # SDK 随后会直接对 content 调 append，所以必须先修成 list。
        if snapshot is not None and getattr(snapshot, "content", None) is None:
            snapshot.content = []
        # content_block_start 由 SDK 自己 append，不预先补位，否则会多出空块。
        if (etype != "content_block_start" and isinstance(index, int)
                and index >= 0 and snapshot is not None):
            content = getattr(snapshot, "content", None)
            if content is None:
                content = []
                snapshot.content = content
            if len(content) <= index:
                # 占位块要和后面的增量类型一致，否则 SDK 会静默丢弃该增量。
                delta = getattr(event, "delta", None)
                delta_type = getattr(delta, "type", None)
                if delta_type == "input_json_delta":
                    # 工具调用的 id/name 只在 content_block_start 里，丢了这个
                    # 事件就无法还原一次可执行的调用。补一个无名占位块只会让
                    # 参数静默消失、模型以为调用过，比直接失败更难排查，因此
                    # 交给重试层重新要一份完整的流。
                    raise ProviderStreamError(
                        "Anthropic 流式事件缺失 content_block_start（工具调用），"
                        "无法还原调用信息",
                        retryable=True,
                    )
                placeholder = _placeholder_block_for(delta_type)
                while len(content) <= index:
                    # 只有本次响应内承接增量，空占位块不会写回历史。
                    content.append(_placeholder_block_for(None) if len(content) < index
                                   else placeholder)
        try:
            updated = accumulate_event(
                event=event,
                current_snapshot=getattr(stream, snapshot_attr, None),
                output_format=output_format,
            )
        except (IndexError, AttributeError) as exc:
            raise ProviderStreamError(
                f"Anthropic 流式事件序列无法修复: {type(exc).__name__}: {exc}",
                retryable=True,
            ) from exc
        # 回写 SDK 的私有快照，否则 get_final_message()/current_message_snapshot
        # 读到的仍是旧值（__stream__ 原本负责这次赋值）。
        setattr(stream, snapshot_attr, updated)
        yield event


def _materialize_anthropic_messages(messages: list[dict]) -> list[dict]:
    """把内部 file 图片引用转换为 Anthropic Messages 的 base64 source。"""
    converted: list[dict] = []
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            converted.append(message)
            continue
        blocks: list[Any] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "image":
                media_type, data = materialize_image_block(block)
                blocks.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": data,
                    },
                })
            else:
                blocks.append(block)
        converted.append({**message, "content": blocks})
    return converted


def _temperature_compatible(method):
    @wraps(method)
    def invoke(self, *args, **kwargs):
        from anthropic import BadRequestError
        try:
            return method(self, *args, **kwargs)
        except BadRequestError as exc:
            message = str(exc).lower()
            if (getattr(self, "_omit_temperature", False) or "temperature" not in message
                    or not any(marker in message for marker in ("does not support non-default temperature", "temperature is not supported", "unsupported parameter: temperature"))):
                raise
            cancel = kwargs.get("cancel")
            if cancel is not None:
                cancel.raise_if_cancelled()
            self._omit_temperature = True
            return method(self, *args, **kwargs)
    return invoke


def _accumulated_message(stream, *, events_seen: int = 0):
    """取本次响应最终累积出的消息。

    真实 ``MessageStream`` 上 ``_iter_events_with_repaired_snapshot`` 已经
    自己维护了私有快照，直接取它；测试替身没有该字段，退回 SDK 的公开接口。

    用 isinstance 而不是 hasattr/getattr：MagicMock 会为任意属性凭空返回值，
    靠属性存在性判断会误判。

    快照为空说明整条流没有产出任何可用事件（网关空响应、或全是无法解析的
    畸变事件）。此时绝不能回退 ``get_final_message()`` —— 它内部会再 assert
    一次，抛出没有消息的 ``AssertionError``，在 CLI 上只显示
    "AssertionError: (异常无消息)"，完全无法定位。这里显式给出可读的、
    可重试的错误。
    """
    try:
        from anthropic.lib.streaming import MessageStream
    except ImportError:  # pragma: no cover - 理论上不会发生
        return stream.get_final_message()
    if isinstance(stream, MessageStream):
        snapshot = getattr(stream, "_MessageStream__final_message_snapshot", None)
        if snapshot is not None:
            return snapshot
        raise ProviderStreamError(
            f"Anthropic 流未产出可用事件（收到 {events_seen} 个事件但无消息快照）；"
            f"疑似网关空响应或事件序列整体损坏",
            retryable=True,
        )
    return stream.get_final_message()


class AnthropicAdapter:
    def __init__(self, cfg: LLMConfig):
        from anthropic import Anthropic

        if not (cfg.auth_token or cfg.api_key):
            raise RuntimeError("anthropic provider 需要 ANTHROPIC_AUTH_TOKEN 或 ANTHROPIC_API_KEY")

        kwargs: dict[str, Any] = {"timeout": cfg.timeout_seconds, "max_retries": 0}
        if cfg.auth_token:
            kwargs["auth_token"] = cfg.auth_token
        else:
            kwargs["api_key"] = cfg.api_key
        if cfg.base_url:
            kwargs["base_url"] = cfg.base_url
        self._client = Anthropic(**kwargs)
        self._cfg = cfg
        self._omit_temperature = cfg.model == "claude-fable-5-2"

    def fetch_context_size(self) -> int | None:
        """Use the Models API's declared input limit; never infer it from a model ID."""
        info = self._client.with_options(timeout=3, max_retries=0).models.retrieve(self.model)
        return getattr(info, "max_input_tokens", None)

    @property
    def model(self) -> str:
        return self._cfg.model

    @property
    def provider(self) -> str:
        return "anthropic"

    @staticmethod
    def _split_system(messages: list[dict]) -> tuple[Optional[str], list[dict]]:
        """归一化混合 provider 历史，并把 system 提升为 Anthropic 顶层参数。"""
        return _normalize_anthropic_messages(messages)

    @_temperature_compatible
    def chat(self, messages: list[dict], temperature: Optional[float] = None) -> str:
        system_text, converted = self._split_system(messages)
        converted = _materialize_anthropic_messages(converted)
        params: dict[str, Any] = {
            "model": self._cfg.model,
            "messages": converted,
            "temperature": self._cfg.temperature if temperature is None else temperature,
            "max_tokens": self._cfg.max_tokens,
        }
        if system_text:
            params["system"] = system_text
        if self._cfg.top_p is not None:
            params["top_p"] = self._cfg.top_p
        if self._omit_temperature:
            params.pop("temperature", None)
        msg = self._client.messages.create(**params)
        return "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")

    @_temperature_compatible
    @audited_request
    def create_message(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        system: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        on_progress: Optional[ProgressCallback] = None,
        on_text_delta: Optional[TextDeltaCallback] = None,
        on_thinking_delta: Optional[ThinkingDeltaCallback] = None,
        cancel: Optional[CancelToken] = None,
    ) -> ModelResponse:
        system_from_msgs, converted = self._split_system(messages)
        converted = _materialize_anthropic_messages(converted)
        system_text = system or system_from_msgs

        params: dict[str, Any] = {
            "model": self._cfg.model,
            "messages": converted,
            "temperature": self._cfg.temperature if temperature is None else temperature,
            "max_tokens": max_tokens or self._cfg.max_tokens,
        }
        if system_text:
            params["system"] = ([{"type": "text", "text": system_text,
                                  "cache_control": {"type": "ephemeral"}}]
                                if self._cfg.prompt_cache_enabled else system_text)
        if self._cfg.top_p is not None:
            params["top_p"] = self._cfg.top_p
        if tools:
            params["tools"] = tools
        if self._omit_temperature:
            params.pop("temperature", None)

        in_tokens = 0
        out_tokens = 0
        events_seen = 0
        progress = StreamingTokenProgress(on_progress)
        normalizer = StreamDeltaNormalizer()
        progress.emit(force=True)

        with self._client.messages.stream(**params) as stream:
            record_stream(stream)
            with close_on_cancel(cancel, stream):
                # 不再用 ``for event in stream``：SDK 的 MessageStream.__stream__
                # 在 yield 之前就调用 accumulate_event()，而那里直接用
                # ``snapshot.content[event.index]`` 取块
                # （见 anthropic/lib/streaming/_messages.py 的
                #  content_block_delta 与 content_block_stop 两处）。
                # 自建代理偶尔会缺 content_block_start 或让 index 跳号，
                # 于是整轮请求以 IndexError 结束 —— 属于传输层畸变，
                # 重试一次通常就好，不该让 20+ 分钟的执行前功尽弃。
                #
                # 这里直接遍历原始事件流并自己累加快照，遇到不完整的序列
                # 按 index 补齐占位块，既能继续解析，也不会再越界。
                for event in _iter_events_with_repaired_snapshot(stream, cancel):
                    events_seen += 1
                    etype = getattr(event, "type", None)
                    if etype == "message_start":
                        # content=null 已由 _iter_events_with_repaired_snapshot 修好。
                        usage = getattr(event.message, "usage", None)
                        record_usage(usage, provider="anthropic", response=event.message)
                        if usage:
                            in_tokens = getattr(usage, "input_tokens", 0) or 0
                            out_tokens = getattr(usage, "output_tokens", 0) or 0
                        progress.set_usage(
                            input_tokens=in_tokens,
                            output_tokens=out_tokens,
                            force=True,
                        )
                    elif etype == "content_block_delta":
                        delta = getattr(event, "delta", None)
                        if delta is not None and getattr(delta, "type", None) == "text_delta":
                            text_chunk = normalizer.normalize(
                                "content", getattr(delta, "text", "") or ""
                            )
                            if text_chunk:
                                if on_text_delta:
                                    on_text_delta(text_chunk)
                                progress.add_text(text_chunk)
                    elif etype == "message_delta":
                        usage = getattr(event, "usage", None)
                        if usage:
                            v = getattr(usage, "output_tokens", None)
                            if v is not None:
                                out_tokens = v
                                record_attempt(output_tokens=v)
                                progress.set_usage(output_tokens=v)

                if cancel is not None:
                    cancel.raise_if_cancelled()
                # 事件由 _iter_events_with_repaired_snapshot 自行累加，SDK 的
                # get_final_message() 依赖它自己 __stream__ 里写入的私有快照，
                # 这里改为直接取那条已被我们更新过的快照。测试替身（MagicMock）
                # 会凭空造出任意属性，因此只在确认是真实 MessageStream 时走快照。
                message = _accumulated_message(stream, events_seen=events_seen)

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        raw_blocks: list[dict] = []
        for block in message.content:
            btype = getattr(block, "type", None)
            if btype == "text":
                # 补位用的占位块没有内容，不能写进历史，否则回放时会出现空文本块。
                if not (getattr(block, "text", "") or ""):
                    continue
                text_parts.append(block.text)
                raw_blocks.append({"type": "text", "text": block.text})
            elif btype == "tool_use":
                # 正常路径下 block_start 一定带 id/name；这里只是防御：
                # 没有它们就不是一次可执行的调用。
                if not getattr(block, "name", "") or not getattr(block, "id", ""):
                    continue
                tool_calls.append(ToolCall(
                    id=block.id,
                    name=block.name,
                    arguments=dict(block.input or {}),
                ))
                raw_blocks.append({
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": dict(block.input or {}),
                })

        usage: dict[str, int] = {}
        if getattr(message, "usage", None):
            usage = {
                "input_tokens":  getattr(message.usage, "input_tokens",  in_tokens) or in_tokens,
                "output_tokens": getattr(message.usage, "output_tokens", out_tokens) or out_tokens,
            }
        else:
            usage = {"input_tokens": in_tokens, "output_tokens": out_tokens}

        from app.models.cache_usage import cache_usage
        record_usage(getattr(message, "usage", None), provider="anthropic", response=message)
        usage.update(cache_usage(getattr(message, "usage", None), provider="anthropic"))
        # 用最终 usage 再发一次进度，确保 spinner 显示的数字与 [stats] 一致
        if on_progress:
            progress.set_usage(
                input_tokens=usage["input_tokens"],
                output_tokens=usage["output_tokens"],
                force=True,
                final=True,
            )

        # provider_payload 是 list[dict],由 Runtime 调 messages.extend(...) 追加。
        # Anthropic 要求把这一轮所有 content block(text + tool_use)合在
        # 同一条 assistant 消息里完整回放,否则下一轮 messages.create 会报
        # "tool_use without matching tool_result"。所以这里只产生一个 dict,
        # 但仍包成单元素 list 以匹配统一接口(OpenAI Responses 那边可能会有多条)。
        return ModelResponse(
            text="".join(text_parts),
            tool_calls=tool_calls,
            usage=usage,
            provider_payload=[{"role": "assistant", "content": raw_blocks}],
            finish_reason=getattr(message, "stop_reason", None),
            # API 实际使用的模型 ID。Anthropic 官方 API 会原样返回请求里写的
            # model;但代理(如 ai.vdian.net)有可能映射到别的真实模型,这里把
            # 它如实暴露给上层,Runtime / CLI 在不一致时可以警告
            actual_model=getattr(message, "model", None),
        )

    @staticmethod
    def format_tool_results(results: list[ToolResult]) -> list[dict]:
        """把工具执行结果转成 Anthropic 格式,作为 messages 的下一轮输入项返回。

        Anthropic Messages API 规定:
          - 工具结果必须放在 role=user 的消息里
          - content 是 tool_result 块列表,每个块的 tool_use_id 必须和上一轮
            assistant 消息里某个 tool_use 块的 id 对应
          - 一个 user 消息可以包含多个 tool_result 块(对应同一轮多个工具调用)

        所以即便有 N 个 ToolResult,也只产生一条 user 消息(里面 N 个 block)。
        """
        blocks = [
            {
                "type": "tool_result",
                "tool_use_id": r.tool_call_id,
                "content": r.output,
                "is_error": r.is_error,
            }
            for r in results
        ]
        return [{"role": "user", "content": blocks}]
