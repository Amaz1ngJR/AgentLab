"""SessionRouter —— 管理多个 AgentSession 实例的生命周期和切换。

CLI 命令族：
  /session              - 显示当前 session 信息
  /session list         - 列出所有活跃 session
  /session agents       - 列出可用 AgentProfile
  /session new [agent_id] [title]  - 创建新 session（并切换进去）
  /session switch <session_id>     - 切换到已有 session
  /session rename <title>          - 重命名当前 session
  /session archive                 - 归档当前 session
"""
from __future__ import annotations

import uuid
from typing import Any, Callable, Optional

from app.agent.profiles import AgentProfile
from app.agent.runtime import AgentSession
from app.storage import Storage


class SessionRouter:
    """维护 session_id -> AgentSession 的映射，并负责创建/切换/归档。"""

    def __init__(
        self,
        storage: Storage,
        session_factory: Callable[[AgentProfile, str], AgentSession],
        profiles: dict[str, AgentProfile],
        default_profile_id: str = "default",
    ):
        self._storage = storage
        self._factory = session_factory
        self._profiles = profiles
        self._default_profile_id = default_profile_id
        self._sessions: dict[str, AgentSession] = {}
        self.current_id: Optional[str] = None

    # ── 属性 ──────────────────────────────────────────────────────────────────

    @property
    def current(self) -> Optional[AgentSession]:
        return self._sessions.get(self.current_id) if self.current_id else None

    # ── 公开操作 ──────────────────────────────────────────────────────────────

    def new(self, agent_id: Optional[str] = None, title: str = "") -> str:
        """创建并切换到新 session，返回 session_id。"""
        aid = agent_id or self._default_profile_id
        profile = self._profiles.get(aid)
        if profile is None:
            # 没有 agents.yaml 时 fallback 到占位 profile
            profile = AgentProfile(agent_id=aid, name=aid,
                                   model_profile=self._default_profile_id)
        session_id = str(uuid.uuid4())[:8]
        session = self._factory(profile, session_id)
        self._sessions[session_id] = session
        self._storage.create_session(
            session_id=session_id,
            agent_id=aid,
            model_profile=profile.model_profile,
            title=title or f"{profile.name} #{session_id}",
        )
        self.current_id = session_id
        return session_id

    def switch(self, session_id: str) -> bool:
        """切换到已有 session（内存或 SQLite 恢复），返回是否成功。"""
        if session_id not in self._sessions:
            # 尝试从 SQLite 恢复
            row = self._storage.get_session(session_id)
            if not row:
                return False
            profile = self._profiles.get(row["agent_id"])
            if profile is None:
                profile = AgentProfile(
                    agent_id=row["agent_id"],
                    name=row["agent_id"],
                    model_profile=row["model_profile"],
                )
            session = self._factory(profile, session_id)
            session.messages = self._storage.load_messages(session_id)
            self._sessions[session_id] = session
        self.current_id = session_id
        return True

    def rename(self, title: str) -> None:
        if self.current_id:
            self._storage.update_session_title(self.current_id, title)

    def archive(self) -> None:
        if self.current_id:
            self._storage.archive_session(self.current_id)
            session = self._sessions.pop(self.current_id, None)
            if session:
                session.close()
            self.current_id = None

    def list_sessions(self) -> list[dict]:
        return self._storage.list_sessions()

    def list_profiles(self) -> dict[str, AgentProfile]:
        return self._profiles

    def persist_current(self) -> None:
        """把当前 session 的消息历史存盘（每次 chat 后调用）。"""
        if self.current_id and self.current:
            self._storage.save_messages(self.current_id, self.current.messages)

    def close_all(self) -> None:
        for s in self._sessions.values():
            s.close()
        self._sessions.clear()

    # ── CLI 命令解析 ──────────────────────────────────────────────────────────

    def handle_command(self, line: str) -> Optional[str]:
        """
        解析 /session ... 命令并执行，返回给用户的文本；
        不是 /session 命令则返回 None。
        """
        if not line.startswith("/session"):
            return None
        parts = line.split(maxsplit=2)
        sub = parts[1] if len(parts) > 1 else ""
        arg = parts[2] if len(parts) > 2 else ""

        if sub == "" or sub == "info":
            return self._cmd_info()
        if sub == "list":
            return self._cmd_list()
        if sub == "agents":
            return self._cmd_agents()
        if sub == "new":
            # /session new [agent_id] [title]
            words = arg.split(maxsplit=1)
            aid = words[0] if words else None
            title = words[1] if len(words) > 1 else ""
            sid = self.new(agent_id=aid, title=title)
            row = self._storage.get_session(sid)
            return f"已创建并切换到新 session: {sid}  ({row['title'] if row else ''})"
        if sub == "switch":
            if not arg:
                return "用法: /session switch <session_id>"
            if self.switch(arg):
                row = self._storage.get_session(arg)
                return f"已切换到: {arg}  ({row['title'] if row else ''})"
            return f"找不到 session: {arg}"
        if sub == "rename":
            if not arg:
                return "用法: /session rename <新标题>"
            self.rename(arg)
            return f"已重命名为: {arg}"
        if sub == "archive":
            old = self.current_id
            self.archive()
            return f"已归档 session {old}。当前无活跃 session，用 /session new 创建。"
        return f"未知子命令: {sub}。可用: list / agents / new / switch / rename / archive"

    def _cmd_info(self) -> str:
        if not self.current_id:
            return "当前无活跃 session。用 /session new 创建。"
        row = self._storage.get_session(self.current_id)
        title = row["title"] if row else ""
        msgs = len(self.current.messages) if self.current else 0
        return f"session: {self.current_id}  {title}  消息数: {msgs}"

    def _cmd_list(self) -> str:
        rows = self.list_sessions()
        if not rows:
            return "暂无活跃 session。"
        lines = ["ID       Agent     标题"]
        for r in rows:
            marker = "▸" if r["id"] == self.current_id else " "
            lines.append(f"{marker} {r['id']:<8} {r['agent_id']:<9} {r['title']}")
        return "\n".join(lines)

    def _cmd_agents(self) -> str:
        profiles = self.list_profiles()
        if not profiles:
            return "无可用 AgentProfile（未配置 config/agents.yaml）。"
        lines = ["agent_id    name              model_profile"]
        for p in profiles.values():
            lines.append(f"  {p.agent_id:<12}{p.name:<18}{p.model_profile}")
        return "\n".join(lines)
