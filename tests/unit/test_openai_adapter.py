"""离线测试：OpenAI Responses API adapter。用 mock 替代真实 HTTP 调用。

OpenAI Responses API 的关键差异需要 mock 反映:
  - client.responses.stream(...) 返回上下文管理器,支持 for event in stream
  - 流式事件 type 是 "response.output_text.delta" 这种串
  - stream.get_final_response() 返回 Response 对象,有 output (list of items)
    和 usage (.input_tokens / .output_tokens)
  - output item 类型: type=message 含 content (output_text part 列表),
    或 type=function_call (call_id, name, arguments JSON 字符串)
"""
import pytest
from unittest.mock import MagicMock, patch

from app.config.schemas import LLMConfig
from app.attachments import build_user_content
from app.models.openai_adapter import (
    OpenAIAdapter,
    ProviderStreamError,
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


def _refusal_item(text: str) -> MagicMock:
    part = MagicMock()
    part.type = "refusal"
    part.refusal = text
    item = MagicMock()
    item.type = "message"
    item.content = [part]
    item.model_dump.return_value = {
        "type": "message", "role": "assistant",
        "content": [{"type": "refusal", "refusal": text}],
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


def _reasoning_item() -> MagicMock:
    item = MagicMock()
    item.type = "reasoning"
    item.model_dump.return_value = {"type": "reasoning", "summary": []}
    return item


def _delta_event(text: str) -> MagicMock:
    """构造 response.output_text.delta 流式事件。"""
    ev = MagicMock()
    ev.type = "response.output_text.delta"
    ev.delta = text
    return ev


def _reasoning_delta_event(text: str, *, summary: bool = False) -> MagicMock:
    ev = MagicMock()
    ev.type = (
        "response.reasoning_summary_text.delta"
        if summary else "response.reasoning_text.delta"
    )
    ev.delta = text
    return ev


def _output_item_done_event(item: MagicMock, index: int = 0) -> MagicMock:
    event = MagicMock()
    event.type = "response.output_item.done"
    event.item = item
    event.output_index = index
    return event


def _completed_event(final: MagicMock) -> MagicMock:
    event = MagicMock()
    event.type = "response.completed"
    event.response = final
    return event


def _function_args_delta_event(text: str) -> MagicMock:
    ev = MagicMock()
    ev.type = "response.function_call_arguments.delta"
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


def test_reasoning_only_response_retries_with_lower_effort():
    cfg = _cfg()
    cfg.reasoning_effort = "high"
    with patch("openai.OpenAI") as MockOpenAI:
        client = MockOpenAI.return_value
        client.responses.stream.side_effect = [
            _FakeStream([], _build_final([_reasoning_item()], out_tokens=89)),
            _FakeStream([], _build_final([_message_item("OK")], out_tokens=6)),
        ]
        response = OpenAIAdapter(cfg).create_message(
            messages=[{"role": "user", "content": "只回复 OK"}],
            max_tokens=512,
        )

    assert response.text == "OK"
    assert response.usage == {"input_tokens": 24, "output_tokens": 95}
    assert response.provider_payload[0]["type"] == "message"
    assert client.responses.stream.call_args_list[0].kwargs["reasoning"] == {"effort": "high"}
    assert client.responses.stream.call_args_list[1].kwargs["reasoning"] == {"effort": "low"}


def test_empty_final_output_recovers_completed_stream_item():
    with patch("openai.OpenAI") as MockOpenAI:
        client = MockOpenAI.return_value
        client.responses.stream.return_value = _FakeStream(
            [_output_item_done_event(_message_item("OK"))], _build_final([], out_tokens=9),
        )
        response = OpenAIAdapter(_cfg()).create_message(
            messages=[{"role": "user", "content": "test"}],
        )

    assert response.text == "OK"
    assert response.provider_payload[0]["type"] == "message"
    assert client.responses.stream.call_count == 1


def test_empty_final_output_recovers_structured_tool_call():
    tool_item = _function_call_item("call-1", "edit_file", '{"path":"a.py"}')
    with patch("openai.OpenAI") as MockOpenAI:
        client = MockOpenAI.return_value
        client.responses.stream.return_value = _FakeStream(
            [_output_item_done_event(tool_item)], _build_final([], out_tokens=12),
        )
        response = OpenAIAdapter(_cfg()).create_message(
            messages=[{"role": "user", "content": "修复代码"}],
            tools=[{
                "name": "edit_file", "description": "Edit a file",
                "input_schema": {"type": "object", "properties": {}},
            }],
        )

    assert response.tool_calls[0].name == "edit_file"
    assert response.tool_calls[0].arguments == {"path": "a.py"}
    assert response.provider_payload[0]["type"] == "function_call"
    assert client.responses.stream.call_count == 1


def test_empty_final_output_with_reasoning_usage_retries_lower_effort():
    cfg = _cfg()
    cfg.reasoning_effort = "high"
    with patch("openai.OpenAI") as MockOpenAI:
        client = MockOpenAI.return_value
        client.responses.stream.side_effect = [
            _FakeStream([_reasoning_delta_event("思考中")], _build_final([], out_tokens=99)),
            _FakeStream([], _build_final([_message_item("OK")], out_tokens=8)),
        ]
        response = OpenAIAdapter(cfg).create_message(
            messages=[{"role": "user", "content": "test"}],
        )

    assert response.text == "OK"
    assert response.usage["output_tokens"] == 107
    assert client.responses.stream.call_args_list[1].kwargs["reasoning"] == {"effort": "none"}


def test_empty_final_output_still_empty_after_fallback_reports_state():
    cfg = _cfg()
    cfg.reasoning_effort = "high"
    with patch("openai.OpenAI") as MockOpenAI:
        client = MockOpenAI.return_value
        client.responses.stream.side_effect = [
            _FakeStream([], _build_final([], out_tokens=99)),
            _FakeStream([], _build_final([], out_tokens=30)),
        ]
        with pytest.raises(ProviderStreamError, match="空输出") as exc:
            OpenAIAdapter(cfg).create_message(messages=[{"role": "user", "content": "test"}])

    assert "output_types=[]" in str(exc.value)
    assert client.responses.stream.call_count == 2


def test_streamed_text_survives_missing_final_message_item():
    cfg = _cfg()
    cfg.reasoning_effort = "high"
    with patch("openai.OpenAI") as MockOpenAI:
        client = MockOpenAI.return_value
        client.responses.stream.return_value = _FakeStream(
            [_delta_event("OK")], _build_final([_reasoning_item()]),
        )
        response = OpenAIAdapter(cfg).create_message(
            messages=[{"role": "user", "content": "test"}],
        )

    assert response.text == "OK"
    assert response.provider_payload[-1]["content"][0]["text"] == "OK"
    assert client.responses.stream.call_count == 1


def test_persistently_reasoning_only_response_reports_provider_state():
    cfg = _cfg()
    cfg.reasoning_effort = "high"
    with patch("openai.OpenAI") as MockOpenAI:
        client = MockOpenAI.return_value
        client.responses.stream.side_effect = [
            _FakeStream([], _build_final([_reasoning_item()])),
            _FakeStream([], _build_final([_reasoning_item()])),
        ]
        with pytest.raises(ProviderStreamError, match="仅返回推理内容") as exc:
            OpenAIAdapter(cfg).create_message(messages=[{"role": "user", "content": "test"}])

    assert "status=completed" in str(exc.value)
    assert "output_types=['reasoning']" in str(exc.value)
    assert client.responses.stream.call_count == 2


def test_refusal_is_returned_as_visible_text_without_reasoning_retry():
    cfg = _cfg()
    cfg.reasoning_effort = "high"
    with patch("openai.OpenAI") as MockOpenAI:
        client = MockOpenAI.return_value
        client.responses.stream.return_value = _FakeStream(
            [], _build_final([_reasoning_item(), _refusal_item("无法协助")]),
        )
        response = OpenAIAdapter(cfg).create_message(
            messages=[{"role": "user", "content": "test"}],
        )

    assert response.text == "无法协助"
    assert client.responses.stream.call_count == 1


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


def test_cumulative_text_snapshots_are_not_duplicated():
    final = _build_final([_message_item("目前可确定：蓝屏无信号对应容器退出")])
    stream = _FakeStream(events=[
        _delta_event("目前可确定：蓝屏无信号"),
        _delta_event("目前可确定：蓝屏无信号对应容器退出"),
    ], final=final)
    seen = []
    with patch("openai.OpenAI") as MockOpenAI:
        client = MagicMock()
        MockOpenAI.return_value = client
        client.responses.stream.return_value = stream
        OpenAIAdapter(_cfg()).create_message(
            messages=[{"role": "user", "content": "分析"}],
            on_text_delta=seen.append,
        )
    assert seen == ["目前可确定：蓝屏无信号", "对应容器退出"]
    assert "".join(seen) == "目前可确定：蓝屏无信号对应容器退出"


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

    assert progress_log[0] == {"input_tokens": 0, "output_tokens": 0}
    assert len(progress_log) >= 2
    # 最终值应该是真实 usage,不是估算
    final_progress = progress_log[-1]
    assert final_progress["input_tokens"] == 20
    assert final_progress["output_tokens"] == 4


def test_streaming_progress_includes_reasoning_and_tool_arguments():
    """长时间 reasoning/tool-call 阶段也应持续增长，而不是固定在输入 token。"""
    final = _build_final([_message_item("ok")], in_tokens=30, out_tokens=20)
    stream = _FakeStream(events=[
        _reasoning_delta_event("推理第一段"),
        _reasoning_delta_event("推理第二段", summary=True),
        _function_args_delta_event('{"path":"README.md"}'),
    ], final=final)
    progress_log = []
    thinking = []
    with patch("openai.OpenAI") as MockOpenAI:
        client = MagicMock()
        MockOpenAI.return_value = client
        client.responses.stream.return_value = stream
        OpenAIAdapter(_cfg()).create_message(
            messages=[{"role": "user", "content": "分析"}],
            on_progress=progress_log.append,
            on_thinking_delta=thinking.append,
        )
    live_values = [entry["output_tokens"] for entry in progress_log[:-1]]
    assert len(set(live_values)) >= 3
    assert "".join(thinking) == "推理第一段推理第二段"
    assert progress_log[-1] == {
        "input_tokens": 30, "output_tokens": 20, "final": True,
    }


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


def test_codex_metadata_before_created_falls_back_to_raw_stream():
    """网关的 codex.* 前置事件绕过 SDK 状态机，由原始流兼容解析。"""
    class BrokenStream:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def __iter__(self):
            raise RuntimeError(
                "Expected to have received `response.created` before `codex.response.metadata`"
            )
            yield  # pragma: no cover

    with patch("openai.OpenAI") as MockOpenAI:
        client = MagicMock()
        MockOpenAI.return_value = client
        client.responses.stream.return_value = BrokenStream()
        final = _build_final([_message_item("OK")])
        metadata = MagicMock()
        metadata.type = "codex.response.metadata"
        client.responses.create.return_value = iter([
            metadata,
            _output_item_done_event(_message_item("OK")),
            _completed_event(final),
        ])
        adapter = OpenAIAdapter(_cfg())

        response = adapter.create_message(messages=[{"role": "user", "content": "hello"}])

    assert response.text == "OK"
    assert client.responses.create.call_args.kwargs["stream"] is True


def test_standard_stream_order_error_is_not_hidden():
    class BrokenStream:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def __iter__(self):
            raise RuntimeError(
                "Expected to have received `response.created` before `response.output_item.added`"
            )
            yield  # pragma: no cover

    with patch("openai.OpenAI") as MockOpenAI:
        client = MockOpenAI.return_value
        client.responses.stream.return_value = BrokenStream()
        with pytest.raises(ProviderStreamError, match="流式协议异常"):
            OpenAIAdapter(_cfg()).create_message(
                messages=[{"role": "user", "content": "hello"}],
            )


def test_convert_messages_filters_unsupported_fields():
    """确保转换过程中过滤掉不支持的字段，如 status、parsed_arguments 等。
    
    这修复了 'Unknown parameter: input[N].status' 的 400 错误。
    """
    messages = [
        # 已经是 Responses API 格式，但包含不支持的字段
        {
            "type": "function_call",
            "call_id": "call_123",
            "name": "read_file",
            "arguments": '{"path": "test.py"}',
            "status": "completed",  # 不支持的字段
            "parsed_arguments": {"path": "test.py"},  # 不支持的字段
        },
        {
            "type": "function_call_output",
            "call_id": "call_123",
            "output": "file content",
            "status": "success",  # 不支持的字段
        },
        # 普通消息格式
        {"role": "user", "content": "继续"},
    ]
    
    converted = _convert_messages_to_responses_format(messages)
    
    # 验证不支持的字段被移除
    assert "status" not in converted[0]
    assert "parsed_arguments" not in converted[0]
    assert "status" not in converted[1]
    
    # 验证支持的字段保留
    assert converted[0]["type"] == "function_call"
    assert converted[0]["call_id"] == "call_123"
    assert converted[0]["name"] == "read_file"
    assert converted[0]["arguments"] == '{"path": "test.py"}'
    
    assert converted[1]["type"] == "function_call_output"
    assert converted[1]["call_id"] == "call_123"
    assert converted[1]["output"] == "file content"
    
    # 验证普通消息正确转换
    assert converted[2]["type"] == "message"
    assert converted[2]["role"] == "user"
