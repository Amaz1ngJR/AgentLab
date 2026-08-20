"""SessionRouter —— 管理多个 AgentSession 实例的生命周期和切换。

CLI 命令族：
  /session              - 显示当前 session 信息
  /session list         - 列出所有活跃 session
  /session agents       - 列出可用 AgentProfile
  /session new [agent_id] [title]  - 创建新 session（并切换进去）
  /session switch <session_id>     - 切换到已有 session
  /session rename <title>          - 重命名当前 session
  /session archive                 - 归档当前 session（软删除,数据保留,从列表隐藏）
  /session delete [session_id]     - 彻底删除 session 及其消息（硬删除,不可恢复;留空删当前）
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
        # 由 CLI 注入的全局共享资源(如 MCPManager),在 close_all 时统一关闭。
        # 不放进单个 session 的 closeables,避免切换/归档某个 session 时误关全局连接。
        self.mcp_manager = None

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
            # 恢复任务快照(若有):让 /session switch 后任务面板与编排状态接上
            tasks_snapshot = self._storage.load_tasks(session_id)
            if tasks_snapshot and getattr(session, "task_store", None) is not None:
                session.task_store.restore(tasks_snapshot)
            self._sessions[session_id] = session
        self.current_id = session_id
        return True

    def resume_or_new(self, agent_id: Optional[str] = None) -> tuple[str, bool]:
        """启动时调用:有未归档且非空的历史 session 就恢复最近一个,否则新建。

        返回 (session_id, resumed):resumed=True 表示恢复了历史会话。
        "最近"按 list_sessions 的 updated_at DESC 顺序;跳过 0 消息的空会话,
        避免恢复到一个从未对话过的空壳(否则用户会以为"历史丢了")。
        """
        rows = self._storage.list_sessions()  # 已过滤 archived,按 updated_at DESC
        for row in rows:
            if self._storage.count_messages(row["id"]) > 0 and self.switch(row["id"]):
                return row["id"], True
        return self.new(agent_id=agent_id), False

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

    def delete(self, session_id: str) -> bool:
        """硬删除指定 session(连消息一起抹掉,不可恢复)。返回是否成功。

        允许删非当前 session;删的是当前 session 时,清空 current_id。
        """
        if self._storage.get_session(session_id) is None:
            return False
        session = self._sessions.pop(session_id, None)
        if session:
            session.close()
        self._storage.delete_session(session_id)
        try:
            from app.attachments import AttachmentStore
            AttachmentStore().delete_session(session_id)
        except Exception:
            # 数据库硬删除不能因附件清理失败而回滚；遗留文件仍位于受控目录。
            pass
        if self.current_id == session_id:
            self.current_id = None
        return True

    def clear_session_images(self, session_id: str | None = None) -> dict[str, int]:
        """清除 Session 历史中的图片引用和磁盘文件，但保留文本对话。"""
        target = session_id or self.current_id
        if not target:
            raise ValueError("当前无活跃 session")
        if target not in self._sessions and not self.switch(target):
            raise KeyError(f"找不到 session: {target}")
        session = self._sessions[target]
        from app.attachments import AttachmentStore, strip_image_blocks

        cleaned, reference_count = strip_image_blocks(session.messages)
        # 先更新内存和 SQLite，确保即使文件清理失败，后续也不会再引用已删除图片。
        session.messages[:] = cleaned
        self._storage.save_messages(target, session.messages)
        file_count = AttachmentStore().delete_session(target)
        return {"references": reference_count, "files": file_count}

    def list_sessions(self) -> list[dict]:
        return self._storage.list_sessions()

    def list_profiles(self) -> dict[str, AgentProfile]:
        return self._profiles

    def persist_current(self) -> None:
        """把当前 session 的消息历史 + 任务快照存盘。"""
        if self.current_id:
            self.persist(self.current_id)

    def persist(self, session_id: str) -> None:
        """持久化指定 session，避免并发 run 因 current 切换而写错会话。"""
        sess = self._sessions.get(session_id)
        if sess is None:
            return
        self._storage.save_messages(session_id, sess.messages)
        # 标记最后活跃时间:让 resume_or_new 的"最近"= 最后对话时间,而非创建/
        # 重命名时间,避免每次启动恢复到从未对话过的空壳 session。只在确有消息时
        # touch,空会话不抢占"最近"位置。
        if sess.messages:
            self._storage.touch_session(session_id)
        store = getattr(sess, "task_store", None)
        if store is not None:
            self._storage.save_tasks(session_id, store.snapshot())
        # 上下文压缩摘要审计:把本轮产生的 ContextSummary flush 到 context_summaries。
        # 原始消息已由 save_messages 保留 —— 压缩只换"模型输入里的旧片段",不删原文。
        ctx = getattr(sess, "context_manager", None)
        if ctx is not None:
            for summary in ctx.drain_records():
                self._storage.save_context_summary(session_id, summary.to_record())
        # 编排 run 审计:有 goal 才记(单轮 legacy chat 不写 runs)
        goal = getattr(sess, "last_goal", "") or ""
        status = getattr(sess, "last_run_status", "") or ""
        if goal and status:
            usage = getattr(sess, "last_turn_usage", {}) or {}
            self._storage.log_run(
                session_id, goal, status,
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
            )
            # 写完清空,避免下一轮无 goal 时重复记账
            sess.last_goal = ""
            sess.last_run_status = ""

    def close_all(self) -> None:
        """关闭所有 session + 共享资源(MCP server),退出前调用。

        read_write 记忆策略的 session 会在此时写入会话摘要(§6.2)。
        """
        from app.config.loader import workspace_root
        ws = str(workspace_root())

        for sid, sess in self._sessions.items():
            # read_write 策略的会话结束时写摘要到 memories
            mem_policy = getattr(sess, "mem_policy", None)
            agent_profile = getattr(sess, "agent_profile", None)
            if mem_policy and agent_profile and hasattr(mem_policy, "save"):
                try:
                    mem_policy.save(
                        agent_id=agent_profile.agent_id,
                        session_id=sid,
                        messages=sess.messages,
                        workspace=ws,
                    )
                except Exception:
                    # 写摘要失败不应阻断退出,静默吞掉
                    pass
            sess.close()
        self._sessions.clear()
        # 关闭全局共享资源(MCP server 进程等)
        if self.mcp_manager is not None:
            try:
                self.mcp_manager.stop()
            except Exception:
                pass
            self.mcp_manager = None

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
        if sub == "delete":
            target = arg or self.current_id
            if not target:
                return "用法: /session delete <session_id>(留空则删当前 session)"
            if self.delete(target):
                return f"已彻底删除 session {target}(消息已一并抹除,不可恢复)。"
            return f"找不到 session: {target}"
        return ("未知子命令: {0}。可用: list / agents / new / switch / "
                "rename / archive / delete").format(sub)

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
        lines = ["ID       Agent     消息数  标题"]
        for r in rows:
            marker = "▸" if r["id"] == self.current_id else " "
            msg_count = self._storage.count_messages(r["id"])
            lines.append(f"{marker} {r['id']:<8} {r['agent_id']:<9} {msg_count:<7} {r['title']}")
        return "\n".join(lines)

    def _cmd_agents(self) -> str:
        profiles = self.list_profiles()
        if not profiles:
            return "无可用 AgentProfile（未配置 config/agents.yaml）。"
        lines = ["agent_id    name              model_profile"]
        for p in profiles.values():
            lines.append(f"  {p.agent_id:<12}{p.name:<18}{p.model_profile}")
        return "\n".join(lines)
