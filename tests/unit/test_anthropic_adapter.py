"""Anthropic 图片 file 引用转换与跨 provider 历史归一化测试。"""
import json

from PIL import Image
import pytest
from unittest.mock import MagicMock, patch

from app import attachments
from app.attachments import AttachmentStore, build_user_content
from app.models.anthropic_adapter import (
    _materialize_anthropic_messages, _normalize_anthropic_messages,
)


def _sdk_stream(initial_content, *, tool=False):
    from anthropic import NOT_GIVEN
    from anthropic.lib.streaming import MessageStream
    from anthropic.types import (
        Message, RawMessageStartEvent, RawContentBlockStartEvent,
        RawContentBlockDeltaEvent, RawContentBlockStopEvent, RawMessageStopEvent,
        TextBlock, TextDelta, ToolUseBlock, InputJSONDelta, Usage,
    )

    message = Message.model_construct(
        id="msg-test", type="message", role="assistant", model="claude-test",
        content=initial_content, usage=Usage(input_tokens=10, output_tokens=0),
        stop_reason=None, stop_sequence=None,
    )
    block = (ToolUseBlock(id="tool-1", type="tool_use", name="read_file", input={})
             if tool else TextBlock(type="text", text=""))
    delta = (InputJSONDelta(type="input_json_delta", partial_json='{"path":"README.md"}')
             if tool else TextDelta(type="text_delta", text="OK"))
    raw = MagicMock()
    raw.__iter__.return_value = iter([
        RawMessageStartEvent(type="message_start", message=message),
        RawContentBlockStartEvent(type="content_block_start", index=0, content_block=block),
        RawContentBlockDeltaEvent(type="content_block_delta", index=0, delta=delta),
        RawContentBlockStopEvent(type="content_block_stop", index=0),
        RawMessageStopEvent(type="message_stop"),
    ])
    return MessageStream(raw, output_format=NOT_GIVEN), raw


def _config():
    from app.config.schemas import LLMConfig
    return LLMConfig(provider="anthropic", model="claude-test", base_url=None,
                     api_key="fake", auth_token=None, temperature=0.2, top_p=None,
                     context_size=8192, timeout_seconds=30, stream=True)


def test_fable_52_does_not_send_nondefault_temperature():
    from app.models.anthropic_adapter import AnthropicAdapter
    cfg = _config()
    cfg.model = "claude-fable-5-2"
    stream, _ = _sdk_stream([])
    with patch("anthropic.Anthropic") as sdk:
        sdk.return_value.messages.stream.return_value = stream
        AnthropicAdapter(cfg).create_message([])
        assert "temperature" not in sdk.return_value.messages.stream.call_args.kwargs
        assert cfg.temperature == 0.2


@pytest.mark.parametrize("streaming", [False, True])
def test_explicit_temperature_rejection_retries_once_without_parameter(streaming):
    import httpx
    from types import SimpleNamespace
    from anthropic import BadRequestError
    from app.models.anthropic_adapter import AnthropicAdapter
    error = BadRequestError("claude-test does not support non-default temperature",
        response=httpx.Response(400, request=httpx.Request("POST", "https://example.test/v1/messages")), body={})
    with patch("anthropic.Anthropic") as sdk:
        adapter = AnthropicAdapter(_config())
        if streaming:
            response, _ = _sdk_stream([])
            method = sdk.return_value.messages.stream
            method.side_effect = [error, response]
            result = adapter.create_message([]).text
        else:
            method = sdk.return_value.messages.create
            method.side_effect = [error, SimpleNamespace(content=[SimpleNamespace(type="text", text="OK")])]
            result = adapter.chat([])
        assert result == "OK"
        assert len(method.call_args_list) == 2
        assert method.call_args_list[0].kwargs["temperature"] == 0.2
        assert "temperature" not in method.call_args_list[1].kwargs
        assert adapter._omit_temperature is True


def test_unrelated_bad_request_is_not_retried():
    import httpx
    from anthropic import BadRequestError
    from app.models.anthropic_adapter import AnthropicAdapter
    error = BadRequestError("invalid messages", response=httpx.Response(400,
        request=httpx.Request("POST", "https://example.test/v1/messages")), body={})
    with patch("anthropic.Anthropic") as sdk:
        sdk.return_value.messages.create.side_effect = error
        with pytest.raises(BadRequestError):
            AnthropicAdapter(_config()).chat([])
        assert sdk.return_value.messages.create.call_count == 1


@pytest.mark.parametrize("initial_content", [None, []])
@pytest.mark.parametrize("tool", [False, True])
def test_null_initial_content_with_real_sdk_does_not_require_a_retry(initial_content, tool):
    from app.models.anthropic_adapter import AnthropicAdapter
    stream, raw = _sdk_stream(initial_content, tool=tool)
    with patch("anthropic.Anthropic") as sdk:
        client = sdk.return_value
        client.messages.stream.return_value = stream
        result = AnthropicAdapter(_config()).create_message([])
    if tool:
        assert result.tool_calls[0].name == "read_file"
        assert result.tool_calls[0].arguments == {"path": "README.md"}
    else:
        assert result.text == "OK"
    assert result.usage["input_tokens"] == 10
    client.messages.stream.assert_called_once()
    client.messages.create.assert_not_called()
    raw.close.assert_called_once()


def test_callback_attribute_error_is_not_swallowed_or_retried():
    from app.models.anthropic_adapter import AnthropicAdapter
    stream, raw = _sdk_stream(None)

    def broken_callback(text):
        value = None
        value.append(text)

    with patch("anthropic.Anthropic") as sdk:
        client = sdk.return_value
        client.messages.stream.return_value = stream
        with pytest.raises(AttributeError, match="append"):
            AnthropicAdapter(_config()).create_message([], on_text_delta=broken_callback)
    client.messages.stream.assert_called_once()
    client.messages.create.assert_not_called()
    raw.close.assert_called_once()


def test_anthropic_file_image_materializes_to_base64(tmp_path, monkeypatch):
    monkeypatch.setattr(attachments, "DEFAULT_ATTACHMENT_ROOT", tmp_path)
    path = tmp_path / "shot.png"
    Image.new("RGB", (5, 4), "green").save(path)
    attachment = AttachmentStore(tmp_path).add_path(
        "s1", path, workspace_root=tmp_path,
    )
    messages = [{
        "role": "user", "content": build_user_content("分析", [attachment]),
    }]
    converted = _materialize_anthropic_messages(messages)
    image = converted[0]["content"][1]
    assert image["type"] == "image"
    assert image["source"]["type"] == "base64"
    assert image["source"]["media_type"] == "image/png"
    assert image["source"]["data"]
    # 转换不修改持久化历史中的 file 引用。
    assert messages[0]["content"][1]["source"]["type"] == "file"


# ── 跨 provider 历史归一化 ────────────────────────────────────────────────────
# 会话在 Responses / ChatGPT / Ollama 之间迁移后，历史里会残留 function_call、
# function_call_output、reasoning、developer 等 Anthropic 不认的条目。任何一个
# 漏出去，网关都会整体 400：Input should be 'user', 'assistant', 'system'。


def _foreign_history():
    return [
        {"type": "function_call", "call_id": "c1", "name": "shell",
         "arguments": '{"command":"ls"}'},
        {"type": "function_call_output", "call_id": "c1", "output": "a.txt"},
        {"type": "reasoning", "summary": [{"type": "summary_text", "text": "思考"}]},
        {"type": "message", "role": "assistant",
         "content": [{"type": "output_text", "text": "看完了"}]},
        {"role": "developer", "content": "运行时指令"},
        {"role": "user", "content": "继续"},
    ]


def test_foreign_provider_history_only_yields_anthropic_roles():
    system, converted = _normalize_anthropic_messages(_foreign_history())
    # 关键断言：没有任何非法 role 会被发给网关
    assert {m["role"] for m in converted} <= {"user", "assistant"}
    # developer 提升为顶层 system，而不是留在 messages 里
    assert system == "运行时指令"
    assert all("developer" not in json.dumps(m, ensure_ascii=False) for m in converted)


def test_foreign_tool_calls_become_anthropic_blocks():
    _, converted = _normalize_anthropic_messages(_foreign_history())
    tool_uses = [b for m in converted for b in m["content"] if b.get("type") == "tool_use"]
    tool_results = [b for m in converted for b in m["content"] if b.get("type") == "tool_result"]
    assert tool_uses == [{"type": "tool_use", "id": "c1", "name": "shell",
                          "input": {"command": "ls"}}]
    assert tool_results[0]["tool_use_id"] == "c1"
    assert tool_results[0]["content"] == "a.txt"


def test_reasoning_items_are_dropped_from_replayed_input():
    _, converted = _normalize_anthropic_messages(_foreign_history())
    assert not any(b.get("type") == "reasoning"
                   for m in converted for b in m["content"])


def test_adjacent_same_role_messages_are_merged_for_alternation():
    messages = [
        {"role": "user", "content": "第一句"},
        {"role": "user", "content": "第二句"},
        {"role": "assistant", "content": "收到"},
        {"role": "user", "content": "继续"},
    ]
    _, converted = _normalize_anthropic_messages(messages)
    assert [m["role"] for m in converted] == ["user", "assistant", "user"]
    assert [b["text"] for b in converted[0]["content"]] == ["第一句", "第二句"]


def test_openai_chat_tool_role_becomes_tool_result():
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c9", "type": "function",
             "function": {"name": "read_file", "arguments": '{"path":"a"}'}}]},
        {"role": "tool", "tool_call_id": "c9", "content": "内容"},
        {"role": "user", "content": "继续"},
    ]
    _, converted = _normalize_anthropic_messages(messages)
    assert {m["role"] for m in converted} <= {"user", "assistant"}
    blocks = [b for m in converted for b in m["content"]]
    assert any(b.get("type") == "tool_use" and b["name"] == "read_file" for b in blocks)
    assert any(b.get("type") == "tool_result" and b["tool_use_id"] == "c9" for b in blocks)


def test_clean_anthropic_history_is_preserved():
    messages = [
        {"role": "system", "content": "系统提示"},
        {"role": "user", "content": "问题"},
        {"role": "assistant", "content": [{"type": "text", "text": "回答"}]},
    ]
    system, converted = _normalize_anthropic_messages(messages)
    assert system == "系统提示"
    assert [m["role"] for m in converted] == ["user", "assistant"]
    assert converted[1]["content"] == [{"type": "text", "text": "回答"}]
