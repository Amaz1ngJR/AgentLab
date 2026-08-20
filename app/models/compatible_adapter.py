"""OpenAI-compatible adapter —— 覆盖 Ollama / LM Studio / vLLM 等本地端点。

P0 关键：实现 create_message() 使 Ollama 工具循环闭环。

OpenAI 工具循环与 Anthropic 的差异：
  - 工具调用在 response.choices[0].message.tool_calls 里（ChoiceDelta 对象列表）
  - provider_payload 存 OpenAI 格式的 assistant message dict，下轮原样放入 messages
  - 工具结果回传格式：{"role": "tool", "tool_call_id": ..., "content": ...}
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

from app.config.schemas import LLMConfig
from app.attachments import image_block_to_data_url
from app.models.protocol import (
    ModelResponse,
    ProgressCallback,
    TextDeltaCallback,
    ThinkingDeltaCallback,
    ToolCall,
    ToolResult,
)


def _content_to_text(content: Any) -> str:
    """将 Responses/Anthropic 的结构化 content 转为 Chat Completions 文本。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)

    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


def _content_to_chat_content(content: Any) -> str | list[dict[str, Any]]:
    """转换多模态 content；有图片时保留 Chat Completions block 结构。"""
    if not isinstance(content, list):
        return _content_to_text(content)
    blocks: list[dict[str, Any]] = []
    has_image = False
    for item in content:
        if not isinstance(item, dict):
            if isinstance(item, str):
                blocks.append({"type": "text", "text": item})
            continue
        item_type = item.get("type")
        if item_type == "text":
            blocks.append({"type": "text", "text": item.get("text", "")})
        elif item_type == "image":
            has_image = True
            blocks.append({
                "type": "image_url",
                "image_url": {"url": image_block_to_data_url(item)},
            })
    return blocks if has_image else _content_to_text(content)


def _normalize_chat_messages(messages: list[dict]) -> list[dict[str, Any]]:
    """把混合 provider 历史转换为 Ollama/OpenAI Chat Completions 格式。"""
    normalized: list[dict[str, Any]] = []
    pending_calls: list[dict[str, Any]] = []

    for message in messages:
        msg_type = message.get("type")
        if msg_type == "function_call":
            arguments = message.get("arguments") or "{}"
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments, ensure_ascii=False)
            pending_calls.append({
                "id": message.get("call_id") or message.get("id") or "",
                "type": "function",
                "function": {"name": message.get("name") or "", "arguments": arguments},
            })
            continue

        if pending_calls:
            normalized.append({"role": "assistant", "content": "", "tool_calls": pending_calls})
            pending_calls = []

        if msg_type == "function_call_output":
            normalized.append({
                "role": "tool",
                "tool_call_id": message.get("call_id") or "",
                "content": _content_to_text(message.get("output")),
            })
            continue

        role = message.get("role")
        if role not in {"system", "user", "assistant", "tool"}:
            continue
        converted: dict[str, Any] = {
            "role": role,
            "content": (
                _content_to_chat_content(message.get("content"))
                if role == "user"
                else _content_to_text(message.get("content"))
            ),
        }
        # 只保留 Chat Completions 支持的字段，避免 Responses API 的 id、status、
        # phase 等字段被透传给 Ollama 后触发 invalid message format。
        if role == "assistant" and message.get("tool_calls"):
            converted["tool_calls"] = message["tool_calls"]
        if role == "tool" and message.get("tool_call_id") is not None:
            converted["tool_call_id"] = message["tool_call_id"]
        if message.get("name") is not None:
            converted["name"] = message["name"]
        normalized.append(converted)

    if pending_calls:
        normalized.append({"role": "assistant", "content": "", "tool_calls": pending_calls})
    return normalized


class OpenAICompatibleAdapter:
    def __init__(self, cfg: LLMConfig):
        from openai import OpenAI

        self._cfg = cfg
        self._client = OpenAI(
            base_url=cfg.base_url,
            api_key=cfg.api_key or "ollama",
            timeout=cfg.timeout_seconds,
        )

    @property
    def model(self) -> str:
        return self._cfg.model

    @property
    def provider(self) -> str:
        return "openai_compatible"

    def _base_params(self, messages: list[dict], temperature: Optional[float]) -> dict[str, Any]:
        # 会话可能在 OpenAI Responses 与 Ollama 之间切换。Responses 历史中的
        # function_call、function_call_output 和 content 列表不能直接发送给 Chat
        # Completions；这里统一转换为 Ollama 接受的 assistant/tool/纯文本格式。
        normalized_messages = _normalize_chat_messages(messages)

        params: dict[str, Any] = {
            "model": self._cfg.model,
            "messages": normalized_messages,
            "temperature": self._cfg.temperature if temperature is None else temperature,
        }
        if self._cfg.top_p is not None:
            params["top_p"] = self._cfg.top_p
        return params

    def chat(self, messages: list[dict], temperature: Optional[float] = None) -> str:
        resp = self._client.chat.completions.create(**self._base_params(messages, temperature))
        return resp.choices[0].message.content or ""

    def create_message(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        system: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: int = 4096,
        on_progress: Optional[ProgressCallback] = None,
        on_text_delta: Optional[TextDeltaCallback] = None,
        on_thinking_delta: Optional[ThinkingDeltaCallback] = None,
    ) -> ModelResponse:
        # OpenAI-compatible 接口把 system 放在 messages 里（role=system）
        all_messages = list(messages)
        if system and not any(m.get("role") == "system" for m in all_messages):
            all_messages = [{"role": "system", "content": system}] + all_messages

        params = self._base_params(all_messages, temperature)
        params["max_tokens"] = max_tokens
        params["stream"] = True
        params["stream_options"] = {"include_usage": True}
        if tools:
            params["tools"] = [_to_openai_tool(t) for t in tools]
            params["tool_choice"] = "auto"

        # 深度思考模型(Qwen3 / DeepSeek-R1 等)需要显式开启,服务端才会在流里
        # 额外吐出 delta.reasoning_content。通过 extra_body 透传,非思考模型不带此参,
        # 避免给 Ollama / 硅基流动等端点塞它们不认识的字段。
        if getattr(self._cfg, "enable_thinking", False):
            params["extra_body"] = {"enable_thinking": True}

        # 输入 token 估算（在真值到达前用），约 3 字符一个 token
        in_tokens_est = max(1, sum(len(str(m.get("content", ""))) for m in all_messages) // 3)
        in_tokens = in_tokens_est
        out_tokens = 0
        out_chars = 0

        def emit_progress() -> None:
            if on_progress:
                on_progress({"input_tokens": in_tokens, "output_tokens": out_tokens})

        emit_progress()

        text_parts: list[str] = []
        reasoning_parts: list[str] = []  # 深度思考模型的推理过程(reasoning_content)
        # tool_calls 流式分片到达：index -> {"id", "name", "args_str"}
        tool_calls_buf: dict[int, dict[str, str]] = {}
        finish_reason: Optional[str] = None
        # 服务端实际使用的模型 ID。每个 chunk 都会带 model 字段,但通常都一样,
        # 任意一个非空值即可记下来(OpenAI / 各代理普遍如此)
        actual_model: Optional[str] = None

        stream = self._client.chat.completions.create(**params)
        for chunk in stream:
            if actual_model is None and getattr(chunk, "model", None):
                actual_model = chunk.model
            if chunk.choices:
                choice = chunk.choices[0]
                delta = choice.delta
                if choice.finish_reason:
                    finish_reason = choice.finish_reason
                if delta is not None:
                    # 推理增量:思考模型在正式 content 之前先吐 reasoning_content。
                    # 只用于实时展示,不计入 out_tokens 估算(usage 已包含它),
                    # 也不进入 text_parts / 对话历史。
                    reasoning_chunk = getattr(delta, "reasoning_content", None)
                    if reasoning_chunk:
                        reasoning_parts.append(reasoning_chunk)
                        if on_thinking_delta:
                            on_thinking_delta(reasoning_chunk)
                    if getattr(delta, "content", None):
                        chunk_text = delta.content
                        text_parts.append(chunk_text)
                        if on_text_delta:
                            on_text_delta(chunk_text)
                        out_chars += len(chunk_text)
                        est = max(out_tokens, out_chars // 3)
                        if est != out_tokens:
                            out_tokens = est
                            emit_progress()
                    if getattr(delta, "tool_calls", None):
                        for tc in delta.tool_calls:
                            idx = getattr(tc, "index", 0) or 0
                            buf = tool_calls_buf.setdefault(idx, {"id": "", "name": "", "args": ""})
                            if getattr(tc, "id", None):
                                buf["id"] = tc.id
                            fn = getattr(tc, "function", None)
                            if fn is not None:
                                if getattr(fn, "name", None):
                                    buf["name"] = fn.name
                                if getattr(fn, "arguments", None):
                                    buf["args"] += fn.arguments

            usage = getattr(chunk, "usage", None)
            if usage:
                p = getattr(usage, "prompt_tokens", None)
                c = getattr(usage, "completion_tokens", None)
                if p is not None:
                    in_tokens = p
                if c is not None:
                    out_tokens = c
                emit_progress()

        text = "".join(text_parts)

        # 拼装 tool_calls
        tool_calls: list[ToolCall] = []
        raw_tool_calls: list[dict] = []
        for idx in sorted(tool_calls_buf):
            buf = tool_calls_buf[idx]
            if not buf["name"]:
                continue
            try:
                args = json.loads(buf["args"] or "{}")
            except json.JSONDecodeError:
                args = {}
            tool_calls.append(ToolCall(id=buf["id"], name=buf["name"], arguments=args))
            raw_tool_calls.append({
                "id": buf["id"],
                "type": "function",
                "function": {"name": buf["name"], "arguments": buf["args"] or "{}"},
            })

        # 兜底:本地小模型(qwen2.5-coder:7b 等)经常不按模板把调用包进
        # <tool_call> 标签,而是把裸 JSON 吐进 content,导致 Ollama 解析不出
        # tool_calls 字段。这里在原生 tool_calls 为空、且本轮确实提供了工具时,
        # 尝试从正文里把调用捞回来。详见 tests/unit/test_compatible_adapter.py。
        if not tool_calls and tools and text.strip():
            valid_names = {t["name"] for t in tools}
            recovered = _extract_tool_calls_from_text(text, valid_names)
            for i, (name, args) in enumerate(recovered):
                call_id = f"call_fallback_{i}"
                tool_calls.append(ToolCall(id=call_id, name=name, arguments=args))
                raw_tool_calls.append({
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
                })
            if recovered:
                # 正文本身就是工具调用而非散文,清空它:既避免把这段 JSON
                # 当作助手发言渲染给用户,也让回放时模板走 .ToolCalls 分支
                # (模板里 content 与 tool_calls 是 if/else-if,content 非空会
                #  压掉 tool_calls,破坏历史里的调用记录)
                text = ""

        payload: dict[str, Any] = {"role": "assistant", "content": text}
        if raw_tool_calls:
            payload["tool_calls"] = raw_tool_calls

        usage_dict = {"input_tokens": in_tokens, "output_tokens": out_tokens}

        # provider_payload 是 list[dict],由 Runtime 调 messages.extend 追加。
        # OpenAI Chat Completions 用单条 assistant 消息表示这一轮(无论有多少
        # 工具调用都塞在 message.tool_calls 里),所以这里只产生一个 dict,
        # 包成单元素 list 以匹配统一接口。
        return ModelResponse(
            text=text,
            tool_calls=tool_calls,
            usage=usage_dict,
            provider_payload=[payload],
            finish_reason=finish_reason,
            actual_model=actual_model,
            reasoning="".join(reasoning_parts),
        )

    @staticmethod
    def format_tool_results(results: list[ToolResult]) -> list[dict]:
        """把工具执行结果转成 OpenAI Chat Completions 格式。

        Chat Completions 规定:
          - 每个工具结果是一条独立 role=tool 的消息
          - tool_call_id 必须对应上一轮 assistant.tool_calls[*].id
          - content 是工具输出字符串(无 is_error 字段,出错信息直接写在 content 里)

        所以 N 个 ToolResult 会产生 N 条独立 message;is_error 通过在 content
        里加 "ERROR: " 前缀让模型识别(Chat Completions 协议本身不区分)。
        """
        return [
            {
                "role": "tool",
                "tool_call_id": r.tool_call_id,
                "content": (f"ERROR: {r.output}" if r.is_error else r.output),
            }
            for r in results
        ]


def _to_openai_tool(t: dict) -> dict:
    """把 Anthropic 风格的工具 schema 转成 OpenAI function tool 格式。"""
    return {
        "type": "function",
        "function": {
            "name": t["name"],
            "description": t.get("description", ""),
            "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
        },
    }


# <tool_call>...</tool_call> 标签内的内容(Qwen 模板约定的正确格式)
_TOOL_CALL_TAG_RE = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL)


def _iter_json_objects(text: str):
    """扫描文本,产出其中每个用花括号平衡配对的顶层 JSON 对象子串。

    小模型常把工具调用 JSON 夹在散文中间(甚至套 ```json 围栏),整段
    json.loads 会失败。这里靠括号配对找出每一段 {...},逐个交给调用方试解析。
    只追踪顶层对象(深度从 0→1 开始、回到 0 结束),嵌套对象包含其中。
    字符串内的花括号用简单状态机跳过,避免被 {"path": "{x}"} 这类值带偏。
    """
    depth = 0
    start = -1
    in_str = False
    escaped = False
    for i, ch in enumerate(text):
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start != -1:
                    yield text[start:i + 1]
                    start = -1


def _extract_tool_calls_from_text(text: str, valid_names: set[str]) -> list[tuple[str, dict]]:
    """从助手正文里捞回被当成文本输出的工具调用。

    本地小模型常见的不规范输出,都在这里兜住:
      1. 规范但被 Ollama 漏解析:<tool_call>{"name":..., "arguments":...}</tool_call>
      2. 裸 JSON:{"name": "list_dir", "arguments": {"path": "."}}
      3. Markdown 围栏:```json\n{...}\n```
      4. JSON 夹在大段散文中间(前后都有解释性文字)

    策略:先抓 <tool_call> 标签;没有标签就扫描整段里所有花括号配对出的
    JSON 对象。只认 name 在 valid_names 里的对象,避免把模型正常输出的、
    恰好长得像工具调用的 JSON(或代码示例)误判成调用。
    返回 [(name, arguments_dict), ...]。
    """
    # 标签内可能还套着 ```json 围栏或多余文字,所以统一交给 _iter_json_objects;
    # 有标签时只在标签内找,没标签时在全文找。
    tagged = _TOOL_CALL_TAG_RE.findall(text)
    search_spaces = tagged if tagged else [text]

    results: list[tuple[str, dict]] = []
    for space in search_spaces:
        for raw in _iter_json_objects(space):
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            name = obj.get("name")
            if name not in valid_names:
                continue
            args = obj.get("arguments", {})
            # arguments 可能是 JSON 字符串(部分模型这么做),也可能已是 dict
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            if not isinstance(args, dict):
                args = {}
            results.append((name, args))

    return results
