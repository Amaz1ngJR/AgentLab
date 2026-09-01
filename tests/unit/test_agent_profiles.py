"""离线测试：AgentProfile 配置加载。"""
from app.agent.profiles import AgentProfile, load_agent_profiles


def test_missing_file_returns_empty(tmp_path):
    assert load_agent_profiles(tmp_path / "nope.yaml") == {}


def test_parses_profile(tmp_path):
    f = tmp_path / "a.yaml"
    f.write_text(
        "agents:\n"
        "  coder:\n"
        "    name: 代码助手\n"
        "    model_profile: cloud_claude\n"
        "    memory_policy: read_write\n"
        "    max_steps: 12\n"
        "    max_task_steps: 5\n",
        encoding="utf-8",
    )
    profiles = load_agent_profiles(f)
    p = profiles["coder"]
    assert p.agent_id == "coder"
    assert p.name == "代码助手"
    assert p.memory_policy == "read_write"
    assert p.max_steps == 12
    assert p.max_task_steps == 5
    assert p.orchestrate is True


def test_parses_auto_mode(tmp_path):
    f = tmp_path / "a.yaml"
    f.write_text(
        "agents:\n  x:\n    model_profile: local_qwen\n    mode: auto\n",
        encoding="utf-8",
    )
    assert load_agent_profiles(f)["x"].mode == "auto"


def test_shipped_profiles_use_auto_mode():
    profiles = load_agent_profiles()

    assert profiles
    assert all(profile.mode == "auto" for profile in profiles.values())


def test_rejects_unknown_mode():
    import pytest
    with pytest.raises(ValueError, match="mode"):
        AgentProfile("x", "X", "fake", mode="unknown")


def test_parses_orchestrate_false(tmp_path):
    f = tmp_path / "a.yaml"
    f.write_text(
        "agents:\n  x:\n    model_profile: local_qwen\n    orchestrate: false\n",
        encoding="utf-8",
    )
    assert load_agent_profiles(f)["x"].orchestrate is False



def test_rejects_invalid_orchestrate_value(tmp_path):
    import pytest

    f = tmp_path / "a.yaml"
    f.write_text(
        "agents:\n  x:\n    model_profile: local_qwen\n    orchestrate: maybe\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="orchestrate"):
        load_agent_profiles(f)


    f = tmp_path / "a.yaml"
    f.write_text("agents:\n  x:\n    model_profile: local_qwen\n", encoding="utf-8")
    p = load_agent_profiles(f)["x"]
    assert p.memory_policy == "none"
    assert p.max_steps == 8
    assert p.max_task_steps is None
    assert p.orchestrate is True
    assert p.tools == []
