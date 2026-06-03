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
        "    max_steps: 12\n",
        encoding="utf-8",
    )
    profiles = load_agent_profiles(f)
    p = profiles["coder"]
    assert p.agent_id == "coder"
    assert p.name == "代码助手"
    assert p.memory_policy == "read_write"
    assert p.max_steps == 12


def test_defaults(tmp_path):
    f = tmp_path / "a.yaml"
    f.write_text("agents:\n  x:\n    model_profile: local_qwen\n", encoding="utf-8")
    p = load_agent_profiles(f)["x"]
    assert p.memory_policy == "none"
    assert p.max_steps == 8
    assert p.tools == []
