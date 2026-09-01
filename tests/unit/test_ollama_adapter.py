"""Ollama 原生 /api/chat adapter 离线测试。"""
import json
from unittest.mock import MagicMock, patch

from app.config.schemas import LLMConfig
from app.models.ollama_adapter import OllamaAdapter, _to_ollama_messages
from app.models.protocol import ToolResult


def _cfg(enable_thinking: bool = False) -> LLMConfig:
    return LLMConfig(
        provider="ollama",
        model="qwen3:14b",
        base_url="http://127.0.0.1:11434/v1",
        api_key=None,
        auth_token=None,
        temperature=0.6,
        top_p=None,
        context_size=8192,
        timeout_seconds=120,
        stream=True,
        enable_thinking=enable_thinking,
        profile_name="local_qwen3_14b",
    )


def _response(chunks: list[dict]) -> MagicMock:
    response = MagicMock()
    response.__enter__.return_value = response
    response.iter_lines.return_value = [
        json.dumps(chunk, ensure_ascii=False).encode() for chunk in chunks
    ]
    return response


def test_native_adapter_streams_text_and_uses_profile_options():
    response = _response([
        {"model": "qwen3:14b", "message": {"thinking": "想"}, "done": False},
        {"model": "qwen3:14b", "message": {"content": "你好"}, "done": False},
        {
            "model": "qwen3:14b", "message": {}, "done": True,
            "done_reason": "stop", "prompt_eval_count": 12, "eval_count": 3,
        },
    ])
    thinking: list[str] = []
    text: list[str] = []

    with patch("requests.Session") as session_cls:
        session_cls.return_value.post.return_value = response
        adapter = OllamaAdapter(_cfg())
        result = adapter.create_message(
            [{"role": "user", "content": "介绍自己"}],
            on_thinking_delta=thinking.append,
            on_text_delta=text.append,
        )

    call = session_cls.return_value.post.call_args
    assert call.args[0] == "http://127.0.0.1:11434/api/chat"
    body = call.kwargs["json"]
    assert body["think"] is False
    assert body["stream"] is True
    assert body["options"]["num_ctx"] == 8192
    assert result.text == "你好"
    assert result.reasoning == "想"
    assert result.usage == {"input_tokens": 12, "output_tokens": 3}
    assert text == ["你好"]
    assert thinking == ["想"]


def test_native_adapter_tool_call_and_result_round_trip():
    response = _response([
        {
            "model": "qwen3:14b",
            "message": {"tool_calls": [{
                "function": {"name": "read_file", "arguments": {"path": "README.md"}},
            }]},
            "done": False,
        },
        {
            "model": "qwen3:14b", "message": {}, "done": True,
            "done_reason": "stop", "prompt_eval_count": 20, "eval_count": 5,
        },
    ])
    tool_schema = [{
        "name": "read_file",
        "description": "读文件",
        "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
    }]

    with patch("requests.Session") as session_cls:
        session_cls.return_value.post.return_value = response
        adapter = OllamaAdapter(_cfg())
        result = adapter.create_message(
            [{"role": "user", "content": "读 README"}], tools=tool_schema,
        )

    assert len(result.tool_calls) == 1
    call = result.tool_calls[0]
    assert call.name == "read_file"
    assert call.arguments == {"path": "README.md"}
    formatted = adapter.format_tool_results([
        ToolResult(tool_call_id=call.id, output="contents"),
    ])
    assert formatted[0]["tool_name"] == "read_file"
    replay = _to_ollama_messages(result.provider_payload + formatted)
    assert replay[0]["tool_calls"][0]["function"]["arguments"] == {
        "path": "README.md",
    }
    assert replay[1] == {
        "role": "tool", "content": "contents", "tool_name": "read_file",
    }


def test_native_message_normalizer_converts_openai_tool_history():
    messages = _to_ollama_messages([
        {
            "role": "assistant", "content": "", "tool_calls": [{
                "id": "c1", "type": "function",
                "function": {"name": "shell", "arguments": '{"command":"pwd"}'},
            }],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "C:/repo"},
    ])
    assert messages[0]["tool_calls"][0]["function"]["name"] == "shell"
    assert messages[1]["tool_name"] == "shell"

