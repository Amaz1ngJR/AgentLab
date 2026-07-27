"""SQLite 存储层 —— 会话、消息、记忆、工具执行审计的持久化。

设计原则（technical_architecture.md §10.3）：
- 标准库 sqlite3，无 ORM 依赖。
- 默认数据库路径 data/agentlab.db，不存在时自动创建。
- 所有写入都经 redact() 脱敏（密钥不进库）。
- 消息和工具输出体积可大，默认只存摘要；完整内容可选存。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from app.storage.loop_store import init_loop_tables
from app.util.redact import redact

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "agentlab.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_profiles (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    model_profile TEXT NOT NULL,
    memory_policy TEXT NOT NULL DEFAULT 'none',
    config_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    agent_id    TEXT NOT NULL DEFAULT 'default',
    title       TEXT NOT NULL DEFAULT '',
    model_profile TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    archived    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);

CREATE TABLE IF NOT EXISTS memories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scope       TEXT NOT NULL,
    agent_id    TEXT,
    session_id  TEXT,
    workspace   TEXT,
    content     TEXT NOT NULL,
    confidence  REAL NOT NULL DEFAULT 1.0,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tool_executions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    tool_name   TEXT NOT NULL,
    risk        TEXT NOT NULL DEFAULT '',
    target_type TEXT NOT NULL DEFAULT '',
    scope       TEXT NOT NULL DEFAULT '',
    origin      TEXT NOT NULL DEFAULT '',
    host        TEXT,
    approval_action TEXT NOT NULL DEFAULT '',
    outcome     TEXT NOT NULL DEFAULT 'completed',
    requires_observation INTEGER NOT NULL DEFAULT 0,
    args_summary TEXT NOT NULL DEFAULT '',
    result_summary TEXT NOT NULL DEFAULT '',
    is_error    INTEGER NOT NULL DEFAULT 0,
    elapsed_seconds REAL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    goal        TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT '',
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT NOT NULL,
    task_id      TEXT NOT NULL,
    content      TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'pending',
    dependencies TEXT NOT NULL DEFAULT '[]',
    evidence     TEXT NOT NULL DEFAULT '',
    error        TEXT NOT NULL DEFAULT '',
    history      TEXT NOT NULL DEFAULT '[]',
    position     INTEGER NOT NULL DEFAULT 0,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS context_summaries (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT NOT NULL,
    summary_json  TEXT NOT NULL DEFAULT '{}',
    range_start   INTEGER NOT NULL DEFAULT 0,
    range_end     INTEGER NOT NULL DEFAULT 0,
    source_run_ids TEXT NOT NULL DEFAULT '[]',
    token_before  INTEGER NOT NULL DEFAULT 0,
    token_after   INTEGER NOT NULL DEFAULT 0,
    compression_model_profile TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL
);
"""

_TOOL_EXECUTION_MIGRATIONS = {
    "risk": "TEXT NOT NULL DEFAULT ''",
    "target_type": "TEXT NOT NULL DEFAULT ''",
    "scope": "TEXT NOT NULL DEFAULT ''",
    "origin": "TEXT NOT NULL DEFAULT ''",
    "host": "TEXT",
    "approval_action": "TEXT NOT NULL DEFAULT ''",
    "outcome": "TEXT NOT NULL DEFAULT 'completed'",
    "requires_observation": "INTEGER NOT NULL DEFAULT 0",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _truncate(text: str, max_len: int = 500) -> str:
    return text[:max_len] + "…" if len(text) > max_len else text


class Storage:
    """SQLite 存储接口。每次操作自动提交。"""

    def __init__(self, db_path: Optional[Path] = None):
        self._path = db_path or DEFAULT_DB_PATH
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._con = sqlite3.connect(str(self._path), check_same_thread=False)
        self._con.row_factory = sqlite3.Row
        self._con.executescript(_SCHEMA)
        self._migrate_tool_executions()
        self._con.commit()
        # Loop Engineering 相关表(goal_specs/loop_runs/loop_iterations/
        # verification_results/worktrees/subagent_runs)。与上面的核心表共用同一连接,
        # 放在单独模块里建,避免主 _SCHEMA 越来越长。无 loop 功能时这些表只是空着。
        init_loop_tables(self._con)

    @property
    def conn(self) -> sqlite3.Connection:
        """暴露底层连接,供 loop_store 等模块复用同一个 SQLite 连接。"""
        return self._con

    def close(self) -> None:
        self._con.close()

    def _migrate_tool_executions(self) -> None:
        """为已有数据库补 ToolDescriptor 审计列。"""
        existing = {
            row["name"]
            for row in self._con.execute("PRAGMA table_info(tool_executions)").fetchall()
        }
        for name, declaration in _TOOL_EXECUTION_MIGRATIONS.items():
            if name not in existing:
                self._con.execute(
                    f"ALTER TABLE tool_executions ADD COLUMN {name} {declaration}"
                )

    @contextmanager
    def _tx(self):
        try:
            yield self._con
            self._con.commit()
        except Exception:
            self._con.rollback()
            raise

    # ── sessions ─────────────────────────────────────────────────────────────

    def create_session(self, session_id: str, agent_id: str,
                       model_profile: str, title: str = "") -> None:
        now = _now()
        with self._tx() as con:
            con.execute(
                "INSERT OR REPLACE INTO sessions VALUES (?,?,?,?,?,?,0)",
                (session_id, agent_id, title, model_profile, now, now),
            )

    def update_session_title(self, session_id: str, title: str) -> None:
        with self._tx() as con:
            con.execute(
                "UPDATE sessions SET title=?, updated_at=? WHERE id=?",
                (title, _now(), session_id),
            )

    def touch_session(self, session_id: str) -> None:
        """更新 session 的 updated_at 为当前时间(标记"最后活跃")。

        每次保存消息后调用,让 resume_or_new 的"最近"= 最后对话时间,而不是
        创建/重命名时间,避免每次启动恢复到一个从未对话过的空壳 session。
        """
        with self._tx() as con:
            con.execute(
                "UPDATE sessions SET updated_at=? WHERE id=?",
                (_now(), session_id),
            )

    def archive_session(self, session_id: str) -> None:
        with self._tx() as con:
            con.execute(
                "UPDATE sessions SET archived=1, updated_at=? WHERE id=?",
                (_now(), session_id),
            )

    def delete_session(self, session_id: str) -> None:
        """硬删除:把 session 及其消息、工具执行审计一并从库里抹掉,不可恢复。

        区别于 archive_session(只置 archived=1 软隐藏)。memories 不动 ——
        长期记忆按设计跨 session 留存,不随单个 session 删除。
        """
        with self._tx() as con:
            con.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
            con.execute("DELETE FROM tool_executions WHERE session_id=?", (session_id,))
            con.execute("DELETE FROM tasks WHERE session_id=?", (session_id,))
            con.execute("DELETE FROM runs WHERE session_id=?", (session_id,))
            con.execute("DELETE FROM context_summaries WHERE session_id=?", (session_id,))
            con.execute("DELETE FROM sessions WHERE id=?", (session_id,))

    def list_sessions(self, include_archived: bool = False) -> list[dict]:
        q = "SELECT * FROM sessions"
        if not include_archived:
            q += " WHERE archived=0"
        q += " ORDER BY updated_at DESC"
        rows = self._con.execute(q).fetchall()
        return [dict(r) for r in rows]

    def get_session(self, session_id: str) -> Optional[dict]:
        row = self._con.execute(
            "SELECT * FROM sessions WHERE id=?", (session_id,)
        ).fetchone()
        return dict(row) if row else None

    # ── messages ─────────────────────────────────────────────────────────────

    def save_messages(self, session_id: str,
                      messages: list[dict[str, Any]]) -> None:
        """保存完整消息历史（覆盖）：先删再批量插。"""
        with self._tx() as con:
            con.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
            now = _now()
            for m in messages:
                content = redact(json.dumps(m, ensure_ascii=False))
                con.execute(
                    "INSERT INTO messages(session_id,role,content,created_at) VALUES(?,?,?,?)",
                    (session_id, m.get("role", ""), content, now),
                )

    def load_messages(self, session_id: str) -> list[dict[str, Any]]:
        rows = self._con.execute(
            "SELECT content FROM messages WHERE session_id=? ORDER BY id",
            (session_id,),
        ).fetchall()
        result = []
        for r in rows:
            try:
                result.append(json.loads(r["content"]))
            except json.JSONDecodeError:
                pass
        return result

    def count_messages(self, session_id: str) -> int:
        """返回某 session 的消息条数。供 resume_or_new 跳过空会话。"""
        row = self._con.execute(
            "SELECT COUNT(*) AS c FROM messages WHERE session_id=?",
            (session_id,),
        ).fetchone()
        return int(row["c"]) if row else 0

    # ── memories ─────────────────────────────────────────────────────────────

    def write_memory(self, content: str, scope: str = "session",
                     agent_id: Optional[str] = None,
                     session_id: Optional[str] = None,
                     workspace: Optional[str] = None,
                     confidence: float = 1.0) -> int:
        now = _now()
        with self._tx() as con:
            cur = con.execute(
                """INSERT INTO memories(scope,agent_id,session_id,workspace,
                   content,confidence,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (scope, agent_id, session_id, workspace,
                 redact(content), confidence, now, now),
            )
            return cur.lastrowid

    def search_memories(self, query: str, agent_id: Optional[str] = None,
                        scope: Optional[str] = None,
                        limit: int = 10) -> list[dict]:
        """简单的 LIKE 全文搜索（无向量索引）。足够 MVP 使用。"""
        conds = ["content LIKE ?"]
        params: list[Any] = [f"%{query}%"]
        if agent_id:
            conds.append("agent_id=?")
            params.append(agent_id)
        if scope:
            conds.append("scope=?")
            params.append(scope)
        params.append(limit)
        rows = self._con.execute(
            f"SELECT * FROM memories WHERE {' AND '.join(conds)} "
            f"ORDER BY confidence DESC, updated_at DESC LIMIT ?",
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    def get_recent_memories(self, agent_id: Optional[str] = None,
                            limit: int = 20) -> list[dict]:
        conds = []
        params: list[Any] = []
        if agent_id:
            conds.append("agent_id=?")
            params.append(agent_id)
        where = f"WHERE {' AND '.join(conds)}" if conds else ""
        params.append(limit)
        rows = self._con.execute(
            f"SELECT * FROM memories {where} ORDER BY updated_at DESC LIMIT ?",
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    # ── tool_executions ───────────────────────────────────────────────────────

    def log_tool_execution(self, session_id: str, tool_name: str,
                           args_summary: str, result_summary: str,
                           is_error: bool = False,
                           elapsed_seconds: Optional[float] = None, *,
                           risk: str = "",
                           target_type: str = "",
                           scope: str = "",
                           origin: str = "",
                           host: Optional[str] = None,
                           approval_action: str = "",
                           outcome: Optional[str] = None,
                           requires_observation: bool = False) -> None:
        final_outcome = outcome or ("error" if is_error else "completed")
        with self._tx() as con:
            con.execute(
                """INSERT INTO tool_executions
                   (session_id,tool_name,risk,target_type,scope,origin,host,
                    approval_action,outcome,requires_observation,args_summary,
                    result_summary,is_error,elapsed_seconds,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (session_id, tool_name, risk, target_type, scope, origin, host,
                 approval_action, final_outcome, int(requires_observation),
                 redact(_truncate(args_summary)),
                 redact(_truncate(result_summary)),
                 int(is_error), elapsed_seconds, _now()),
            )

    def list_tool_executions(self, session_id: str) -> list[dict]:
        """按执行顺序返回某会话的工具审计记录。"""
        rows = self._con.execute(
            "SELECT * FROM tool_executions WHERE session_id=? ORDER BY id",
            (session_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    # ── tasks(TaskStore 持久化,支持退出重启恢复任务状态)──────────────────────

    def save_tasks(self, session_id: str, snapshot: list[dict]) -> None:
        """覆盖保存某 session 的任务快照(先删再批量插)。

        snapshot 是 TaskStore.snapshot() 的输出(id/content/status/dependencies/
        evidence/error/history)。dependencies / history 以 JSON 文本存。position
        保留任务顺序,读回时按它排序。evidence/error 经 redact 脱敏。
        """
        with self._tx() as con:
            con.execute("DELETE FROM tasks WHERE session_id=?", (session_id,))
            now = _now()
            for pos, t in enumerate(snapshot or []):
                con.execute(
                    """INSERT INTO tasks
                       (session_id,task_id,content,status,dependencies,
                        evidence,error,history,position,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (session_id, str(t.get("id", "")), str(t.get("content", "")),
                     str(t.get("status", "pending")),
                     json.dumps(t.get("dependencies", []), ensure_ascii=False),
                     redact(str(t.get("evidence", ""))),
                     redact(str(t.get("error", ""))),
                     json.dumps(t.get("history", []), ensure_ascii=False),
                     pos, now),
                )

    def load_tasks(self, session_id: str) -> list[dict]:
        """读回某 session 的任务快照(按 position 排序),格式同 TaskStore.snapshot()。"""
        rows = self._con.execute(
            "SELECT * FROM tasks WHERE session_id=? ORDER BY position", (session_id,)
        ).fetchall()
        out: list[dict] = []
        for r in rows:
            try:
                deps = json.loads(r["dependencies"])
            except (json.JSONDecodeError, TypeError):
                deps = []
            try:
                hist = json.loads(r["history"])
            except (json.JSONDecodeError, TypeError):
                hist = []
            out.append({
                "id": r["task_id"], "content": r["content"], "status": r["status"],
                "dependencies": deps, "evidence": r["evidence"],
                "error": r["error"], "history": hist,
            })
        return out

    # ── runs(每次编排 run 的目标 / 状态 / token 审计)───────────────────────────

    def log_run(self, session_id: str, goal: str, status: str,
                input_tokens: int = 0, output_tokens: int = 0) -> int:
        with self._tx() as con:
            cur = con.execute(
                """INSERT INTO runs
                   (session_id,goal,status,input_tokens,output_tokens,created_at)
                   VALUES(?,?,?,?,?,?)""",
                (session_id, redact(_truncate(goal, 200)), status,
                 int(input_tokens), int(output_tokens), _now()),
            )
            return cur.lastrowid

    def list_runs(self, session_id: str, limit: int = 50) -> list[dict]:
        rows = self._con.execute(
            "SELECT * FROM runs WHERE session_id=? ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── context_summaries(上下文压缩摘要审计,§7.3.3)────────────────────────────

    def save_context_summary(self, session_id: str, record: dict) -> int:
        """写入一条压缩摘要审计记录。

        record 是 ContextSummary.to_record() 的输出:summary_json / source_message_range
        / source_run_ids / token_count_before/after / compression_model_profile。
        summary_json 已是 JSON 文本(其字符串值在压缩阶段已脱敏),这里再兜底脱敏一次。
        原始消息不在这里删除 —— 压缩只换"模型输入里的旧片段",原始消息由消息表保留。
        """
        rng = record.get("source_message_range") or [0, 0]
        start = int(rng[0]) if len(rng) > 0 else 0
        end = int(rng[1]) if len(rng) > 1 else 0
        with self._tx() as con:
            cur = con.execute(
                """INSERT INTO context_summaries
                   (session_id,summary_json,range_start,range_end,source_run_ids,
                    token_before,token_after,compression_model_profile,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (session_id,
                 redact(str(record.get("summary_json", "{}"))),
                 start, end,
                 json.dumps(record.get("source_run_ids", []), ensure_ascii=False),
                 int(record.get("token_count_before", 0)),
                 int(record.get("token_count_after", 0)),
                 str(record.get("compression_model_profile", "")),
                 _now()),
            )
            return cur.lastrowid

    def list_context_summaries(self, session_id: str, limit: int = 50) -> list[dict]:
        rows = self._con.execute(
            "SELECT * FROM context_summaries WHERE session_id=? ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def latest_context_summary(self, session_id: str) -> Optional[dict]:
        row = self._con.execute(
            "SELECT * FROM context_summaries WHERE session_id=? ORDER BY id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        return dict(row) if row else None

    # ── agent_profiles ───────────────────────────────────────────────────────
    def upsert_agent_profile(self, agent_id: str, name: str,
                             model_profile: str,
                             memory_policy: str = "none",
                             config: Optional[dict] = None) -> None:
        with self._tx() as con:
            con.execute(
                "INSERT OR REPLACE INTO agent_profiles VALUES(?,?,?,?,?)",
                (agent_id, name, model_profile, memory_policy,
                 json.dumps(config or {})),
            )

    def list_agent_profiles(self) -> list[dict]:
        rows = self._con.execute("SELECT * FROM agent_profiles").fetchall()
        return [dict(r) for r in rows]
