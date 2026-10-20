"""Reusable context-compaction input data."""
_VALID_SUMMARY = {
    "user_goal": "重构登录模块",
    "active_constraints": ["不要改数据库 schema"],
    "decisions": ["用 JWT 而非 session"],
    "current_state": "已读完 auth.py",
    "open_tasks": ["补单元测试"],
    "tool_evidence": [{"source": "read_file", "finding": "auth.py 有硬编码密钥"}],
    "files_and_artifacts": ["app/auth.py"],
    "failed_attempts": [],
    "approvals_and_risks": [],
    "memory_candidates": [],
    "handoff_note": "继续补测试并移除硬编码密钥",
}


def _long_messages(n=12):
    return [{"role": "user" if i % 2 == 0 else "assistant",
             "content": f"消息 {i} " + "内容" * 20} for i in range(n)]
