"""离线测试：OpenAI-compatible adapter 流式工具循环。用 mock 替代真实 HTTP 调用。"""
from unittest.mock import MagicMock, patch

from app.models.compatible_adapter import OpenAICompatibleAdapter
from app.config.schemas import LLMConfig


def _cfg(enable_thinking: bool = False):
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
        enable_thinking=enable_thinking,
    )


def _chunk(*, content=None, tool_calls=None, finish_reason=None, usage=None, reasoning=None):
    """构造单个流式 chunk。"""
    chunk = MagicMock()
    if usage is not None:
        chunk.usage = MagicMock()
        chunk.usage.prompt_tokens = usage[0]
        chunk.usage.completion_tokens = usage[1]
    else:
        chunk.usage = None

    if content is None and tool_calls is None and finish_reason is None and reasoning is None:
        chunk.choices = []
        return chunk

    choice = MagicMock()
    choice.finish_reason = finish_reason
    delta = MagicMock()
    delta.content = content
    delta.tool_calls = tool_calls
    # MagicMock 默认对任意属性返回真值,会让 adapter 误以为有推理内容;
    # 显式置为传入值(默认 None),只有想测思考流时才给字符串。
    delta.reasoning_content = reasoning
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


def test_create_message_normalizes_null_message_content():
    """Ollama 不接受 JSON null content，助手工具调用历史也需要规范化。"""
    with patch("openai.OpenAI") as MockOpenAI:
        mock_client = MagicMock()
        MockOpenAI.return_value = mock_client
        mock_client.chat.completions.create.return_value = _stream_text("done")

        adapter = OpenAICompatibleAdapter(_cfg())
        adapter.create_message(messages=[
            {"role": "assistant", "content": None, "tool_calls": [{"id": "c1"}]},
            {"role": "tool", "tool_call_id": "c1", "content": "ok"},
        ])

    sent = mock_client.chat.completions.create.call_args.kwargs["messages"]
    assert sent[0]["content"] == ""
    assert sent[0]["tool_calls"] == [{"id": "c1"}]


def test_create_message_normalizes_mixed_provider_history():
    """切换到 Ollama 后应将 Responses API 历史转换为 Chat 格式。"""
    with patch("openai.OpenAI") as MockOpenAI:
        mock_client = MagicMock()
        MockOpenAI.return_value = mock_client
        mock_client.chat.completions.create.return_value = _stream_text("done")

        adapter = OpenAICompatibleAdapter(_cfg())
        adapter.create_message(messages=[
            {"role": "assistant", "type": "message", "content": [
                {"type": "output_text", "text": "准备调用工具"},
            ]},
            {"type": "function_call", "call_id": "c1", "name": "shell", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "c1", "output": "ok"},
            {"role": "assistant", "content": None},
        ])

    sent = mock_client.chat.completions.create.call_args.kwargs["messages"]
    assert sent[0] == {"role": "assistant", "content": "准备调用工具"}
    assert sent[1]["role"] == "assistant"
    assert sent[1]["content"] == ""
    assert sent[1]["tool_calls"][0]["function"]["name"] == "shell"
    assert sent[2] == {"role": "tool", "tool_call_id": "c1", "content": "ok"}
    assert sent[3] == {"role": "assistant", "content": ""}


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


def _stream_thinking(reasoning: str, answer: str, prompt_tokens=12, completion_tokens=6):
    """模拟深度思考模型:先吐 reasoning_content,再吐正式 content。"""
    chunks = [_chunk(reasoning=ch) for ch in reasoning]
    chunks += [_chunk(content=ch) for ch in answer]
    chunks.append(_chunk(finish_reason="stop"))
    chunks.append(_chunk(usage=(prompt_tokens, completion_tokens)))
    return iter(chunks)


def test_thinking_stream_separates_reasoning_from_answer():
    """enable_thinking 时:reasoning_content 走 on_thinking_delta + resp.reasoning,
    正式答案走 on_text_delta + resp.text,两者不串味,且推理不进对话历史。"""
    thinking_chunks: list[str] = []
    text_chunks: list[str] = []

    with patch("openai.OpenAI") as MockOpenAI:
        mock_client = MagicMock()
        MockOpenAI.return_value = mock_client
        mock_client.chat.completions.create.return_value = _stream_thinking("想一下", "答案")

        adapter = OpenAICompatibleAdapter(_cfg(enable_thinking=True))
        resp = adapter.create_message(
            messages=[{"role": "user", "content": "1+1"}],
            on_text_delta=text_chunks.append,
            on_thinking_delta=thinking_chunks.append,
        )

    # 推理与答案分别归位
    assert "".join(thinking_chunks) == "想一下"
    assert resp.reasoning == "想一下"
    assert "".join(text_chunks) == "答案"
    assert resp.text == "答案"
    # 推理过程不能混进 provider_payload(对话历史只回放最终答案)
    assert resp.provider_payload[0]["content"] == "答案"
    assert "想一下" not in resp.provider_payload[0]["content"]
    # enable_thinking 应通过 extra_body 透传给服务端
    kwargs = mock_client.chat.completions.create.call_args.kwargs
    assert kwargs.get("extra_body") == {"enable_thinking": True}


def test_enable_thinking_off_omits_extra_body():
    """默认(enable_thinking=False)不带 extra_body,避免给不认识它的端点塞参数。"""
    with patch("openai.OpenAI") as MockOpenAI:
        mock_client = MagicMock()
        MockOpenAI.return_value = mock_client
        mock_client.chat.completions.create.return_value = _stream_text("hi")

        adapter = OpenAICompatibleAdapter(_cfg(enable_thinking=False))
        resp = adapter.create_message(messages=[{"role": "user", "content": "hi"}])

    assert resp.reasoning == ""
    kwargs = mock_client.chat.completions.create.call_args.kwargs
    assert "extra_body" not in kwargs


# ── 兜底:本地小模型把工具调用当文本吐出来,从正文捞回 ─────────────────────────

_TOOLS = [{
    "name": "list_dir",
    "description": "列出目录内容",
    "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
}]


def _run_with_text(model_text: str, tools=_TOOLS):
    """让流只吐一段文本(无原生 tool_calls),返回 adapter 响应。"""
    with patch("openai.OpenAI") as MockOpenAI:
        mock_client = MagicMock()
        MockOpenAI.return_value = mock_client
        mock_client.chat.completions.create.return_value = _stream_text(model_text)

        adapter = OpenAICompatibleAdapter(_cfg())
        return adapter.create_message(
            messages=[{"role": "user", "content": "列出当前目录"}],
            tools=tools,
        )


def test_fallback_recovers_bare_json_tool_call():
    """模型漏掉 <tool_call> 标签,直接吐裸 JSON —— 应被捞回成 ToolCall。"""
    resp = _run_with_text('{"name": "list_dir", "arguments": {"path": "."}}')
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].name == "list_dir"
    assert resp.tool_calls[0].arguments == {"path": "."}
    # 正文是调用本身,捞回后清空,避免当散文渲染
    assert resp.text == ""
    # provider_payload 里要带上 tool_calls,回放历史才完整
    assert "tool_calls" in resp.provider_payload[0]


def test_fallback_recovers_tagged_tool_call():
    """带 <tool_call> 标签但 Ollama 没解析进 tool_calls 字段的情况。"""
    resp = _run_with_text('<tool_call>\n{"name": "list_dir", "arguments": {"path": "src"}}\n</tool_call>')
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].arguments == {"path": "src"}


def test_fallback_recovers_fenced_json_tool_call():
    """套了 markdown ```json 围栏的裸调用(日志里出现过的形态)。"""
    resp = _run_with_text('```json\n{"name": "list_dir", "arguments": {"path": "."}}\n```')
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].name == "list_dir"


def test_fallback_recovers_json_embedded_in_prose():
    """调用 JSON 夹在前后散文中间(7B 模型实际出现的形态)。"""
    text = (
        "好的，请稍等，我将读取并分析该目录下的文件。\n\n"
        '```json\n{"name": "list_dir", "arguments": {"path": "/Users/weidian/yjr/AgentLab"}}\n```\n\n'
        "请确认路径是否正确。"
    )
    resp = _run_with_text(text)
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].name == "list_dir"
    assert resp.tool_calls[0].arguments == {"path": "/Users/weidian/yjr/AgentLab"}


def test_fallback_handles_braces_inside_string_args():
    """参数值里含花括号时,括号配对扫描不应被带偏。"""
    resp = _run_with_text('{"name": "list_dir", "arguments": {"path": "a/{x}/b"}}')
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].arguments == {"path": "a/{x}/b"}


def test_fallback_recovers_multiple_tagged_calls():
    """一段正文里多个 <tool_call> 标签,全部捞回。"""
    text = (
        '<tool_call>{"name": "list_dir", "arguments": {"path": "a"}}</tool_call>'
        '<tool_call>{"name": "list_dir", "arguments": {"path": "b"}}</tool_call>'
    )
    resp = _run_with_text(text)
    assert [tc.arguments["path"] for tc in resp.tool_calls] == ["a", "b"]


def test_fallback_ignores_unknown_tool_name():
    """正文 JSON 的 name 不在工具表里 —— 不当作调用,保留为普通文本。"""
    resp = _run_with_text('{"name": "not_a_real_tool", "arguments": {}}')
    assert resp.tool_calls == []
    assert resp.text != ""


def test_fallback_ignores_plain_prose():
    """普通中文回答不含工具 JSON —— 不误判,原样作为文本返回。"""
    resp = _run_with_text("当前目录下有 app、tests 和 docs 三个子目录。")
    assert resp.tool_calls == []
    assert "app" in resp.text


def test_fallback_not_triggered_without_tools():
    """没提供工具时,即便正文长得像调用也不捞 —— 否则纯对话会被误伤。"""
    resp = _run_with_text('{"name": "list_dir", "arguments": {"path": "."}}', tools=None)
    assert resp.tool_calls == []
    assert resp.text != ""
