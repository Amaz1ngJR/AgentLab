"""Context summaries round-trip through real SQLite storage."""
import json

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
