"""压缩模型超时/网络异常时本地兜底测试。"""
from app.agent.context import ContextManager
from app.agent.context_compaction import ContextCompressor
from app.agent.context_budget import ContextBudget
from tests.support.context import _long_messages


class TimeoutLLM:
    def create_message(self, *args, **kwargs):
        raise TimeoutError("read operation timed out")


def test_timeout_uses_local_fallback_and_replaces_history():
    manager = ContextManager(
        ContextBudget.from_context_size(context_size=256_000),
        ContextCompressor(TimeoutLLM(), allow_local_fallback=True),
        keep_recent=4,
    )
    messages = _long_messages(20)
    assert manager.maybe_compact(messages, force=True)
    assert len(messages) == 13
    assert [m for m in messages[1:] if m.get("role") == "user"] == [
        m for m in _long_messages(20) if m.get("role") == "user"
    ]
    assert manager.last_summary is not None
    assert "本地兜底" in manager.last_summary.summary["current_state"]
