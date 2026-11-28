"""Context summaries round-trip through real SQLite storage."""
import json
import pytest

from app.agent.context_compaction import ContextSummary
from app.storage import Storage
from tests.support.context import _VALID_SUMMARY


def test_storage_context_summary_round_trip(tmp_path):
    s = Storage(tmp_path / "t.db")
    s.create_session("sid", "default", "cloud_claude")
    rec = ContextSummary(summary=_VALID_SUMMARY, source_message_range=(0, 8),
                         source_run_ids=["r1"], token_count_before=500,
                         token_count_after=80, compression_model_profile="p").to_record()
    s.save_context_summary("sid", rec)
    rows = s.list_context_summaries("sid")
    assert len(rows) == 1
    assert rows[0]["range_start"] == 0 and rows[0]["range_end"] == 8
    assert rows[0]["token_before"] == 500
    latest = s.latest_context_summary("sid")
    assert latest is not None
    assert json.loads(latest["summary_json"])["user_goal"] == "重构登录模块"


def test_storage_context_summary_redacted(tmp_path):
    s = Storage(tmp_path / "t.db")
    s.create_session("sid", "default", "cloud_claude")
    leaky = dict(_VALID_SUMMARY)
    leaky["current_state"] = "sk-ant-api03-LEAKLEAKLEAKLEAK"
    rec = ContextSummary(summary=leaky, source_message_range=(0, 4)).to_record()
    s.save_context_summary("sid", rec)
    latest = s.latest_context_summary("sid")
    assert "LEAKLEAKLEAKLEAK" not in latest["summary_json"]


def test_storage_delete_session_clears_summaries(tmp_path):
    s = Storage(tmp_path / "t.db")
    s.create_session("sid", "default", "cloud_claude")
    s.save_context_summary("sid", ContextSummary(
        summary=_VALID_SUMMARY, source_message_range=(0, 4)).to_record())
    s.delete_session("sid")
    assert s.list_context_summaries("sid") == []


@pytest.mark.parametrize("clear_images", [None, "before_persist", "after_persist"])
def test_threshold_compaction_preserves_source_and_reloads_window(tmp_path, clear_images):
    from app.agent.context import ContextManager
    from app.agent.context_budget import ContextBudget
    from app.agent.context_compaction import ContextCompressor
    from app.agent.profiles import AgentProfile
    from app.agent.runtime import AgentSession
    from app.agent.session_router import SessionRouter
    from app.tools.registry import ToolRegistry
    from tests.support.delegation import FakeRouter, response

    budget = ContextBudget.from_context_size(200_000, auto_compact_token_limit=20_000)

    def make_router(storage):
        def factory(profile, sid):
            model = FakeRouter([])
            context = ContextManager(budget, ContextCompressor(FakeRouter([response(json.dumps(_VALID_SUMMARY))])))
            return AgentSession(model, ToolRegistry(), context_manager=context)
        return SessionRouter(storage, factory, {"default": AgentProfile("default", "Default", "codex")})

    path = tmp_path / "window.db"
    storage = Storage(path)
    router = make_router(storage)
    sid = router.new("default")
    history = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"round {i} " + "证据" * 1000}
               for i in range(16)]
    if clear_images:
        history[0]["content"] = [{"type": "text", "text": history[0]["content"]},
                                 {"type": "image", "source": {"type": "file", "path": "shot.png"}}]
    router.current.messages[:] = history
    ctx = router.current.context_manager
    assert budget.compact_threshold <= ctx.estimate(history) < budget.window_compact_threshold
    usage = []
    assert ctx.compact_before_model_call(router.current.messages, on_summary_usage=usage.append)
    assert usage and ctx.estimate(router.current.messages) < budget.compact_threshold
    if clear_images == "before_persist":
        router.clear_session_images()
    router.persist(sid)
    if clear_images == "after_persist":
        router.clear_session_images()
    summary_id = storage.latest_context_summary(sid)["id"]
    storage.close()

    storage = Storage(path)
    router = make_router(storage)
    assert router.switch(sid)
    assert any(message.get("_agentlab_summary") for message in router.current.messages)
    if clear_images:
        history[0] = {**history[0], "content": [history[0]["content"][0]]}
    assert storage.load_context_compaction_source(summary_id) == history
    storage.delete_session(sid)
    assert storage.load_context_compaction_source(summary_id) == []
    storage.close()
