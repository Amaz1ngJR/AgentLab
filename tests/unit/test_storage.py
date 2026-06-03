"""离线测试：Storage SQLite 层。"""
import json
from app.storage import Storage


def _store(tmp_path):
    return Storage(tmp_path / "test.db")


def test_create_and_list_session(tmp_path):
    s = _store(tmp_path)
    s.create_session("s1", "default", "cloud_claude", "测试会话")
    rows = s.list_sessions()
    assert len(rows) == 1
    assert rows[0]["id"] == "s1"
    assert rows[0]["title"] == "测试会话"


def test_archive_removes_from_list(tmp_path):
    s = _store(tmp_path)
    s.create_session("s1", "default", "cloud_claude")
    s.archive_session("s1")
    assert s.list_sessions() == []
    assert s.list_sessions(include_archived=True)[0]["archived"] == 1


def test_messages_round_trip(tmp_path):
    s = _store(tmp_path)
    s.create_session("s1", "default", "cloud_claude")
    msgs = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}]
    s.save_messages("s1", msgs)
    loaded = s.load_messages("s1")
    assert loaded[0]["role"] == "user"
    assert loaded[1]["content"] == "hi"


def test_save_messages_overwrites(tmp_path):
    s = _store(tmp_path)
    s.create_session("s1", "default", "cloud_claude")
    s.save_messages("s1", [{"role": "user", "content": "old"}])
    s.save_messages("s1", [{"role": "user", "content": "new"}])
    loaded = s.load_messages("s1")
    assert len(loaded) == 1
    assert loaded[0]["content"] == "new"


def test_write_and_search_memory(tmp_path):
    s = _store(tmp_path)
    s.write_memory("用户喜欢简洁代码", scope="agent", agent_id="coder")
    s.write_memory("用户在北京", scope="user")
    results = s.search_memories("简洁", agent_id="coder")
    assert len(results) == 1
    assert "简洁" in results[0]["content"]


def test_memory_secret_redacted(tmp_path):
    s = _store(tmp_path)
    s.write_memory("api_key=sk-abcdefghijklmnopqrstuvwxyz0123", scope="session")
    results = s.search_memories("sk-")
    # redact 处理后不应有原始密钥
    for r in results:
        assert "sk-abcdefghij" not in r["content"]


def test_tool_execution_logged(tmp_path):
    s = _store(tmp_path)
    s.create_session("s1", "default", "cloud_claude")
    s.log_tool_execution("s1", "read_file", '{"path":"x"}', "content", elapsed_seconds=0.1)
    # 只验证不报错，审计写入成功


def test_upsert_agent_profile(tmp_path):
    s = _store(tmp_path)
    s.upsert_agent_profile("coder", "代码助手", "cloud_claude", "read_write")
    rows = s.list_agent_profiles()
    assert rows[0]["id"] == "coder"
    assert rows[0]["memory_policy"] == "read_write"
