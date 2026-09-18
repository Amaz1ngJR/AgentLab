"""单轮输出上限(max_tokens)的配置与传递测试。

背景:adapter 曾把 max_tokens 硬编码成 4096 且调用点从不传值,导致开启扩展
思考的模型在推理阶段就耗尽预算,正文/工具调用都没写出来就 stop_reason=max_tokens。
这里锁定三级优先级:显式参数 > LLM_MAX_TOKENS 环境变量 > profile 参数 > 默认值。
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.config.loader import load_config
from app.config.schemas import LLMConfig


def _base_cfg(**overrides) -> LLMConfig:
    kwargs = dict(
        provider="anthropic",
        model="claude-fable-5",
        base_url=None,
        api_key=None,
        auth_token="test-token",
        temperature=0.2,
        top_p=None,
        context_size=None,
        timeout_seconds=30,
        stream=False,
    )
    kwargs.update(overrides)
    return LLMConfig(**kwargs)


def test_schema_default_is_generous():
    """默认值必须给扩展思考留出余量,不能退回 4096。"""
    assert _base_cfg().max_tokens == 16384


def test_env_overrides_profile(monkeypatch):
    """LLM_MAX_TOKENS 覆盖 profile 里的 max_tokens。"""
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "test-token")
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.setenv("LLM_MAX_TOKENS", "20000")
    assert load_config("cloud_claude_opus").max_tokens == 20000


def test_profile_param_used_when_env_absent(monkeypatch):
    """env 未设置时取 profile 的 params.max_tokens。"""
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "test-token")
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_MAX_TOKENS", raising=False)
    # cloud_claude_opus 在 models.yaml 里显式配了 32768
    assert load_config("cloud_claude_opus").max_tokens == 32768


def _patch_anthropic(captured: dict):
    """返回 patch 上下文,捕获 adapter 实际发给 SDK 的 max_tokens。"""
    class FakeStream:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def __iter__(self): return iter([])
        def get_final_message(self):
            m = MagicMock()
            m.content = []
            m.usage = None
            m.stop_reason = "end_turn"
            m.model = "claude-fable-5"
            return m

    def fake_stream(**kw):
        captured.update(kw)
        return FakeStream()

    def fake_create(**kw):
        captured.update(kw)
        m = MagicMock()
        m.content = []
        m.usage = None
        m.stop_reason = "end_turn"
        m.model = "claude-fable-5"
        return m

    patcher = patch("anthropic.Anthropic")
    return patcher, fake_stream, fake_create


def test_adapter_sends_configured_max_tokens_on_stream():
    """create_message(流式)发出配置值,而不是硬编码 4096。"""
    captured: dict = {}
    patcher, fake_stream, _ = _patch_anthropic(captured)
    with patcher as MockAnthropic:
        client = MagicMock()
        MockAnthropic.return_value = client
        client.messages.stream.side_effect = fake_stream
        from app.models.anthropic_adapter import AnthropicAdapter
        adapter = AnthropicAdapter(_base_cfg(max_tokens=32768))
        adapter.create_message(messages=[{"role": "user", "content": "hi"}])
    assert captured["max_tokens"] == 32768
    assert captured["max_tokens"] != 4096


def test_adapter_sends_configured_max_tokens_on_chat():
    """chat()(非流式)同样使用配置值。"""
    captured: dict = {}
    patcher, _, fake_create = _patch_anthropic(captured)
    with patcher as MockAnthropic:
        client = MagicMock()
        MockAnthropic.return_value = client
        client.messages.create.side_effect = fake_create
        from app.models.anthropic_adapter import AnthropicAdapter
        adapter = AnthropicAdapter(_base_cfg(max_tokens=32768))
        adapter.chat([{"role": "user", "content": "hi"}])
    assert captured["max_tokens"] == 32768


def test_explicit_argument_beats_config():
    """调用方显式传值优先级最高。"""
    captured: dict = {}
    patcher, fake_stream, _ = _patch_anthropic(captured)
    with patcher as MockAnthropic:
        client = MagicMock()
        MockAnthropic.return_value = client
        client.messages.stream.side_effect = fake_stream
        from app.models.anthropic_adapter import AnthropicAdapter
        adapter = AnthropicAdapter(_base_cfg(max_tokens=32768))
        adapter.create_message(
            messages=[{"role": "user", "content": "hi"}], max_tokens=999,
        )
    assert captured["max_tokens"] == 999


def test_openai_adapter_uses_config():
    """Responses API 路径的 max_output_tokens 同样取自配置。"""
    cfg = _base_cfg(provider="openai", api_key="k", auth_token=None, max_tokens=24000)
    captured: dict = {}

    class FakeStream:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def __iter__(self): return iter([])

    def fake_stream(**kw):
        captured.update(kw)
        return FakeStream()

    with patch("openai.OpenAI") as MockOpenAI:
        client = MagicMock()
        MockOpenAI.return_value = client
        client.responses.stream.side_effect = fake_stream
        from app.models.openai_adapter import OpenAIAdapter
        # 流结束后的收尾依赖 SDK 内部对象，fake 到不了那一步；这里只关心
        # 请求参数是否带上了正确上限，故显式捕获收尾阶段的 AttributeError。
        with pytest.raises(AttributeError):
            OpenAIAdapter(cfg).create_message(
                messages=[{"role": "user", "content": "hi"}],
            )
    assert captured.get("max_output_tokens") == 24000


def test_compatible_adapter_uses_config():
    """Chat Completions 兼容路径的 max_tokens 取自配置。"""
    from app.models.compatible_adapter import OpenAICompatibleAdapter

    cfg = _base_cfg(
        provider="openai_compatible", model="qwen2.5-coder:7b",
        api_key="k", auth_token=None, max_tokens=8192, stream=True,
    )
    captured: dict = {}

    def fake_create(**kw):
        captured.update(kw)
        stream = MagicMock()
        stream.__iter__ = lambda self: iter([])
        return stream

    with patch("openai.OpenAI") as MockOpenAI:
        client = MagicMock()
        MockOpenAI.return_value = client
        client.chat.completions.create.side_effect = fake_create
        OpenAICompatibleAdapter(cfg).create_message(
            messages=[{"role": "user", "content": "hi"}],
        )
    assert captured.get("max_tokens") == 8192
