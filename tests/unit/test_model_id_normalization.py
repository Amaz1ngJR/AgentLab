"""离线测试:模型 ID 规范化对比,避免代理 `.` vs `-` 命名风格误报警告。"""
from app.cli import _normalize_model_id


def test_normalize_dot_vs_dash():
    assert _normalize_model_id("claude-opus-4-6") == _normalize_model_id("claude-opus-4.6")


def test_normalize_underscore():
    assert _normalize_model_id("Claude_Opus_4_6") == _normalize_model_id("claude-opus-4-6")


def test_normalize_case_insensitive():
    assert _normalize_model_id("Claude-Sonnet-4-6") == _normalize_model_id("claude-sonnet-4-6")


def test_normalize_none_safe():
    assert _normalize_model_id(None) == ""
    assert _normalize_model_id("") == ""


def test_normalize_distinguishes_different_models():
    """规范化不应把真正不同的模型混为同一个。"""
    assert _normalize_model_id("claude-opus-4-7") != _normalize_model_id("claude-sonnet-4-6")
    assert _normalize_model_id("claude-opus-4-7") != _normalize_model_id("claude-3-5-sonnet-20241022")


def test_normalize_handles_proxy_silent_mapping():
    """真正的代理静默映射:不同的真实模型,规范化后仍不同 -> 警告会触发。"""
    assert _normalize_model_id("claude-opus-4-9") != _normalize_model_id("claude-3-5-sonnet-20241022")
