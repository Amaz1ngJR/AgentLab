"""ChatGPT plan Responses transport; execution remains in AgentLab."""
from __future__ import annotations

import json
import copy

from app.agent.cancel import close_on_cancel
from app.auth.chatgpt import RESOURCE, TokenManager
from app.models.openai_adapter import (
    OpenAIAdapter, ProviderStreamError, _convert_messages_to_responses_format,
    _repair_responses_tool_pairs, _to_responses_tool,
)
from app.models.protocol import ModelResponse, ToolCall
from app.models.cache_usage import cache_usage
from app.models.request_audit import emit_request_event, start_request
from app.models.token_progress import StreamingTokenProgress
from app.models.context_window import positive_int

NAMESPACE = "agentlab"
TEMPORARY_CODES = {"subscription_sharing_usage_unavailable", "subscription_sharing_user_unavailable",
                   "server_error", "internal_error", "service_unavailable", "rate_limit_exceeded", "overloaded"}


class ChatGPTError(ProviderStreamError):
    def __init__(self, code, *, status=None, request_id=None, param=None, retry_after=None):
        self.code, self.status_code, self.request_id = code, status, request_id
        self.param, self.retry_after = param, retry_after
        hint = {
            "subscription_sharing_usage_limit_exceeded": "额度限制，请查看 https://chatgpt.com/settings/usage",
            "subscription_sharing_user_not_eligible": "该账号或工作区暂不支持 ChatGPT 额度授权",
            "subscription_sharing_invalid_user": "登录身份不可用，请检查账号或重新登录",
            "subscription_sharing_unsupported_capability": "请求包含当前授权路线不支持的能力",
            "admission_denied": "请求未获准，请检查账号权限、地区或登录状态",
        }.get(code, "请检查 ChatGPT 账号授权和接口配置")
        super().__init__(f"ChatGPT 请求失败 ({code}; HTTP {status}; request_id={request_id}; param={param})：{hint}",
                         retryable=code in TEMPORARY_CODES)


class ChatGPTAdapter:
    def __init__(self, cfg):
        self._cfg = cfg
        self.tokens = TokenManager(cfg.chatgpt_account)

    def _client(self):
        from openai import OpenAI
        # Request-local clients avoid stale credentials and work with older SDKs.
        return OpenAI(api_key=self.tokens.access_token(), base_url=RESOURCE,
                      organization="", project="", timeout=self._cfg.timeout_seconds, max_retries=0)

    @property
    def provider(self):
        return "openai_chatgpt"

    @property
    def model(self):
        return self._cfg.model

    @property
    def supports_vision(self):
        return "vision" in self._cfg.capabilities

    def fetch_context_size(self):
        """Use this account's catalog, not the generic API model's maximum.

        ContextWindowResolver caches this per router and honors explicit config
        first. A failed lookup keeps its conservative fallback and short retry TTL.
        """
        import httpx
        response = httpx.get(RESOURCE + "/models", timeout=3, follow_redirects=False,
            headers={"Authorization": "Bearer " + self.tokens.access_token()})
        response.raise_for_status()
        for model in response.json().get("models", []):
            if model.get("slug") == self.model:
                return positive_int(model.get("context_window"))
        return None

    def _params(self, messages, tools, system, effort):
        from app.models.append_prefix import INSTRUCTION, tool_bindings
        stable = any(m.get(INSTRUCTION) for m in messages)
        inputs = []
        for message in messages:
            if message.get("type") == "additional_tools":
                inputs.append(copy.deepcopy({key: message[key] for key in ("type", "role", "tools")}))
                continue
            if message.get("type") == "reasoning":
                # Stateless Responses needs the opaque reasoning item, in place.
                # Plain summaries without encrypted state cannot restore reasoning.
                if (isinstance(message.get("encrypted_content"), str) and message["encrypted_content"]
                        and isinstance(message.get("id"), str) and message["id"]):
                    state = {key: copy.deepcopy(message[key]) for key in
                             ("type", "id", "summary", "encrypted_content") if key in message}
                    state["summary"] = state.get("summary") or []
                    inputs.append(state)
                continue
            if message.get("role") in {"system", "developer"}:
                content = message.get("content", "")
                if isinstance(content, str):
                    content = [{"type": "input_text", "text": content}]
                converted = _convert_messages_to_responses_format([
                    {"type": "message", "role": "developer", "content": content}])
            else:
                converted = _convert_messages_to_responses_format([message])
            if message.get("type") == "message" and message.get("role") == "assistant":
                if message.get("phase") in {"commentary", "final_answer"}:
                    for item in converted:
                        item["phase"] = message["phase"]
            inputs.extend(converted)
        # The common converter intentionally strips provider-specific fields.
        # Restore namespace on calls by stable call_id, including persisted history.
        namespaces = {m.get("call_id"): m.get("namespace") for m in messages
                      if m.get("type") == "function_call"}
        for item in inputs:
            if item.get("role") == "system":
                item["role"] = "developer"
            if item.get("type") == "function_call" and namespaces.get(item["call_id"]):
                item["namespace"] = namespaces[item["call_id"]]
        params = dict(model=self.model, input=_repair_responses_tool_pairs(inputs), store=False, stream=True)
        if system and not stable:
            params["instructions"] = system
        if effort:
            params["reasoning"] = {"effort": effort}
        if stable:
            params["tool_choice"] = ({"type": "allowed_tools", "mode": "auto",
                "tools": [{"type": "function", "name": name} for name in tool_bindings(messages, tools)]} if tools else "none")
        elif tools:
            params["tools"] = [{"type": "namespace", "name": NAMESPACE,
                "description": "AgentLab local tools, subject to user approval.",
                "tools": [_to_responses_tool(t) for t in tools]}]
        return params

    def chat(self, messages, temperature=None):
        return self.create_message(messages).text

    def create_message(self, messages, tools=None, system=None, temperature=None,
                       max_tokens=None, on_progress=None, on_text_delta=None,
                       on_thinking_delta=None, cancel=None, reasoning_effort_override=None):
        from openai import APIStatusError
        params = self._params(messages, tools, system,
                              reasoning_effort_override or self._cfg.reasoning_effort)
        progress = StreamingTokenProgress(on_progress, 0)
        progress.emit(force=True)
        final = None
        request_id = None
        response_id = None
        response_usage = None
        http_status = None
        error_code = None
        outcome = "failed"
        reasoning = []
        completed_items = {}
        streamed_text = {}
        if cancel:
            cancel.raise_if_cancelled()
        audit = start_request()
        try:
            with self._client() as client, client.responses.create(**params) as stream:
                response = getattr(stream, "response", None)
                http_status = getattr(response, "status_code", None)
                headers = getattr(response, "headers", {})
                request_id = headers.get("x-request-id")
                with close_on_cancel(cancel, stream):
                    for event in stream:
                        if cancel:
                            cancel.raise_if_cancelled()
                        kind = event.type
                        if kind in {"response.failed", "response.error", "error", "response.incomplete"}:
                            failed = getattr(event, "response", None)
                            response_id = getattr(failed, "id", None)
                            response_usage = getattr(failed, "usage", None)
                            error = getattr(failed, "error", None) or getattr(event, "error", None)
                            code = getattr(error, "code", None) or getattr(event, "code", None) or kind
                            raise ChatGPTError(code, status=getattr(response, "status_code", None),
                                request_id=request_id, param=getattr(error, "param", None))
                        if kind == "response.output_text.delta":
                            streamed_text.setdefault(event.output_index, []).append(event.delta)
                            if on_text_delta:
                                on_text_delta(event.delta)
                            progress.add_text(event.delta)
                        elif kind == "response.reasoning_summary_text.delta":
                            reasoning.append(event.delta)
                            if on_thinking_delta:
                                on_thinking_delta(event.delta)
                            progress.add_reasoning(event.delta)
                        elif kind == "response.function_call_arguments.delta":
                            progress.add_text(event.delta)
                        elif kind == "response.output_item.done":
                            completed_items[event.output_index] = event.item
                        elif kind == "response.completed":
                            final = event.response
                            response_id = getattr(final, "id", None)
                            response_usage = final.usage
            if final is not None and final.status == "completed":
                outcome = "completed"
            else:
                error_code = "stream_not_completed"
        except APIStatusError as exc:
            request_id = exc.request_id or request_id
            http_status = exc.status_code
            if cancel:
                cancel.raise_if_cancelled()
            body = exc.body if isinstance(exc.body, dict) else {}
            error = body.get("error", body)
            code = error.get("code") if isinstance(error, dict) else None
            if not code and exc.status_code >= 500:
                code = "service_unavailable"
            if not code and exc.status_code == 429:
                code = "rate_limit_exceeded"
            error_code = code or "admission_denied"
            from app.models.provider_retry import _retry_after
            raise ChatGPTError(error_code, status=exc.status_code,
                               request_id=exc.request_id,
                               param=error.get("param") if isinstance(error, dict) else None,
                               retry_after=_retry_after(exc)) from None
        except Exception as exc:
            error_code = getattr(exc, "code", None) or type(exc).__name__
            # Closing an in-flight stream can manifest as an SDK/transport error.
            if cancel:
                cancel.raise_if_cancelled()
            raise
        finally:
            if audit is not None:
                emit_request_event("model.request.finished", {
                    **audit, "provider": self.provider, "model": self.model,
                    "actual_model": getattr(final, "model", None),
                    "reasoning_effort": reasoning_effort_override or self._cfg.reasoning_effort,
                    "request_id": request_id, "response_id": response_id,
                    "http_status": http_status,
                    "status": "cancelled" if cancel and cancel.cancelled and outcome != "completed" else outcome,
                    "error_code": error_code,
                    "input_tokens": getattr(response_usage, "input_tokens", None),
                    "output_tokens": getattr(response_usage, "output_tokens", None),
                    "cached_tokens": cache_usage(response_usage, provider="openai").get("cache_read_input_tokens"),
                })
        if cancel:
            cancel.raise_if_cancelled()
        if final is None or final.status != "completed":
            raise ProviderStreamError("ChatGPT 流未完成；未提交工具调用或会话结果", retryable=False)
        output, calls, text = [], [], []
        from app.models.append_prefix import INSTRUCTION, tool_bindings
        bindings = (tool_bindings(messages, tools) if any(m.get(INSTRUCTION) for m in messages)
                    else {t["name"]: t["name"] for t in tools or []})
        call_ids = set()
        # Some streams omit output items from the terminal response. Only recover
        # after a successful completion, and never build tool calls from deltas.
        items = {index: item.model_dump(exclude_none=True)
                 for index, item in completed_items.items()}
        items.update({index: item.model_dump(exclude_none=True)
                      for index, item in enumerate(final.output or [])})
        for index, chunks in streamed_text.items():
            value = items.get(index)
            if value is None:
                items[index] = {"type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": "".join(chunks)}]}
            elif value.get("type") == "message" and not any(
                    part.get("text") or part.get("refusal") for part in value.get("content") or []):
                value["content"] = [{"type": "output_text", "text": "".join(chunks)}]
        for index in sorted(items):
            value = items[index]
            output.append(value)
            if value["type"] == "message":
                for part in value.get("content") or []:
                    text.append(part.get("text") or part.get("refusal") or "")
            elif value["type"] == "function_call":
                if value["name"] not in bindings or value.get("namespace") not in {None, NAMESPACE}:
                    raise ProviderStreamError("ChatGPT 返回了未声明的工具或 namespace")
                if not value.get("call_id") or value["call_id"] in call_ids:
                    raise ProviderStreamError("ChatGPT 返回了缺失或重复的工具调用 ID；未执行工具")
                call_ids.add(value["call_id"])
                try:
                    arguments = json.loads(value["arguments"])
                    if not isinstance(arguments, dict):
                        raise ValueError()
                except ValueError:
                    raise ProviderStreamError("ChatGPT 返回了无效的工具参数；未执行工具") from None
                calls.append(ToolCall(id=value["call_id"], name=bindings[value["name"]], arguments=arguments))
        usage = final.usage
        if not calls and not any(text):
            raise ProviderStreamError("ChatGPT 已结束但没有返回文本或工具调用，不能判定任务完成")
        in_tokens = getattr(usage, "input_tokens", 0) or 0
        out_tokens = getattr(usage, "output_tokens", 0) or 0
        details = cache_usage(usage, provider="openai")
        reasoning_tokens = getattr(getattr(usage, "output_tokens_details", None), "reasoning_tokens", None)
        if isinstance(reasoning_tokens, int):
            details["reasoning_tokens"] = reasoning_tokens
        progress.set_usage(input_tokens=in_tokens, output_tokens=out_tokens, final=True)
        return ModelResponse(text="".join(text), tool_calls=calls, provider_payload=output,
            usage={"input_tokens": in_tokens, "output_tokens": out_tokens, **details},
            finish_reason=final.status, actual_model=final.model, reasoning="".join(reasoning))

    format_tool_results = staticmethod(OpenAIAdapter.format_tool_results)
