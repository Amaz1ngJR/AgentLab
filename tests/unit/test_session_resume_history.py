"""恢复会话时按目标 provider 协议转换历史，避免换 provider 后整次请求被拒。"""
from app.agent.session_router import _portable_for

# ── 恢复会话时的历史转换 ──────────────────────────────────────────────────────
# 换 provider 不只有 /model switch 一条路：直接改 .env 的 ACTIVE_PROFILE 再重启，
# 会在 resume_or_new → switch 里原样读回旧 provider 的结构化条目。

class _Provider:
    def __init__(self, name):
        self.provider = name


class _Session:
    def __init__(self, provider):
        self.llm = _Provider(provider)


def _foreign_history():
    return [
        {"type": "function_call", "call_id": "c1", "name": "shell", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c1", "output": "ok"},
        {"role": "developer", "content": "指令"},
        {"role": "user", "content": "继续"},
    ]


def test_resume_converts_foreign_history_for_anthropic():
    converted = _portable_for(_Session("anthropic"), _foreign_history())
    assert {m["role"] for m in converted} <= {"user", "assistant"}


def test_resume_keeps_structured_history_for_openai():
    messages = _foreign_history()
    assert _portable_for(_Session("openai"), messages) == messages


def test_openai_wire_conversion_skips_oauth_instruction_and_private_metadata():
    from app.models.append_prefix import INSTRUCTION
    from app.models.openai_adapter import _convert_messages_to_responses_format

    messages = [
        {"type": "message", "role": "developer", "content": "OAuth rules", INSTRUCTION: True},
        {"type": "message", "role": "user", "content": "summary", "_agentlab_summary": True},
    ]
    assert _convert_messages_to_responses_format(messages) == [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "summary"}]},
    ]


def test_resume_leaves_clean_history_untouched():
    clean = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}]
    assert _portable_for(_Session("anthropic"), clean) == clean


def test_resume_handles_empty_history():
    assert _portable_for(_Session("anthropic"), []) == []
