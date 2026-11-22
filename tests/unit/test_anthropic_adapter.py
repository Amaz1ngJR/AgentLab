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


# ── 网关事件序列畸变的修复与重试 ──────────────────────────────────────────────
# SDK 的 accumulate_event 直接拿 event.index 当列表下标，网关少发
# content_block_start 或让 index 跳号就会在 SDK 内部抛 IndexError，
# 让整轮长任务作废。这里验证我们能在边界上修好，并把它标记为可重试。


def _malformed_stream(events):
    from anthropic import NOT_GIVEN
    from anthropic.lib.streaming import MessageStream
    raw = MagicMock()
    raw.__iter__.return_value = iter(events)
    return MessageStream(raw, output_format=NOT_GIVEN)


def _message_start(content=None):
    from anthropic.types import Message, RawMessageStartEvent, Usage
    return RawMessageStartEvent(type="message_start", message=Message.model_construct(
        id="m", type="message", role="assistant", model="claude-test",
        content=content if content is not None else [],
        usage=Usage(input_tokens=5, output_tokens=0),
        stop_reason=None, stop_sequence=None))


def _block_start(index, text=""):
    from anthropic.types import RawContentBlockStartEvent, TextBlock
    return RawContentBlockStartEvent(type="content_block_start", index=index,
                                     content_block=TextBlock(type="text", text=text))


def _delta(index, text):
    from anthropic.types import RawContentBlockDeltaEvent, TextDelta
    return RawContentBlockDeltaEvent(type="content_block_delta", index=index,
                                     delta=TextDelta(type="text_delta", text=text))


def _block_stop(index):
    from anthropic.types import RawContentBlockStopEvent
    return RawContentBlockStopEvent(type="content_block_stop", index=index)


def _message_stop():
    from anthropic.types import RawMessageStopEvent
    return RawMessageStopEvent(type="message_stop")


@pytest.mark.parametrize("events", [
    # 缺 content_block_start，直接推 delta
    [_message_start(), _delta(0, "hi"), _message_stop()],
    # index 跳号：只有 block 0，却推 block 2 的增量
    [_message_start(), _block_start(0), _delta(2, "hi"), _block_stop(2), _message_stop()],
], ids=["missing_block_start", "index_gap"])
def test_malformed_gateway_sequence_is_repaired_not_raised(events):
    from app.models.anthropic_adapter import _iter_events_with_repaired_snapshot
    stream = _malformed_stream(events)
    consumed = [e.type for e in _iter_events_with_repaired_snapshot(stream)]
    assert consumed == [e.type for e in events]
    texts = [b.text for b in stream._MessageStream__final_message_snapshot.content]
    assert "hi" in texts


def test_normal_sequence_does_not_gain_placeholder_blocks():
    from app.models.anthropic_adapter import _iter_events_with_repaired_snapshot
    stream = _malformed_stream([
        _message_start(), _block_start(0), _delta(0, "hi"), _block_stop(0), _message_stop(),
    ])
    list(_iter_events_with_repaired_snapshot(stream))
    content = stream._MessageStream__final_message_snapshot.content
    assert len(content) == 1
    assert content[0].text == "hi"


def test_placeholder_blocks_match_the_increment_they_carry():
    """占位块必须和后续增量同类型，否则 SDK 会静默丢弃该增量。"""
    from app.models.anthropic_adapter import _placeholder_block_for
    assert _placeholder_block_for(None).type == "text"
    assert _placeholder_block_for("text_delta").type == "text"
    assert _placeholder_block_for("input_json_delta").type == "tool_use"
    assert _placeholder_block_for("thinking_delta").type == "thinking"


def test_unrepairable_stream_error_is_retryable():
    """超出修复范围的流畸变要转成 ProviderStreamError(retryable=True)。

    这类异常没有状态码，也不在重试层的文本白名单里；如果原样冒泡，
    classify_provider_error 会判成不可重试，让整轮长任务白跑。
    """
    from app.models.anthropic_adapter import (
        _iter_events_with_repaired_snapshot, AnthropicAdapter,
    )
    from app.models.openai_adapter import ProviderStreamError
    from app.models.provider_retry import classify_provider_error
    import anthropic.lib.streaming._messages as sdk_streaming

    def boom(*_args, **_kwargs):
        raise IndexError("list index out of range")

    original = sdk_streaming.accumulate_event
    sdk_streaming.accumulate_event = boom
    try:
        stream = _malformed_stream([_message_start(), _block_start(0), _delta(0, "hi")])
        with pytest.raises(ProviderStreamError) as caught:
            list(_iter_events_with_repaired_snapshot(stream))
    finally:
        sdk_streaming.accumulate_event = original

    assert caught.value.retryable is True
    # 有了显式标记，重试层就会按瞬时故障处理。
    assert classify_provider_error(caught.value).retryable is True


def test_unrelated_index_error_is_not_rewrapped():
    """业务代码里的 IndexError 不能被当成流畸变。"""
    from app.models.openai_adapter import ProviderStreamError
    from app.models.provider_retry import classify_provider_error

    try:
        [][0]
    except IndexError as exc:
        assert not isinstance(exc, ProviderStreamError)
        # 没有显式 retryable 标记，保持原有判定
        assert classify_provider_error(exc).retryable is False


def _tool_start(index, name, call_id):
    from anthropic.types import RawContentBlockStartEvent, ToolUseBlock
    return RawContentBlockStartEvent(type="content_block_start", index=index,
        content_block=ToolUseBlock(id=call_id, type="tool_use", name=name, input={}))


def _json_delta(index, partial):
    from anthropic.types import RawContentBlockDeltaEvent, InputJSONDelta
    return RawContentBlockDeltaEvent(type="content_block_delta", index=index,
        delta=InputJSONDelta(type="input_json_delta", partial_json=partial))


def test_null_content_from_gateway_is_repaired():
    """网关把 message_start.content 发成 null 时不能让 SDK 的 append 崩掉。"""
    from app.models.anthropic_adapter import _iter_events_with_repaired_snapshot
    stream = _malformed_stream([
        _message_start(content=None), _block_start(0), _delta(0, "hi"),
        _block_stop(0), _message_stop(),
    ])
    list(_iter_events_with_repaired_snapshot(stream))
    content = stream._MessageStream__final_message_snapshot.content
    assert [b.text for b in content] == ["hi"]


def test_parallel_tool_calls_keep_their_arguments():
    """多个并发 tool_use 的参数必须各自落在自己的块上，不能串位。"""
    from app.models.anthropic_adapter import _iter_events_with_repaired_snapshot
    stream = _malformed_stream([
        _message_start(),
        _tool_start(0, "shell", "t0"), _tool_start(1, "read_file", "t1"),
        _json_delta(0, '{"command":"ls"}'), _json_delta(1, '{"path":"a"}'),
        _block_stop(0), _block_stop(1), _message_stop(),
    ])
    list(_iter_events_with_repaired_snapshot(stream))
    blocks = stream._MessageStream__final_message_snapshot.content
    by_name = {b.name: b.input for b in blocks if b.type == "tool_use"}
    assert by_name == {"shell": {"command": "ls"}, "read_file": {"path": "a"}}


def test_missing_tool_use_block_start_fails_retryably_instead_of_losing_arguments():
    """工具调用缺 content_block_start 时无法还原 id/name。

    补一个无名占位块会让参数静默消失、模型以为调用过，比失败更难排查，
    因此必须显式报可重试错误，让上层重新要一份完整的流。
    """
    from app.models.anthropic_adapter import _iter_events_with_repaired_snapshot
    from app.models.openai_adapter import ProviderStreamError
    stream = _malformed_stream([
        _message_start(), _tool_start(0, "shell", "t0"),
        _json_delta(1, '{"command":"ls"}'), _message_stop(),
    ])
    with pytest.raises(ProviderStreamError) as caught:
        list(_iter_events_with_repaired_snapshot(stream))
    assert caught.value.retryable is True


def test_empty_stream_raises_readable_retryable_error():
    """网关返回空流时不能退化成无消息的 AssertionError。

    原实现回退到 SDK 的 get_final_message()，它内部 assert 快照非空，
    于是 CLI 上只剩 "AssertionError: (异常无消息)"，无法定位。
    """
    from app.models.anthropic_adapter import AnthropicAdapter
    from app.models.openai_adapter import ProviderStreamError
    from app.models.provider_retry import classify_provider_error

    stream = _malformed_stream([])
    cfg = _config()
    with patch("anthropic.Anthropic") as sdk:
        sdk.return_value.messages.stream.return_value.__enter__.return_value = stream
        with pytest.raises(ProviderStreamError) as caught:
            AnthropicAdapter(cfg).create_message([{"role": "user", "content": "hi"}])

    assert "未产出可用事件" in str(caught.value)
    assert caught.value.retryable is True
    assert classify_provider_error(caught.value).retryable is True


def test_accumulated_message_rejects_empty_snapshot():
    """快照为空时必须报可读错误，而不是回退到会 assert 的公开接口。"""
    from app.models.anthropic_adapter import _accumulated_message
    from app.models.openai_adapter import ProviderStreamError

    stream = _malformed_stream([])
    list(stream)  # 空流，快照保持 None
    with pytest.raises(ProviderStreamError) as caught:
        _accumulated_message(stream, events_seen=0)
    assert caught.value.retryable is True
    assert "0 个事件" in str(caught.value)
