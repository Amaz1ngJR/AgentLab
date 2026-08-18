"""离线测试：Storage SQLite 层。"""
import json
import sqlite3
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


def test_message_redaction_preserves_json_escaping(tmp_path):
    """工具输出含凭据字段和引号时，脱敏后仍必须能完整恢复。"""
    s = _store(tmp_path)
    s.create_session("s1", "default", "cloud_claude")
    output = 'config: api_key="secret-value" auth_token=None; keep "quoted" text'
    msgs = [{
        "type": "function_call_output",
        "call_id": "call_1",
        "output": output,
    }]

    s.save_messages("s1", msgs)
    loaded = s.load_messages("s1")

    assert len(loaded) == 1
    assert loaded[0]["call_id"] == "call_1"
    assert "secret-value" not in loaded[0]["output"]
    assert 'keep "quoted" text' in loaded[0]["output"]


def test_load_messages_skips_legacy_invalid_json(tmp_path):
    s = _store(tmp_path)
    s.create_session("s1", "default", "cloud_claude")
    s.save_messages("s1", [{"role": "user", "content": "valid"}])
    s.conn.execute(
        "INSERT INTO messages(session_id,role,content,created_at) VALUES(?,?,?,?)",
        ("s1", "", '{"type":"function_call_output","output":"broken "quote""}', "now"),
    )
    s.conn.commit()

    assert s.load_messages("s1") == [{"role": "user", "content": "valid"}]


def test_delete_session_hard_removes(tmp_path):
    s = _store(tmp_path)
    s.create_session("s1", "default", "cloud_claude")
    s.save_messages("s1", [{"role": "user", "content": "hello"}])
    s.log_tool_execution("s1", "read_file", "{}", "x")
    s.delete_session("s1")
    # session、消息、审计都应抹掉;连 archived 列表也查不到
    assert s.get_session("s1") is None
    assert s.list_sessions(include_archived=True) == []
    assert s.load_messages("s1") == []


def test_delete_vs_archive(tmp_path):
    """archive 软删(数据留),delete 硬删(数据无)。"""
    s = _store(tmp_path)
    s.create_session("a", "default", "cloud_claude")
    s.create_session("b", "default", "cloud_claude")
    s.archive_session("a")
    s.delete_session("b")
    # a 软删:默认列表无,带 archived 能查到
    assert "a" not in [r["id"] for r in s.list_sessions()]
    assert "a" in [r["id"] for r in s.list_sessions(include_archived=True)]
    # b 硬删:彻底没了
    assert s.get_session("b") is None


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
    s.log_tool_execution(
        "s1",
        "read_file",
        '{"path":"x"}',
        "content",
        elapsed_seconds=0.1,
        risk="read",
        target_type="filesystem",
        scope="workspace",
        origin="builtin",
        approval_action="",
        outcome="completed",
    )
    rows = s.list_tool_executions("s1")
    assert len(rows) == 1
    assert rows[0]["tool_name"] == "read_file"
    assert rows[0]["risk"] == "read"
    assert rows[0]["target_type"] == "filesystem"
    assert rows[0]["scope"] == "workspace"
    assert rows[0]["origin"] == "builtin"
    assert rows[0]["outcome"] == "completed"


def test_tool_execution_schema_migrates_existing_database(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute(
        """CREATE TABLE tool_executions (
           id INTEGER PRIMARY KEY AUTOINCREMENT,
           session_id TEXT NOT NULL,
           tool_name TEXT NOT NULL,
           args_summary TEXT NOT NULL DEFAULT '',
           result_summary TEXT NOT NULL DEFAULT '',
           is_error INTEGER NOT NULL DEFAULT 0,
           elapsed_seconds REAL,
           created_at TEXT NOT NULL
        )"""
    )
    con.commit()
    con.close()

    store = Storage(path)
    columns = {
        row["name"]
        for row in store.conn.execute("PRAGMA table_info(tool_executions)").fetchall()
    }
    assert {
        "risk",
        "target_type",
        "scope",
        "origin",
        "host",
        "approval_action",
        "outcome",
        "requires_observation",
    }.issubset(columns)


def test_upsert_agent_profile(tmp_path):
    s = _store(tmp_path)
    s.upsert_agent_profile("coder", "代码助手", "cloud_claude", "read_write")
    rows = s.list_agent_profiles()
    assert rows[0]["id"] == "coder"
    assert rows[0]["memory_policy"] == "read_write"


def test_tasks_round_trip(tmp_path):
    """save_tasks / load_tasks 应保留顺序、状态、依赖、证据、history。"""
    s = _store(tmp_path)
    s.create_session("s1", "default", "cloud_claude")
    snapshot = [
        {"id": "t1", "content": "第一步", "status": "completed",
         "dependencies": [], "evidence": "done", "error": "",
         "history": ["-> in_progress (claimed)", "completed"]},
        {"id": "t2", "content": "第二步", "status": "pending",
         "dependencies": ["t1"], "evidence": "", "error": "", "history": []},
    ]
    s.save_tasks("s1", snapshot)
    loaded = s.load_tasks("s1")
    assert [t["id"] for t in loaded] == ["t1", "t2"]
    assert loaded[0]["status"] == "completed"
    assert loaded[0]["history"] == ["-> in_progress (claimed)", "completed"]
    assert loaded[1]["dependencies"] == ["t1"]


def test_save_tasks_overwrites(tmp_path):
    s = _store(tmp_path)
    s.create_session("s1", "default", "cloud_claude")
    s.save_tasks("s1", [{"id": "a", "content": "x", "status": "pending"}])
    s.save_tasks("s1", [{"id": "b", "content": "y", "status": "completed"}])
    loaded = s.load_tasks("s1")
    assert [t["id"] for t in loaded] == ["b"]  # 整表覆盖


def test_tasks_redacts_evidence(tmp_path):
    s = _store(tmp_path)
    s.create_session("s1", "default", "cloud_claude")
    s.save_tasks("s1", [{"id": "t1", "content": "x", "status": "completed",
                         "evidence": "token=sk-abcdefghijklmnopqrstuvwxyz0123"}])
    loaded = s.load_tasks("s1")
    assert "sk-abcdefghij" not in loaded[0]["evidence"]


def test_runs_logged_and_listed(tmp_path):
    s = _store(tmp_path)
    s.create_session("s1", "default", "cloud_claude")
    s.log_run("s1", "做两步任务", "completed", input_tokens=10, output_tokens=5)
    s.log_run("s1", "另一个目标", "blocked", input_tokens=3, output_tokens=1)
    runs = s.list_runs("s1")
    assert len(runs) == 2
    # 最新在前
    assert runs[0]["status"] == "blocked"
    assert runs[1]["status"] == "completed"
    assert runs[1]["input_tokens"] == 10


def test_delete_session_clears_tasks_and_runs(tmp_path):
    s = _store(tmp_path)
    s.create_session("s1", "default", "cloud_claude")
    s.save_tasks("s1", [{"id": "t1", "content": "x", "status": "pending"}])
    s.log_run("s1", "目标", "completed")
    s.delete_session("s1")
    assert s.load_tasks("s1") == []
    assert s.list_runs("s1") == []
