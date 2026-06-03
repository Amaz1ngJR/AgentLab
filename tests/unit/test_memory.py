"""离线测试：长期记忆策略。"""
from app.memory import (
    NoMemory, ReadMemory, ReadWriteMemory,
    build_memory_policy, inject_memories,
)
from app.storage import Storage


def _store(tmp_path):
    return Storage(tmp_path / "test.db")


def test_no_memory_returns_empty():
    m = NoMemory()
    assert m.retrieve("anything", "agent") == []


def test_no_memory_save_is_noop():
    NoMemory().save("agent", "s1", [{"role": "user", "content": "hi"}])


def test_read_memory_retrieves(tmp_path):
    s = _store(tmp_path)
    s.write_memory("用户喜欢 Python", scope="agent", agent_id="coder")
    m = ReadMemory(s)
    results = m.retrieve("Python", "coder")
    assert any("Python" in r for r in results)


def test_read_memory_does_not_save(tmp_path):
    s = _store(tmp_path)
    m = ReadMemory(s)
    m.save("coder", "s1", [{"role": "user", "content": "hi"}])
    assert s.search_memories("hi") == []


def test_read_write_memory_saves_summary(tmp_path):
    s = _store(tmp_path)
    m = ReadWriteMemory(s)
    msgs = [
        {"role": "user", "content": "帮我优化这段代码"},
        {"role": "assistant", "content": "好的，我来看一下"},
    ]
    m.save("coder", "s1", msgs, workspace="/work")
    results = s.search_memories("优化", agent_id="coder")
    assert len(results) == 1
    assert results[0]["scope"] == "session"


def test_inject_memories_appends_block():
    prompt = "你是助手"
    result = inject_memories(prompt, ["用户喜欢简洁", "用户在上海"])
    assert "相关记忆" in result
    assert "用户喜欢简洁" in result


def test_inject_memories_empty_unchanged():
    assert inject_memories("你是助手", []) == "你是助手"


def test_build_memory_policy_none():
    assert isinstance(build_memory_policy("none"), NoMemory)


def test_build_memory_policy_read_write(tmp_path):
    s = _store(tmp_path)
    m = build_memory_policy("read_write", s)
    assert isinstance(m, ReadWriteMemory)
