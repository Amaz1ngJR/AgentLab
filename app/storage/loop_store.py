"""loop_store —— Loop Engineering 的持久化存储。

新增表（PRD §10.3）：
  - goal_specs: GoalSpec 定义
  - loop_runs: Loop 执行实例
  - loop_iterations: 每轮循环记录
  - verification_results: Verifier 结果
  - worktrees: 隔离工作区元数据
  - subagent_runs: 子 Agent 运行记录

设计原则：
  - 与现有 storage 复用同一个 SQLite 连接
  - 所有写入经过 redact() 脱敏
  - 大内容（验证证据、修复计划、diff summary）存到 blobs，表里只存引用
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any

from app.util.redact import redact


def init_loop_tables(conn: sqlite3.Connection) -> None:
    """创建 Loop Engineering 相关表。

    与现有 sessions/messages/memories 等表共用同一个数据库。
    """
    conn.executescript("""
        -- GoalSpec 定义
        CREATE TABLE IF NOT EXISTS goal_specs (
            id TEXT PRIMARY KEY,
            session_id TEXT,
            objective TEXT NOT NULL,
            success_criteria_json TEXT NOT NULL,
            constraints_json TEXT,
            budgets_json TEXT,
            verification_plan_json TEXT NOT NULL,
            stop_conditions_json TEXT,
            workspace_mode TEXT DEFAULT 'git_worktree',
            learning_policy_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        );

        -- Loop 执行实例
        CREATE TABLE IF NOT EXISTS loop_runs (
            id TEXT PRIMARY KEY,
            goal_id TEXT NOT NULL,
            session_id TEXT,
            status TEXT NOT NULL,  -- ready/planning/executing/verifying/diagnosing/repairing/succeeded/blocked/budget_exhausted/cancelled
            current_iteration INTEGER DEFAULT 0,
            budget_used_json TEXT,  -- {iterations: N, tool_calls: M, runtime_seconds: X}
            worktree_id TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            FOREIGN KEY (goal_id) REFERENCES goal_specs(id) ON DELETE CASCADE,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        );

        -- 每轮循环记录
        CREATE TABLE IF NOT EXISTS loop_iterations (
            id TEXT PRIMARY KEY,
            loop_id TEXT NOT NULL,
            iteration_index INTEGER NOT NULL,
            status TEXT NOT NULL,  -- executing/verifying/succeeded/failed/blocked
            task_summary TEXT,
            failure_category TEXT,  -- test_failed/env_failed/permission_denied/model_unreliable/budget_insufficient
            repair_plan_ref TEXT,  -- 指向 blobs 的修复计划
            started_at TEXT NOT NULL,
            finished_at TEXT,
            FOREIGN KEY (loop_id) REFERENCES loop_runs(id) ON DELETE CASCADE
        );

        -- Verifier 结果
        CREATE TABLE IF NOT EXISTS verification_results (
            id TEXT PRIMARY KEY,
            loop_id TEXT NOT NULL,
            iteration_id TEXT,
            status TEXT NOT NULL,  -- pass/fail/blocked/uncertain
            checks_json TEXT NOT NULL,  -- [{name, status, evidence_ref, summary}]
            failure_category TEXT,
            confidence REAL DEFAULT 1.0,
            next_hint TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (loop_id) REFERENCES loop_runs(id) ON DELETE CASCADE,
            FOREIGN KEY (iteration_id) REFERENCES loop_iterations(id) ON DELETE CASCADE
        );

        -- 隔离工作区元数据
        CREATE TABLE IF NOT EXISTS worktrees (
            id TEXT PRIMARY KEY,
            loop_id TEXT,
            path TEXT NOT NULL,
            base_branch TEXT NOT NULL,
            base_commit TEXT NOT NULL,
            is_dirty INTEGER DEFAULT 0,
            auto_cleanup INTEGER DEFAULT 1,
            status TEXT DEFAULT 'active',  -- active/merged/removed
            created_at TEXT NOT NULL,
            removed_at TEXT,
            FOREIGN KEY (loop_id) REFERENCES loop_runs(id) ON DELETE CASCADE
        );

        -- 子 Agent 运行记录
        CREATE TABLE IF NOT EXISTS subagent_runs (
            id TEXT PRIMARY KEY,
            loop_id TEXT NOT NULL,
            role TEXT NOT NULL,  -- executor/verifier/reviewer/research
            session_id TEXT,
            status TEXT NOT NULL,  -- running/succeeded/failed/cancelled
            input_summary TEXT,
            output_summary TEXT,
            result_summary_ref TEXT,  -- 指向 blobs 的完整结果
            started_at TEXT NOT NULL,
            finished_at TEXT,
            FOREIGN KEY (loop_id) REFERENCES loop_runs(id) ON DELETE CASCADE
        );

        -- 索引
        CREATE INDEX IF NOT EXISTS idx_goal_specs_session ON goal_specs(session_id);
        CREATE INDEX IF NOT EXISTS idx_loop_runs_goal ON loop_runs(goal_id);
        CREATE INDEX IF NOT EXISTS idx_loop_runs_session ON loop_runs(session_id);
        CREATE INDEX IF NOT EXISTS idx_loop_iterations_loop ON loop_iterations(loop_id);
        CREATE INDEX IF NOT EXISTS idx_verification_results_loop ON verification_results(loop_id);
        CREATE INDEX IF NOT EXISTS idx_worktrees_loop ON worktrees(loop_id);
        CREATE INDEX IF NOT EXISTS idx_subagent_runs_loop ON subagent_runs(loop_id);
    """)
    conn.commit()


def save_goal_spec(conn: sqlite3.Connection, goal_spec: dict[str, Any]) -> None:
    """保存 GoalSpec。"""
    now = datetime.utcnow().isoformat()
    conn.execute("""
        INSERT OR REPLACE INTO goal_specs
        (id, session_id, objective, success_criteria_json, constraints_json,
         budgets_json, verification_plan_json, stop_conditions_json,
         workspace_mode, learning_policy_json, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        goal_spec["goal_id"],
        goal_spec.get("session_id"),
        redact(goal_spec["objective"]),
        json.dumps(goal_spec["success_criteria"]),
        json.dumps(goal_spec.get("constraints", {})),
        json.dumps(goal_spec.get("budgets", {})),
        json.dumps(goal_spec["verification_plan"]),
        json.dumps(goal_spec.get("stop_conditions", [])),
        goal_spec.get("workspace_mode", "git_worktree"),
        json.dumps(goal_spec.get("learning_policy", {})),
        goal_spec.get("created_at", now),
        now,
    ))
    conn.commit()


def load_goal_spec(conn: sqlite3.Connection, goal_id: str) -> dict[str, Any] | None:
    """加载 GoalSpec。"""
    row = conn.execute(
        "SELECT * FROM goal_specs WHERE id = ?",
        (goal_id,)
    ).fetchone()
    if not row:
        return None
    return {
        "goal_id": row[0],
        "session_id": row[1],
        "objective": row[2],
        "success_criteria": json.loads(row[3]),
        "constraints": json.loads(row[4]) if row[4] else {},
        "budgets": json.loads(row[5]) if row[5] else {},
        "verification_plan": json.loads(row[6]),
        "stop_conditions": json.loads(row[7]) if row[7] else [],
        "workspace_mode": row[8],
        "learning_policy": json.loads(row[9]) if row[9] else {},
        "created_at": row[10],
        "updated_at": row[11],
    }


def save_loop_run(conn: sqlite3.Connection, loop_run: dict[str, Any]) -> None:
    """保存 LoopRun。"""
    conn.execute("""
        INSERT OR REPLACE INTO loop_runs
        (id, goal_id, session_id, status, current_iteration, budget_used_json,
         worktree_id, started_at, finished_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        loop_run["id"],
        loop_run["goal_id"],
        loop_run.get("session_id"),
        loop_run["status"],
        loop_run.get("current_iteration", 0),
        json.dumps(loop_run.get("budget_used", {})),
        loop_run.get("worktree_id"),
        loop_run["started_at"],
        loop_run.get("finished_at"),
    ))
    conn.commit()


def load_loop_run(conn: sqlite3.Connection, loop_id: str) -> dict[str, Any] | None:
    """加载 LoopRun。"""
    row = conn.execute(
        "SELECT * FROM loop_runs WHERE id = ?",
        (loop_id,)
    ).fetchone()
    if not row:
        return None
    return {
        "id": row[0],
        "goal_id": row[1],
        "session_id": row[2],
        "status": row[3],
        "current_iteration": row[4],
        "budget_used": json.loads(row[5]) if row[5] else {},
        "worktree_id": row[6],
        "started_at": row[7],
        "finished_at": row[8],
    }


def save_verification_result(conn: sqlite3.Connection, result: dict[str, Any]) -> None:
    """保存 VerificationResult。"""
    conn.execute("""
        INSERT INTO verification_results
        (id, loop_id, iteration_id, status, checks_json, failure_category,
         confidence, next_hint, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        result["id"],
        result["loop_id"],
        result.get("iteration_id"),
        result["status"],
        json.dumps(result["checks"]),
        result.get("failure_category"),
        result.get("confidence", 1.0),
        redact(result.get("next_hint", "")),
        result.get("created_at", datetime.utcnow().isoformat()),
    ))
    conn.commit()


def save_worktree(conn: sqlite3.Connection, worktree: dict[str, Any]) -> None:
    """保存 Worktree 元数据。"""
    conn.execute("""
        INSERT OR REPLACE INTO worktrees
        (id, loop_id, path, base_branch, base_commit, is_dirty,
         auto_cleanup, status, created_at, removed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        worktree["id"],
        worktree.get("loop_id"),
        str(worktree["path"]),
        worktree["base_branch"],
        worktree["base_commit"],
        1 if worktree.get("is_dirty") else 0,
        1 if worktree.get("auto_cleanup", True) else 0,
        worktree.get("status", "active"),
        worktree.get("created_at", datetime.utcnow().isoformat()),
        worktree.get("removed_at"),
    ))
    conn.commit()
