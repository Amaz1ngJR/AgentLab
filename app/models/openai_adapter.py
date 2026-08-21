"""OpenAI 原生 adapter —— 调用 OpenAI Responses API (推荐的新接口)。

为什么单独做一个 adapter:
  Responses API 不是 Chat Completions 的"超集",输入 / 输出 / 工具调用结构
  都跟 Chat 不同。把它塞进 OpenAICompatibleAdapter 会让那个 adapter 长出
  分支来回切,不如分开干净。

  OpenAICompatibleAdapter -> Chat Completions (Ollama / LM Studio / vLLM)
  OpenAIAdapter           -> Responses API (官方 GPT-5 / GPT-4.1 等)

关键结构差异(对比 Chat Completions):
  - 输入字段叫 input,不是 messages
  - 工具是扁平 dict: {type: "function", name, description, parameters}
    (Chat 是嵌套 {type: function, function: {name, ...}})
  - 模型上轮的"调用了哪些工具"用 type=function_call 项 + role=assistant
    message 项 *分开* 表示,不像 Chat 把 tool_calls 塞进 assistant message
  - 工具结果用 type=function_call_output 项,不是 role=tool 消息
  - 流式事件类型用 "response.output_text.delta" 等,跟 Chat 完全不同
"""
from __future__ import annotations

import json
from typing import Any, Optional

from app.config.schemas import LLMConfig
from app.attachments import image_block_to_data_url
from app.models.token_progress import StreamingTokenProgress
from app.models.protocol import (
    ModelResponse,
    ProgressCallback,
    TextDeltaCallback,
    ThinkingDeltaCallback,
    ToolCall,
    ToolResult,
)


_MISSING_TOOL_OUTPUT = "[历史中的工具结果缺失，状态未知，请勿假设工具已执行]"


def _repair_responses_tool_pairs(items: list[dict]) -> list[dict]:
    """返回满足 Responses API 工具调用配对约束的新列表。

    历史会话可能因中断或旧版持久化损坏而丢失 function_call_output。
    给悬空调用补一个合成结果；同时丢弃没有调用方的孤立结果。这里不修改
    session.messages，避免模型输入清理意外改写审计历史。
    """
    call_ids = {
        item.get("call_id")
        for item in items
        if item.get("type") == "function_call" and item.get("call_id")
    }
    output_ids = {
        item.get("call_id")
        for item in items
        if item.get("type") == "function_call_output" and item.get("call_id")
    }
    # 保留原顺序：在每个 function_call 后插入缺失的结果，避免把旧调用的
    # 合成 output 统一堆到当前用户消息之后。
    repaired: list[dict] = []
    for item in items:
        item_type = item.get("type")
        call_id = item.get("call_id")
        if item_type == "function_call":
            repaired.append(item)
            if call_id:
                if call_id not in output_ids:
                    repaired.append({
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": _MISSING_TOOL_OUTPUT,
                    })
        elif item_type != "function_call_output" or call_id in call_ids:
            repaired.append(item)
    return repaired


class OpenAIAdapter:
    """走 OpenAI Responses API 的 adapter,用于 GPT-5 等 OpenAI 原生模型。"""

    def __init__(self, cfg: LLMConfig):
        from openai import OpenAI

        self._cfg = cfg
        if not cfg.api_key:
            raise RuntimeError("openai provider 需要 OPENAI_API_KEY")

        kwargs: dict[str, Any] = {"timeout": cfg.timeout_seconds, "api_key": cfg.api_key}
        if cfg.base_url:
            kwargs["base_url"] = cfg.base_url
        self._client = OpenAI(**kwargs)

    @property
    def model(self) -> str:
        return self._cfg.model

    @property
    def provider(self) -> str:
        return "openai"

    def _base_params(self, messages: list[dict], temperature: Optional[float],
                     system: Optional[str]) -> dict[str, Any]:
        # Responses API 通过 instructions 顶级字段传 system,比塞进 input 更清晰
        # (语义和 Anthropic 的 system 参数一致)

        # 转换后统一修复工具调用配对。旧会话可能因中断或旧版持久化
        # 损坏而缺少 output；上下文切分也可能留下孤立 output。
        cleaned_input = _repair_responses_tool_pairs(
            _convert_messages_to_responses_format(messages)
        )

        params: dict[str, Any] = {
            "model": self._cfg.model,
            "input": cleaned_input,
            "temperature": self._cfg.temperature if temperature is None else temperature,
        }
        if system:
            params["instructions"] = system
        if self._cfg.top_p is not None:
            params["top_p"] = self._cfg.top_p
        if self._cfg.reasoning_effort is not None:
            # Responses API 的字段是 reasoning={"effort": ...}，不是
            # Chat Completions 风格的顶级 reasoning_effort。
            params["reasoning"] = {"effort": self._cfg.reasoning_effort}
        return params

    def chat(self, messages: list[dict], temperature: Optional[float] = None) -> str:
        """简单对话,不带工具。返回纯文本。"""
        params = self._base_params(messages, temperature, system=None)
        resp = self._client.responses.create(**params)
        # SDK 提供 output_text 便捷属性,自动拼接所有 text 段
        return getattr(resp, "output_text", "") or ""

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
        """带工具调用的完整对话,供 Agent 循环使用。"""
        params = self._base_params(messages, temperature, system)
        params["max_output_tokens"] = max_tokens
        if tools:
            # Responses API 的工具定义是扁平结构,直接 name / parameters,不嵌套 function
            params["tools"] = [_to_responses_tool(t) for t in tools]

        # ── 流式累积状态 ────────────────────────────────────────────────────
        # 输入 token 只能在服务端 usage 到达时确定。Responses API 通常只在
        # response.completed 返回真值；在此之前显示 0，避免把粗略字符估算误标成
        # 已消费 token（例如 planning 一上来固定显示 3.9k）。
        in_tokens = 0
        out_tokens = 0
        progress = StreamingTokenProgress(on_progress, in_tokens)
        progress.emit(force=True)

        # 流式事件中累计正文、reasoning summary 和 function arguments。厂商通常只在
        #结束时返回真实 usage，期间用增量估算让 spinner 持续变化。
        with self._client.responses.stream(**params) as stream:
            for event in stream:
                etype = getattr(event, "type", None)
                delta_text = getattr(event, "delta", "") or ""
                response = getattr(event, "response", None)
                event_usage = getattr(response, "usage", None) if response is not None else None
                if event_usage is not None:
                    event_input = getattr(event_usage, "input_tokens", None)
                    event_output = getattr(event_usage, "output_tokens", None)
                    if event_input is not None:
                        in_tokens = event_input
                    progress.set_usage(
                        input_tokens=event_input,
                        output_tokens=event_output,
                    )
                if etype == "response.output_text.delta":
                    if delta_text:
                        if on_text_delta:
                            on_text_delta(delta_text)
                        progress.add_text(delta_text)
                elif etype in {
                    "response.reasoning_text.delta",
                    "response.reasoning_summary_text.delta",
                }:
                    if delta_text:
                        if on_thinking_delta:
                            on_thinking_delta(delta_text)
                        progress.add_reasoning(delta_text)
                elif etype in {
                    "response.function_call_arguments.delta",
                    "response.custom_tool_call_input.delta",
                }:
                    progress.add_text(delta_text)

            final = stream.get_final_response()

        # ── 从 final.output 解析最终结果 ────────────────────────────────────
        # output 是混合 item 列表:可能含 ResponseOutputMessage 与
        # ResponseFunctionToolCall。用 model_dump() 转 dict 方便后续 JSON 化与回放。
        output_items: list[dict] = []
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []

        for item in final.output:
            item_type = getattr(item, "type", None)
            item_dict = item.model_dump() if hasattr(item, "model_dump") else dict(item)
            output_items.append(item_dict)

            if item_type == "message":
                # message item 的 content 是一个列表,每项可能是 output_text / refusal
                for part in getattr(item, "content", []) or []:
                    if getattr(part, "type", None) == "output_text":
                        text_parts.append(getattr(part, "text", "") or "")
            elif item_type == "function_call":
                # 工具调用项:call_id 用于回传结果时对应,arguments 是 JSON 字符串
                call_id = getattr(item, "call_id", "")
                name = getattr(item, "name", "")
                args_raw = getattr(item, "arguments", "") or "{}"
                try:
                    args = json.loads(args_raw)
                except json.JSONDecodeError:
                    args = {}
                tool_calls.append(ToolCall(id=call_id, name=name, arguments=args))

        # 真实 usage 覆盖估算值(若 SDK 提供)
        usage_obj = getattr(final, "usage", None)
        if usage_obj is not None:
            in_tokens = getattr(usage_obj, "input_tokens", in_tokens) or in_tokens
            out_tokens = getattr(usage_obj, "output_tokens", progress.output_tokens)
            if out_tokens is None:
                out_tokens = progress.output_tokens
            progress.set_usage(
                input_tokens=in_tokens,
                output_tokens=out_tokens,
                force=True,
                final=True,
            )
        else:
            out_tokens = progress.output_tokens

        return ModelResponse(
            text="".join(text_parts),
            tool_calls=tool_calls,
            usage={"input_tokens": in_tokens, "output_tokens": out_tokens},
            # provider_payload 是 list,Runtime 用 messages.extend 追加。
            # Responses API 把 message item 与 function_call item 拆开放,
            # 下一轮把这些 item 原样作为 input 一起发,模型才能理解上下文。
            provider_payload=output_items,
            finish_reason=getattr(final, "status", None),
            # 真实模型 ID:同 Anthropic 的 message.model,代理静默映射时这里
            # 会和请求时写的 model 不同,把它向上暴露
            actual_model=getattr(final, "model", None),
        )

    @staticmethod
    def format_tool_results(results: list[ToolResult]) -> list[dict]:
        """把工具执行结果转成 Responses API 的 function_call_output input items。

        Responses API 规定:
          - 每个工具结果是一条独立的 input item,type=function_call_output
          - call_id 必须对应上一轮 function_call item 的 call_id
          - output 是工具输出字符串(Responses API 不区分 is_error,错误信息
            直接写在 output 里加 ERROR 前缀让模型识别)
        """
        return [
            {
                "type": "function_call_output",
                "call_id": r.tool_call_id,
                "output": (f"ERROR: {r.output}" if r.is_error else r.output),
            }
            for r in results
        ]


def _to_responses_tool(t: dict) -> dict:
    """把 Anthropic 风格 input_schema 工具描述转成 Responses API 的扁平结构。

    Anthropic / 内部统一格式:
        {"name": ..., "description": ..., "input_schema": {...}}
    Responses API 期望:
        {"type": "function", "name": ..., "description": ..., "parameters": {...}}
    """
    return {
        "type": "function",
        "name": t["name"],
        "description": t.get("description", ""),
        "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
    }


def _estimate_input_tokens(messages: list[dict], system: Optional[str]) -> int:
    """粗略估算 input token 数:把消息里所有文本字段的字符数加起来 / 3。

    用于流式开始时让 spinner 显示一个非零的 input_tokens 占位值。真实数字
    由 final.usage 在末尾覆盖。中文 1 字 ≈ 0.6 token,英文 4 字符 ≈ 1 token,
    取折中 3 字符 ≈ 1 token。
    """
    char_count = 0
    if system:
        char_count += len(system)
    for m in messages:
        content = m.get("content")
        if isinstance(content, str):
            char_count += len(content)
        elif isinstance(content, list):
            # content 可能是 part list,把 text 字段加起来
            for part in content:
                if isinstance(part, dict):
                    char_count += len(part.get("text", "") or "")
    return char_count // 3


def _convert_messages_to_responses_format(messages: list[dict]) -> list[dict]:
    """将 Anthropic/通用消息格式转换为 OpenAI Responses API 格式。

    AgentLab 内部使用混合格式:
      - 用户消息: {"role": "user", "content": "..."}
      - Anthropic assistant: {"role": "assistant", "content": [{"type": "text", ...}, {"type": "tool_use", ...}]}
      - Anthropic tool_result: {"role": "user", "content": [{"type": "tool_result", ...}]}
      - OpenAI Responses items: {"type": "message", "role": "assistant", "content": [...]}

    Responses API 需要的格式:
      - 用户文本: {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "..."}]}
      - Assistant 文本: {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "..."}]}
      - 工具调用: {"type": "function_call", "call_id": "...", "name": "...", "arguments": {...}}
      - 工具结果: {"type": "function_call_output", "call_id": "...", "output": "..."}
    """
    result = []

    for msg in messages:
        # 如果已经是 Responses API 格式 (有 type 字段), 清理并保留
        if "type" in msg:
            # 移除不支持的字段（如 parsed_arguments）
            cleaned = {k: v for k, v in msg.items() if k not in ('parsed_arguments',)}
            result.append(cleaned)
            continue

        role = msg.get("role")
        content = msg.get("content")

        # 处理用户消息
        if role == "user":
            if isinstance(content, str):
                # 简单文本消息
                result.append({
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": content}]
                })
            elif isinstance(content, list):
                # 多模态内容 (文本 + 图片) 或 工具结果
                converted_content = []
                tool_results = []

                for item in content:
                    if isinstance(item, dict):
                        item_type = item.get("type")
                        if item_type == "text":
                            converted_content.append({"type": "input_text", "text": item.get("text", "")})
                        elif item_type == "image":
                            # 内部图片块可能是受控 file 引用，也兼容旧版 base64；
                            # 发请求时才读取编码，SQLite 中不会保存大段 base64。
                            converted_content.append({
                                "type": "input_image",
                                "image_url": image_block_to_data_url(item),
                            })
                        elif item_type == "tool_result":
                            # Anthropic tool_result 转为 Responses API function_call_output
                            tool_results.append({
                                "type": "function_call_output",
                                "call_id": item.get("tool_use_id", ""),
                                "output": item.get("content", "") if isinstance(item.get("content"), str) else str(item.get("content", ""))
                            })
                        else:
                            # 其他类型尝试直接保留
                            converted_content.append(item)

                # 如果有普通内容，添加为 message
                if converted_content:
                    result.append({
                        "type": "message",
                        "role": "user",
                        "content": converted_content
                    })

                # 如果有工具结果，添加为 function_call_output items
                result.extend(tool_results)

        # 处理 assistant 消息
        elif role == "assistant":
            if isinstance(content, str):
                # 简单文本回复
                result.append({
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": content}]
                })
            elif isinstance(content, list):
                # Anthropic 格式: content 是 block 列表
                text_blocks = []
                tool_calls = []

                for block in content:
                    if isinstance(block, dict):
                        block_type = block.get("type")
                        if block_type == "text":
                            text_blocks.append({"type": "output_text", "text": block.get("text", "")})
                        elif block_type == "tool_use":
                            # Anthropic tool_use 转为 Responses API function_call
                            # 注意: arguments 必须是 JSON 字符串, 不是对象
                            tool_calls.append({
                                "type": "function_call",
                                "call_id": block.get("id", ""),
                                "name": block.get("name", ""),
                                "arguments": json.dumps(block.get("input", {}))
                            })

                # 添加 assistant message (文本部分)
                if text_blocks:
                    result.append({
                        "type": "message",
                        "role": "assistant",
                        "content": text_blocks
                    })

                # 添加 function_call items (工具调用)
                result.extend(tool_calls)

    return result
