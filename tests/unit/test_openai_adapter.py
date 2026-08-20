"""离线测试：OpenAI Responses API adapter。用 mock 替代真实 HTTP 调用。

OpenAI Responses API 的关键差异需要 mock 反映:
  - client.responses.stream(...) 返回上下文管理器,支持 for event in stream
  - 流式事件 type 是 "response.output_text.delta" 这种串
  - stream.get_final_response() 返回 Response 对象,有 output (list of items)
    和 usage (.input_tokens / .output_tokens)
  - output item 类型: type=message 含 content (output_text part 列表),
    或 type=function_call (call_id, name, arguments JSON 字符串)
"""
from unittest.mock import MagicMock, patch

from app.config.schemas import LLMConfig
from app.attachments import build_user_content
from app.models.openai_adapter import (
    OpenAIAdapter,
    _MISSING_TOOL_OUTPUT,
    _convert_messages_to_responses_format,
    _repair_responses_tool_pairs,
)
from app.models.protocol import ToolResult


def _cfg() -> LLMConfig:
    return LLMConfig(
        provider="openai",
        model="gpt-5",
        base_url=None,
        api_key="sk-test",
        auth_token=None,
        temperature=0.2,
        top_p=None,
        context_size=None,
        timeout_seconds=30,
        stream=False,
    )


def _output_text_part(text: str) -> MagicMock:
    """构造 message item 内的一个 output_text content part。"""
    part = MagicMock()
    part.type = "output_text"
    part.text = text
    return part


def _message_item(text: str) -> MagicMock:
    """构造 type=message 的 output item。"""
    item = MagicMock()
    item.type = "message"
    item.content = [_output_text_part(text)]
    item.model_dump.return_value = {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text}],
    }
    return item


def _function_call_item(call_id: str, name: str, arguments_json: str) -> MagicMock:
    """构造 type=function_call 的 output item。"""
    item = MagicMock()
    item.type = "function_call"
    item.call_id = call_id
    item.name = name
    item.arguments = arguments_json
    item.model_dump.return_value = {
        "type": "function_call",
        "call_id": call_id,
        "name": name,
        "arguments": arguments_json,
    }
    return item


def _delta_event(text: str) -> MagicMock:
    """构造 response.output_text.delta 流式事件。"""
    ev = MagicMock()
    ev.type = "response.output_text.delta"
    ev.delta = text
    return ev


class _FakeStream:
    """模拟 client.responses.stream() 返回的上下文管理器。

    SDK 真实对象支持: with ... as s: for event in s: ...; s.get_final_response()
    """

    def __init__(self, events: list, final: MagicMock):
        self._events = events
        self._final = final

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def __iter__(self):
        return iter(self._events)

    def get_final_response(self):
        return self._final


def _build_final(output_items: list, in_tokens: int = 12, out_tokens: int = 8) -> MagicMock:
    """构造 stream.get_final_response() 返回的 Response 对象。"""
    final = MagicMock()
    final.output = output_items
    final.usage = MagicMock()
    final.usage.input_tokens = in_tokens
    final.usage.output_tokens = out_tokens
    final.status = "completed"
    return final


def test_file_image_converts_to_responses_input_image(tmp_path, monkeypatch):
    from app import attachments
    from app.attachments import AttachmentStore
    monkeypatch.setattr(attachments, "DEFAULT_ATTACHMENT_ROOT", tmp_path)
    from PIL import Image
    image_path = tmp_path / "source.png"
    Image.new("RGB", (4, 4), "red").save(image_path)
    attachment = AttachmentStore(tmp_path).add_path(
        "s1", image_path, workspace_root=tmp_path,
    )
    converted = _convert_messages_to_responses_format([{
        "role": "user",
        "content": build_user_content("看图", [attachment]),
    }])
    blocks = converted[0]["content"]
    assert blocks[0] == {"type": "input_text", "text": "看图"}
    assert blocks[1]["type"] == "input_image"
    assert blocks[1]["image_url"].startswith("data:image/png;base64,")


    cfg = _cfg()
    cfg.reasoning_effort = "high"
    with patch("openai.OpenAI") as MockOpenAI:
        adapter = OpenAIAdapter(cfg)
        params = adapter._base_params(
            [{"role": "user", "content": "solve"}],
            temperature=None,
            system=None,
        )
    assert params["reasoning"] == {"effort": "high"}


def test_reasoning_parameter_omitted_when_not_configured():
    with patch("openai.OpenAI"):
        adapter = OpenAIAdapter(_cfg())
        params = adapter._base_params([], temperature=None, system=None)
    assert "reasoning" not in params


def test_create_message_text_only():
    """纯文本回复: 流式 delta 触发 on_text_delta + 最终从 message item 拼出 text。"""
    final = _build_final([_message_item("hello world")])
    stream = _FakeStream(events=[_delta_event("hello "), _delta_event("world")], final=final)

    seen_text: list[str] = []

    with patch("openai.OpenAI") as MockOpenAI:
        client = MagicMock()
        MockOpenAI.return_value = client
        client.responses.stream.return_value = stream

        adapter = OpenAIAdapter(_cfg())
        resp = adapter.create_message(
            messages=[{"role": "user", "content": "hi"}],
            on_text_delta=seen_text.append,
        )

    assert resp.text == "hello world"
    assert resp.tool_calls == []
    assert "".join(seen_text) == "hello world"
    assert resp.usage["input_tokens"] == 12
    assert resp.usage["output_tokens"] == 8


def test_create_message_with_function_call():
    """工具调用: final.output 含 function_call item,被解析成 ToolCall。"""
    final = _build_final([
        _function_call_item("call_abc", "read_file", '{"path": "README.md"}'),
    ])
    stream = _FakeStream(events=[], final=final)

    with patch("openai.OpenAI") as MockOpenAI:
        client = MagicMock()
        MockOpenAI.return_value = client
        client.responses.stream.return_value = stream

        adapter = OpenAIAdapter(_cfg())
        resp = adapter.create_message(
            messages=[{"role": "user", "content": "read README"}],
            tools=[{
                "name": "read_file",
                "description": "...",
                "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
            }],
        )

    assert len(resp.tool_calls) == 1
    tc = resp.tool_calls[0]
    assert tc.id == "call_abc"
    assert tc.name == "read_file"
    assert tc.arguments == {"path": "README.md"}


def test_provider_payload_is_list_of_output_items():
    """provider_payload 是 list[dict],每项是 final.output 的 model_dump 结果。

    Runtime 用 messages.extend(provider_payload) 追加,所以必须是 list,
    且每个元素是合法的 Responses API input item dict。
    """
    final = _build_final([
        _message_item("ok"),
        _function_call_item("call_x", "list_dir", "{}"),
    ])
    stream = _FakeStream(events=[], final=final)

    with patch("openai.OpenAI") as MockOpenAI:
        client = MagicMock()
        MockOpenAI.return_value = client
        client.responses.stream.return_value = stream

        adapter = OpenAIAdapter(_cfg())
        resp = adapter.create_message(messages=[{"role": "user", "content": "?"}])

    assert isinstance(resp.provider_payload, list)
    assert len(resp.provider_payload) == 2
    assert resp.provider_payload[0]["type"] == "message"
    assert resp.provider_payload[1]["type"] == "function_call"
    assert resp.provider_payload[1]["call_id"] == "call_x"


def test_format_tool_results_emits_function_call_output():
    """format_tool_results 把 ToolResult 列表转成 type=function_call_output input items。"""
    out = OpenAIAdapter.format_tool_results([
        ToolResult(tool_call_id="c1", output="hi", is_error=False),
        ToolResult(tool_call_id="c2", output="boom", is_error=True),
    ])
    assert len(out) == 2
    assert out[0] == {"type": "function_call_output", "call_id": "c1", "output": "hi"}
    # 错误时 output 加 ERROR 前缀让模型识别(协议本身不区分)
    assert out[1]["type"] == "function_call_output"
    assert out[1]["call_id"] == "c2"
    assert out[1]["output"].startswith("ERROR:")


def test_streaming_progress_callback():
    """流式过程中 on_progress 至少被调用 2 次:开始时与 final usage 到达后。"""
    final = _build_final([_message_item("hi")], in_tokens=20, out_tokens=4)
    stream = _FakeStream(events=[_delta_event("hi")], final=final)

    progress_log: list[dict] = []

    with patch("openai.OpenAI") as MockOpenAI:
        client = MagicMock()
        MockOpenAI.return_value = client
        client.responses.stream.return_value = stream

        adapter = OpenAIAdapter(_cfg())
        adapter.create_message(
            messages=[{"role": "user", "content": "hi"}],
            on_progress=progress_log.append,
        )

    assert len(progress_log) >= 2
    # 最终值应该是真实 usage,不是估算
    final_progress = progress_log[-1]
    assert final_progress["input_tokens"] == 20
    assert final_progress["output_tokens"] == 4


def test_base_params_repairs_missing_and_orphan_tool_outputs():
    messages = [
        {"type": "function_call", "call_id": "call_missing", "name": "read_file",
         "arguments": "{}"},
        {"type": "function_call_output", "call_id": "call_orphan", "output": "x"},
        {"role": "user", "content": "continue"},
    ]
    with patch("openai.OpenAI"):
        params = OpenAIAdapter(_cfg())._base_params(messages, None, None)

    items = params["input"]
    assert [item.get("call_id") for item in items if item.get("type") == "function_call_output"] == [
        "call_missing"
    ]
    assert items[1] == {
        "type": "function_call_output",
        "call_id": "call_missing",
        "output": _MISSING_TOOL_OUTPUT,
    }
    assert messages[1]["call_id"] == "call_orphan"  # 清理不改写原始审计历史


def test_repair_responses_tool_pairs_keeps_complete_pairs_unchanged():
    items = [
        {"type": "function_call", "call_id": "call_ok", "name": "shell", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "call_ok", "output": "done"},
    ]
    assert _repair_responses_tool_pairs(items) == items
