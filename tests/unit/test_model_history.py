from app.models.history import make_history_portable


def test_make_history_portable_converts_all_provider_tool_shapes():
    messages = [
        {"role": "user", "content": "问题"},
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "先查看"}, {
                "type": "tool_use", "id": "a1", "name": "read_file",
                "input": {"path": "a.py"},
            }],
        },
        {"role": "user", "content": [{
            "type": "tool_result", "tool_use_id": "a1", "content": "A",
        }]},
        {"type": "function_call", "call_id": "o1", "name": "shell", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "o1", "output": "OK"},
    ]

    portable = make_history_portable(messages)

    assert [m["role"] for m in portable] == ["user", "assistant", "user", "assistant", "user"]
    joined = "\n".join(m["content"] for m in portable)
    assert "先查看" in joined
    assert "历史工具调用 read_file" in joined
    assert "历史工具结果 a1" in joined
    assert "历史工具调用 shell" in joined
    assert "历史工具结果 o1" in joined
