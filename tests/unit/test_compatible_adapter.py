"""离线测试：OpenAI-compatible adapter 流式工具循环。用 mock 替代真实 HTTP 调用。"""
from unittest.mock import MagicMock, patch

from app.models.compatible_adapter import OpenAICompatibleAdapter
from app.config.schemas import LLMConfig


def _cfg():
    return LLMConfig(
        provider="openai_compatible",
        model="qwen2.5-coder:7b-instruct",
        base_url="http://localhost:11434/v1",
        api_key="ollama",
        auth_token=None,
        temperature=0.2,
        top_p=None,
        context_size=None,
        timeout_seconds=30,
        stream=False,
    )


def _chunk(*, content=None, tool_calls=None, finish_reason=None, usage=None):
    """构造单个流式 chunk。"""
    chunk = MagicMock()
    if usage is not None:
        chunk.usage = MagicMock()
        chunk.usage.prompt_tokens = usage[0]
        chunk.usage.completion_tokens = usage[1]
    else:
        chunk.usage = None

    if content is None and tool_calls is None and finish_reason is None:
        chunk.choices = []
        return chunk

    choice = MagicMock()
    choice.finish_reason = finish_reason
    delta = MagicMock()
    delta.content = content
    delta.tool_calls = tool_calls
    choice.delta = delta
    chunk.choices = [choice]
    return chunk


def _tool_call_delta(*, index, id_=None, name=None, arguments=None):
    tc = MagicMock()
    tc.index = index
    tc.id = id_
    if name is not None or arguments is not None:
        tc.function = MagicMock()
        tc.function.name = name
        tc.function.arguments = arguments
    else:
        tc.function = None
    return tc


def _stream_tool_call(tool_id: str, tool_name: str, args_json: str, prompt_tokens=10, completion_tokens=5):
    """模拟一个分片到达的 tool_call 流。"""
    return iter([
        _chunk(tool_calls=[_tool_call_delta(index=0, id_=tool_id, name=tool_name)]),
        _chunk(tool_calls=[_tool_call_delta(index=0, arguments=args_json)]),
        _chunk(finish_reason="tool_calls"),
        _chunk(usage=(prompt_tokens, completion_tokens)),
    ])


def _stream_text(text: str, prompt_tokens=20, completion_tokens=8):
    """模拟分块到达的文本流。"""
    chunks = [_chunk(content=ch) for ch in text]
    chunks.append(_chunk(finish_reason="stop"))
    chunks.append(_chunk(usage=(prompt_tokens, completion_tokens)))
    return iter(chunks)


def test_create_message_tool_call():
    with patch("openai.OpenAI") as MockOpenAI:
        mock_client = MagicMock()
        MockOpenAI.return_value = mock_client
        mock_client.chat.completions.create.return_value = _stream_tool_call(
            "call_1", "read_file", '{"path": "README.md"}'
        )

        adapter = OpenAICompatibleAdapter(_cfg())
        resp = adapter.create_message(
            messages=[{"role": "user", "content": "读 README.md"}],
            tools=[{"name": "read_file", "description": "读文件", "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}],
        )

    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].name == "read_file"
    assert resp.tool_calls[0].arguments == {"path": "README.md"}
    assert resp.tool_calls[0].id == "call_1"
    assert resp.usage["input_tokens"] == 10
    assert resp.usage["output_tokens"] == 5


def test_create_message_text_only():
    with patch("openai.OpenAI") as MockOpenAI:
        mock_client = MagicMock()
        MockOpenAI.return_value = mock_client
        mock_client.chat.completions.create.return_value = _stream_text("你好！")

        adapter = OpenAICompatibleAdapter(_cfg())
        resp = adapter.create_message(messages=[{"role": "user", "content": "你好"}])

    assert resp.text == "你好！"
    assert resp.tool_calls == []
    assert resp.usage["input_tokens"] == 20
    assert resp.usage["output_tokens"] == 8


def test_provider_payload_structure():
    """provider_payload 是 list[dict],单元素是 OpenAI assistant message,可被 messages.extend 原样追加。"""
    with patch("openai.OpenAI") as MockOpenAI:
        mock_client = MagicMock()
        MockOpenAI.return_value = mock_client
        mock_client.chat.completions.create.return_value = _stream_tool_call(
            "call_2", "list_dir", "{}"
        )

        adapter = OpenAICompatibleAdapter(_cfg())
        resp = adapter.create_message(messages=[{"role": "user", "content": "列目录"}])

    payload = resp.provider_payload
    assert isinstance(payload, list) and len(payload) == 1
    msg = payload[0]
    assert msg["role"] == "assistant"
    assert "tool_calls" in msg
    assert msg["tool_calls"][0]["id"] == "call_2"


def test_format_tool_results_emits_tool_role_messages():
    """format_tool_results 把 ToolResult 列表转成 OpenAI Chat 风格的 role=tool 消息列表。

    Chat Completions 协议每个工具结果一条独立消息,不像 Anthropic 把多个块包在一条 user 里。
    """
    from app.models.protocol import ToolResult

    out = OpenAICompatibleAdapter.format_tool_results([
        ToolResult(tool_call_id="c1", output="hello", is_error=False),
        ToolResult(tool_call_id="c2", output="boom", is_error=True),
    ])
    assert len(out) == 2
    assert out[0] == {"role": "tool", "tool_call_id": "c1", "content": "hello"}
    # 错误时在 content 前加 ERROR 让模型识别
    assert out[1]["role"] == "tool"
    assert out[1]["tool_call_id"] == "c2"
    assert out[1]["content"].startswith("ERROR:")


def test_streaming_callbacks_are_invoked():
    """on_progress 和 on_text_delta 在流式过程中被多次调用。"""
    text_chunks: list[str] = []
    progress_updates: list[dict] = []

    with patch("openai.OpenAI") as MockOpenAI:
        mock_client = MagicMock()
        MockOpenAI.return_value = mock_client
        mock_client.chat.completions.create.return_value = _stream_text("hi")

        adapter = OpenAICompatibleAdapter(_cfg())
        adapter.create_message(
            messages=[{"role": "user", "content": "hi"}],
            on_progress=progress_updates.append,
            on_text_delta=text_chunks.append,
        )

    # 两个字符 + 收尾，至少两次 text 回调
    assert "".join(text_chunks) == "hi"
    # 至少有起始进度 + 最终 usage
    assert len(progress_updates) >= 2
    final = progress_updates[-1]
    assert final["input_tokens"] == 20
    assert final["output_tokens"] == 8
