"""Window metadata is scoped, validated, bounded, and never guessed from IDs."""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock
import pytest

from app.models.context_window import ContextWindowResolver, ContextWindow, DEFAULT_CONTEXT_LIMIT
from app.models.router import ModelRouter
from app.agent.context_budget import ContextBudget


def adapter(value=None, configured=None):
    return SimpleNamespace(_cfg=SimpleNamespace(context_size=configured),
                           fetch_context_size=Mock(return_value=value))


def test_explicit_config_skips_metadata_entirely():
    item = adapter(99999, configured=8192)
    assert ContextWindowResolver(item).resolve() == ContextWindow(8192, "config")
    item.fetch_context_size.assert_not_called()


def test_metadata_is_cached_and_concurrent_callers_share_one_probe():
    item = adapter(128000)
    resolver = ContextWindowResolver(item)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: resolver.resolve(), range(8)))
    assert results == [ContextWindow(128000, "provider")] * 8
    item.fetch_context_size.assert_called_once()


@pytest.mark.parametrize("value", [None, 0, -1, True, "128000", 128000.5])
def test_invalid_metadata_is_not_treated_as_a_limit(value):
    assert ContextWindowResolver(adapter(value)).resolve() == ContextWindow(DEFAULT_CONTEXT_LIMIT, "default")


def test_failed_probe_is_negatively_cached_and_retried_after_expiration(monkeypatch):
    from app.models import context_window
    clock = [100.0]
    monkeypatch.setattr(context_window.time, "monotonic", lambda: clock[0])
    item = adapter()
    item.fetch_context_size.side_effect = [RuntimeError("offline"), 200000]
    resolver = ContextWindowResolver(item)
    assert resolver.resolve().source == "default"
    clock[0] += 59
    assert resolver.resolve().source == "default"
    assert item.fetch_context_size.call_count == 1
    clock[0] += 2
    assert resolver.resolve() == ContextWindow(200000, "provider")
    clock[0] += 599
    resolver.resolve()
    assert item.fetch_context_size.call_count == 2


def test_router_caches_do_not_cross_endpoints_or_credentials():
    a, b = ModelRouter(adapter(128000)), ModelRouter(adapter(32000))
    assert a.context_size == 128000
    assert b.context_size == 32000
    budget = ContextBudget.from_llm(a)
    assert budget.model_context_limit == 128000
    assert budget.context_limit_source == "provider"


def test_unsupported_provider_uses_default_without_sdk_request():
    assert ContextBudget.from_llm(ModelRouter(SimpleNamespace())).context_limit_source == "default"


def test_anthropic_uses_documented_field_with_bounded_probe():
    from app.models.anthropic_adapter import AnthropicAdapter
    item = AnthropicAdapter.__new__(AnthropicAdapter)
    item._cfg = SimpleNamespace(model="new-model")
    item._client = Mock()
    item._client.with_options.return_value.models.retrieve.return_value = SimpleNamespace(max_input_tokens=150000)
    assert item.fetch_context_size() == 150000
    item._client.with_options.assert_called_once_with(timeout=3, max_retries=0)
    item._client.with_options.return_value.models.retrieve.assert_called_once_with("new-model")


def test_ollama_uses_actual_runtime_window_and_matches_exact_model():
    from app.models.compatible_adapter import OpenAICompatibleAdapter
    item = OpenAICompatibleAdapter.__new__(OpenAICompatibleAdapter)
    item._cfg = SimpleNamespace(provider="ollama", model="local-model")
    item._client = Mock(base_url="http://localhost:11434/v1/")
    item._client.with_options.return_value.get.return_value.json.return_value = {"models": [
        {"model": "other-model", "context_length": 999999},
        {"model": "local-model:latest", "context_length": 8192},
    ]}
    assert item.fetch_context_size() == 8192
    item._client.with_options.assert_called_once_with(base_url="http://localhost:11434/", timeout=3, max_retries=0)
    item._cfg.provider = "openai_compatible"
    assert item.fetch_context_size() is None
