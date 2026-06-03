"""长期记忆策略 —— 根据 AgentProfile.memory_policy 决定如何检索和写入记忆。

三种策略（§10.3）：
  none        - 不使用记忆，直接返回空（默认，零开销）
  read        - 会话开始时检索相关记忆注入上下文，不写入
  read_write  - 同 read，且会话结束时把对话摘要写入记忆
"""
from __future__ import annotations

from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from app.storage import Storage


class MemoryPolicy:
    """记忆策略接口。"""

    def retrieve(self, query: str, agent_id: str, limit: int = 5) -> list[str]:
        """返回与 query 相关的记忆文本列表，注入到 system prompt 末尾。"""
        return []

    def save(self, agent_id: str, session_id: str,
             messages: list[dict], workspace: Optional[str] = None) -> None:
        """会话结束时调用，按策略决定是否持久化摘要。"""


class NoMemory(MemoryPolicy):
    """memory_policy: none"""


class ReadMemory(MemoryPolicy):
    """memory_policy: read —— 只检索，不写入。"""

    def __init__(self, storage: "Storage"):
        self._store = storage

    def retrieve(self, query: str, agent_id: str, limit: int = 5) -> list[str]:
        rows = self._store.search_memories(query, agent_id=agent_id, limit=limit)
        return [r["content"] for r in rows]


class ReadWriteMemory(ReadMemory):
    """memory_policy: read_write —— 检索，并在会话结束时写摘要。"""

    def save(self, agent_id: str, session_id: str,
             messages: list[dict], workspace: Optional[str] = None) -> None:
        # 取最后几条用户/assistant 消息作为摘要
        turns = [m for m in messages if m.get("role") in ("user", "assistant")][-6:]
        if not turns:
            return
        lines = []
        for m in turns:
            role = m.get("role", "")
            content = m.get("content", "")
            if isinstance(content, list):  # Anthropic content blocks
                content = " ".join(
                    b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
                )
            lines.append(f"{role}: {str(content)[:200]}")
        summary = "\n".join(lines)
        self._store.write_memory(
            content=summary,
            scope="session",
            agent_id=agent_id,
            session_id=session_id,
            workspace=workspace,
        )


def build_memory_policy(policy_name: str,
                        storage: Optional["Storage"] = None) -> MemoryPolicy:
    """工厂函数：按 policy_name 返回对应策略实例。"""
    if policy_name == "read" and storage:
        return ReadMemory(storage)
    if policy_name == "read_write" and storage:
        return ReadWriteMemory(storage)
    return NoMemory()


def inject_memories(system_prompt: str, memories: list[str]) -> str:
    """将检索到的记忆片段追加到 system prompt 末尾。"""
    if not memories:
        return system_prompt
    block = "\n".join(f"- {m}" for m in memories)
    return f"{system_prompt}\n\n【相关记忆】\n{block}"
