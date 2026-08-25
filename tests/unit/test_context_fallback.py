"""压缩模型超时/网络异常时本地兜底测试。"""
from app.agent.context import ContextManager
from app.agent.context_compaction import ContextCompressor
from app.agent.context_budget import ContextBudget
from tests.unit.test_context import _long_messages


class TimeoutLLM:
    def create_message(self, *args, **kwargs):
        raise TimeoutError("read operation timed out")


def test_timeout_uses_local_fallback_and_replaces_history():
    manager = ContextManager(
        ContextBudget.from_model(declared_context_size=256_000),
        ContextCompressor(TimeoutLLM(), allow_local_fallback=True),
        keep_recent=4,
    )
    messages = _long_messages(20)
    assert manager.maybe_compact(messages, force=True)
    assert len(messages) == 5
    assert manager.last_summary is not None
    assert "本地兜底" in manager.last_summary.summary["current_state"]
