"""模型推理强度配置加载测试。"""
import pytest

from app.config.loader import load_config


def test_crs_profile_loads_configured_reasoning_effort(monkeypatch):
    monkeypatch.setenv("CRS_OAI_KEY", "test-key")
    monkeypatch.setenv("CRS_OAI_URL", "https://example.com/openai")
    # 空字符串占位可阻止项目 .env 中的本地覆盖值影响 profile 默认值测试。
    monkeypatch.setenv("LLM_REASONING_EFFORT", "")
    cfg = load_config("crs_gpt")
    assert cfg.reasoning_effort == "max"


def test_reasoning_effort_environment_override(monkeypatch):
    monkeypatch.setenv("CRS_OAI_KEY", "test-key")
    monkeypatch.setenv("CRS_OAI_URL", "https://example.com/openai")
    monkeypatch.setenv("LLM_REASONING_EFFORT", "low")
    cfg = load_config("crs_gpt")
    assert cfg.reasoning_effort == "low"


def test_max_reasoning_effort_is_supported(monkeypatch):
    monkeypatch.setenv("CRS_OAI_KEY", "test-key")
    monkeypatch.setenv("LLM_REASONING_EFFORT", "max")
    cfg = load_config("crs_gpt")
    assert cfg.reasoning_effort == "max"


    monkeypatch.setenv("CRS_OAI_KEY", "test-key")
    monkeypatch.setenv("LLM_REASONING_EFFORT", "maximum")
    with pytest.raises(ValueError, match="reasoning_effort"):
        load_config("crs_gpt")
