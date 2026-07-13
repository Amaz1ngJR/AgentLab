"""测试 CLI session 增强功能：prompt 显示 + 退出写摘要 (§6.2)。"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.agent.profiles import AgentProfile
from app.agent.runtime import AgentSession
from app.agent.session_router import SessionRouter
from app.memory import ReadWriteMemory
from app.storage import Storage


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    return Storage(tmp_path / "test.db")


def test_agent_profile_attached_to_session(storage: Storage):
    """session_factory 应该把 agent_profile 附加到 session 上供 CLI prompt 用。"""
    profile = AgentProfile(agent_id="test", name="测试助手", model_profile="test")

    def factory(prof: AgentProfile, sid: str) -> AgentSession:
        sess = MagicMock(spec=AgentSession)
        sess.messages = []
        sess.close = MagicMock()
        # 模拟 CLI 的 _session_factory：附加 agent_profile
        sess.agent_profile = prof
        sess.mem_policy = MagicMock()
        return sess

    router = SessionRouter(storage, factory, {"test": profile}, "test")
    sid = router.new("test")

    assert router.current is not None
    assert hasattr(router.current, "agent_profile")
    assert router.current.agent_profile.name == "测试助手"


def test_mem_policy_attached_to_session(storage: Storage):
    """session_factory 应该把 mem_policy 附加到 session 上供退出时写摘要。"""
    profile = AgentProfile(agent_id="test", name="测试", model_profile="test",
                          memory_policy="read_write")

    def factory(prof: AgentProfile, sid: str) -> AgentSession:
        sess = MagicMock(spec=AgentSession)
        sess.messages = []
        sess.close = MagicMock()
        # 模拟 CLI 的 _session_factory：附加 mem_policy
        mem_policy = ReadWriteMemory(storage)
        sess.mem_policy = mem_policy
        sess.agent_profile = prof
        return sess

    router = SessionRouter(storage, factory, {"test": profile}, "test")
    sid = router.new("test")

    assert router.current is not None
    assert hasattr(router.current, "mem_policy")
    assert isinstance(router.current.mem_policy, ReadWriteMemory)


def test_close_all_calls_mem_policy_save(storage: Storage):
    """close_all 应该为 read_write 策略的 session 调用 mem_policy.save 写摘要。"""
    profile = AgentProfile(agent_id="test", name="测试", model_profile="test",
                          memory_policy="read_write")

    save_called = []

    def factory(prof: AgentProfile, sid: str) -> AgentSession:
        sess = MagicMock(spec=AgentSession)
        sess.messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
        ]
        sess.close = MagicMock()

        # 模拟带 save 方法的 mem_policy
        mem_policy = MagicMock()
        mem_policy.save = MagicMock(side_effect=lambda **kw: save_called.append(kw))
        sess.mem_policy = mem_policy
        sess.agent_profile = prof
        return sess

    router = SessionRouter(storage, factory, {"test": profile}, "test")
    sid = router.new("test")

    # 退出时应该调用 save
    router.close_all()

    assert len(save_called) == 1
    assert save_called[0]["agent_id"] == "test"
    assert save_called[0]["session_id"] == sid
    assert "messages" in save_called[0]


def test_close_all_graceful_when_no_mem_policy(storage: Storage):
    """close_all 应该优雅处理没有 mem_policy 的 session（向后兼容）。"""
    profile = AgentProfile(agent_id="test", name="测试", model_profile="test")

    def factory(prof: AgentProfile, sid: str) -> AgentSession:
        sess = MagicMock(spec=AgentSession)
        sess.messages = []
        sess.close = MagicMock()
        # 不附加 mem_policy（模拟旧代码）
        return sess

    router = SessionRouter(storage, factory, {"test": profile}, "test")
    router.new("test")

    # 不应该抛异常
    router.close_all()
    assert router.current is None


def test_close_all_swallows_save_exceptions(storage: Storage):
    """close_all 应该吞掉 mem_policy.save 的异常，不阻断退出。"""
    profile = AgentProfile(agent_id="test", name="测试", model_profile="test")

    def factory(prof: AgentProfile, sid: str) -> AgentSession:
        sess = MagicMock(spec=AgentSession)
        sess.messages = [{"role": "user", "content": "test"}]
        sess.close = MagicMock()

        # mem_policy.save 抛异常
        mem_policy = MagicMock()
        mem_policy.save = MagicMock(side_effect=RuntimeError("DB error"))
        sess.mem_policy = mem_policy
        sess.agent_profile = prof
        return sess

    router = SessionRouter(storage, factory, {"test": profile}, "test")
    router.new("test")

    # 异常应该被吞掉，不影响退出
    router.close_all()
    assert router.current is None


def test_read_write_memory_save_writes_to_storage(storage: Storage):
    """ReadWriteMemory.save 应该把对话摘要写入 storage.memories。"""
    mem_policy = ReadWriteMemory(storage)

    messages = [
        {"role": "user", "content": "帮我写个函数"},
        {"role": "assistant", "content": "好的，我来写一个示例函数：\ndef example(): pass"},
        {"role": "user", "content": "谢谢"},
        {"role": "assistant", "content": "不客气！"},
    ]

    mem_policy.save(
        agent_id="test_agent",
        session_id="sess123",
        messages=messages,
        workspace="/tmp/test",
    )

    # 检查是否写入了 memory
    rows = storage.search_memories("", agent_id="test_agent", limit=10)
    assert len(rows) == 1
    assert "user:" in rows[0]["content"]
    assert "assistant:" in rows[0]["content"]
    assert rows[0]["agent_id"] == "test_agent"
    assert rows[0]["session_id"] == "sess123"
