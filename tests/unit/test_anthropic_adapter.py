"""Anthropic 图片 file 引用转换测试。"""
from PIL import Image
import pytest
from unittest.mock import MagicMock, patch

from app import attachments
from app.attachments import AttachmentStore, build_user_content
from app.models.anthropic_adapter import _materialize_anthropic_messages


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
