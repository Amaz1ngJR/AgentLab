"""离线测试：长期记忆策略。"""
from app.memory import NoMemory, build_memory_policy, inject_memories


def test_no_memory_returns_empty():
    m = NoMemory()
    assert m.retrieve("anything", "agent") == []


def test_no_memory_save_is_noop():
    NoMemory().save("agent", "s1", [{"role": "user", "content": "hi"}])


def test_inject_memories_appends_block():
    prompt = "你是助手"
    result = inject_memories(prompt, ["用户喜欢简洁", "用户在上海"])
    assert "相关记忆" in result
    assert "用户喜欢简洁" in result


def test_inject_memories_empty_unchanged():
    assert inject_memories("你是助手", []) == "你是助手"


def test_build_memory_policy_none():
    assert isinstance(build_memory_policy("none"), NoMemory)
