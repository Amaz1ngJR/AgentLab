"""模型推理强度配置加载测试。"""
import pytest

from app.config.loader import load_config


def test_crs_profile_loads_configured_reasoning_effort(monkeypatch):
    monkeypatch.setenv("CRS_OAI_KEY", "test-key")
    monkeypatch.setenv("CRS_OAI_URL", "https://example.com/openai")
    # 空字符串占位可阻止项目 .env 中的本地覆盖值影响 profile 默认值测试。
    monkeypatch.setenv("LLM_REASONING_EFFORT", "")
    cfg = load_config("crs_gpt")
    # models.yaml 中 crs_gpt 配的是 high；env 为空时回落到该 profile 值。
    assert cfg.reasoning_effort == "high"


def test_reasoning_effort_environment_override(monkeypatch):
    monkeypatch.setenv("CRS_OAI_KEY", "test-key")
    monkeypatch.setenv("CRS_OAI_URL", "https://example.com/openai")
    monkeypatch.setenv("LLM_REASONING_EFFORT", "low")
    cfg = load_config("crs_gpt")
    assert cfg.reasoning_effort == "low"


def test_max_reasoning_effort_is_supported(monkeypatch):
    monkeypatch.setenv("CRS_OAI_KEY", "test-key")
    monkeypatch.setenv("CRS_OAI_URL", "https://example.com/openai")
    monkeypatch.setenv("LLM_REASONING_EFFORT", "max")
    cfg = load_config("crs_gpt")
    assert cfg.reasoning_effort == "max"


def test_invalid_reasoning_effort_is_rejected(monkeypatch):
    """非法强度值必须报错，而不是静默降级。"""
    monkeypatch.setenv("CRS_OAI_KEY", "test-key")
    monkeypatch.setenv("CRS_OAI_URL", "https://example.com/openai")
    monkeypatch.setenv("LLM_REASONING_EFFORT", "maximum")
    with pytest.raises(ValueError, match="reasoning_effort"):
        load_config("crs_gpt")


def test_crs_profile_declares_context_window(monkeypatch):
    """回归：crs_gpt 必须显式声明 context_size，否则窗口解析掉进 8192 兜底。

    窗口按模型名前缀查表（context_budget._MODEL_CONTEXT_LIMITS），表里只有
    gpt-5，gpt-6.1-sol 匹配不到会退回 DEFAULT_CONTEXT_LIMIT=8192。这会把压缩
    阈值压到 7536 token —— 小于 system prompt + 工具 schema + 任意一次工具结果，
    于是每次模型调用前都重复压缩旧历史。这里锁定声明值，防止模型名再变动时
    静默回退。
    """
    monkeypatch.setenv("CRS_OAI_KEY", "test-key")
    monkeypatch.setenv("CRS_OAI_URL", "https://example.com/openai")
    monkeypatch.delenv("LLM_CONTEXT_SIZE", raising=False)
    cfg = load_config("crs_gpt")
    assert cfg.context_size is not None and cfg.context_size > 8192

    from app.agent.context_budget import ContextBudget, resolve_context_limit
    # 声明值优先于按名查表：即使模型名匹配不到任何前缀，也用 profile 的窗口。
    assert resolve_context_limit(cfg.model, cfg.context_size) == cfg.context_size
    assert ContextBudget.from_model(cfg.model, cfg.context_size).model_context_limit > 8192


def test_context_size_env_overrides_profile(monkeypatch):
    """LLM_CONTEXT_SIZE 仍可临时覆盖 profile 声明的窗口。"""
    monkeypatch.setenv("CRS_OAI_KEY", "test-key")
    monkeypatch.setenv("CRS_OAI_URL", "https://example.com/openai")
    monkeypatch.setenv("LLM_CONTEXT_SIZE", "12000")
    assert load_config("crs_gpt").context_size == 12000
