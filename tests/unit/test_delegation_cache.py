import copy
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.agent.prefix_fork import prepare_prefix
from app.agent.subagent_runtime import _BoundedModel
from app.agent.delegation_metrics import summarize
from app.models.cache_usage import cache_usage
from app.models.router import ModelRouter
from tests.unit.test_openai_adapter import _cfg, _message_item, _FakeStream
from app.models.openai_adapter import OpenAIAdapter


@pytest.mark.parametrize("provider,usage,expected", [
    ("openai", None, {}),
    ("openai", {}, {}),
    ("openai", {"input_tokens_details": {"cached_tokens": 0}}, {"cache_read_input_tokens": 0}),
    ("compatible", {"prompt_tokens_details": {"cached_tokens": 15}}, {"cache_read_input_tokens": 15}),
    ("anthropic", {"cache_read_input_tokens": 5, "cache_creation_input_tokens": 0},
     {"cache_read_input_tokens": 5, "cache_creation_input_tokens": 0}),
])
def test_cache_usage(provider, usage, expected):
    assert cache_usage(usage, provider=provider) == expected


@pytest.mark.parametrize("enabled", [True, False])
def test_router_cache_key_and_immutable_request_snapshot(enabled):
    cfg = _cfg()
    cfg.prompt_cache_enabled = enabled
    with patch("openai.OpenAI") as sdk:
        client = sdk.return_value
        final = SimpleNamespace(
            output=[_message_item("done")], status="completed", model="gpt-5",
            usage=SimpleNamespace(input_tokens=100, output_tokens=5,
                                  input_tokens_details=SimpleNamespace(cached_tokens=0)))
        client.responses.stream.return_value = _FakeStream([], final)
        router = ModelRouter(OpenAIAdapter(cfg))
        messages = [{"role": "user", "content": "hello"}]
        result = router.create_message(messages, system="stable")
        assert result.usage["cache_read_input_tokens"] == 0
        assert ("prompt_cache_key" in client.responses.stream.call_args.kwargs) == enabled
        messages.append({"role": "user", "content": "mutated"})
        assert len(router.fork_snapshot()["messages"]) == 1
        fork = router.fork()
        assert fork._cache_group == router._cache_group
        assert fork._adapter is not router._adapter
        assert fork.fork_snapshot() is None
        assert ModelRouter(OpenAIAdapter(cfg))._cache_group != router._cache_group


def test_prefix_keeps_messages_system_and_restricts_claim():
    llm = SimpleNamespace(provider="openai", model="gpt-5", context_size=32000)
    snapshot = {"provider": "openai", "model": "gpt-5", "system": "unchanged",
                "messages": [{"role": "user", "content": "background"}], "tools": []}
    messages, system, diagnostic = prepare_prefix(snapshot, llm, "task")
    assert messages == snapshot["messages"] and system == snapshot["system"]
    assert diagnostic["mode"] == "full_prefix" and not diagnostic["tools_match"]
    messages[0]["content"] = "mutated"
    assert snapshot["messages"][0]["content"] == "background"


@pytest.mark.parametrize("change,reason", [
    ({"provider": "anthropic"}, "unsupported_provider_or_missing_request"),
    ({"model": "other"}, "model_mismatch"),
    ({"messages": [{"type": "function_call", "call_id": "orphan"}]}, "incomplete_tool_pairs"),
    ({"messages": [{"type": "reasoning", "encrypted_content": "opaque"}]}, "non_replayable_content"),
    ({"messages": [{"role": "user", "content": "api_key=secret_value"}]}, "redaction_would_change_prefix"),
    ({"messages": [{"role": "user", "content": "x" * 200000}]}, "context_capacity"),
])
def test_prefix_fallback(change, reason):
    snapshot = {"provider": "openai", "model": "gpt-5", "system": "stable",
                "messages": [{"role": "user", "content": "hi"}], **change}
    messages, _, diagnostic = prepare_prefix(snapshot, SimpleNamespace(provider="openai", model="gpt-5", context_size=32000), "task")
    assert messages is None and diagnostic["fallback_reason"] == reason


def test_metrics_preserve_cache_and_unknown_latency():
    from tests.support.delegation import response
    class Source:
        def create_message(self, messages, **kwargs):
            r = response("done")
            r.usage["cache_read_input_tokens"] = 0
            return r
    model = _BoundedModel(Source(), 2)
    model.create_message([])
    assert model.usage["cache_read_input_tokens"] == 0
    assert model.request_metrics[0]["first_generation_delta_seconds"] is None
    record = {"parent_run_id": "p", "result_id": "r", "status": "succeeded",
              "context_diagnostics": {"mode": "brief"}, "usage": model.usage,
              "metrics": {"requests": model.request_metrics, "read_signatures": ["a", "a", "b"]}}
    summary = summarize([record, copy.deepcopy(record)])["brief"]
    assert summary["runs"] == 1 and summary["repeated_read_calls"] == 1
    assert summary["quality"] == "manual_review_required"


@pytest.mark.parametrize("status,detail,falls_back", [
    (400, "unsupported parameter: prompt_cache_key", True),
    (400, "unknown parameter: temperature", False),
    (401, "unsupported prompt_cache_key", False),
])
def test_cache_parameter_fallback_is_narrow(status, detail, falls_back):
    from tests.support.delegation import response
    cfg = _cfg()
    cfg.prompt_cache_enabled = True
    calls = []
    class Adapter:
        _cfg = cfg
        provider = "openai"
        model = "gpt-5"
        def create_message(self, messages, **kwargs):
            calls.append(dict(kwargs))
            if len(calls) == 1:
                error = ValueError(detail)
                error.status_code = status
                raise error
            return response("done")
    router = ModelRouter(Adapter())
    if falls_back:
        router.create_message([])
        router.create_message([])
        assert len(calls) == 3
        assert "prompt_cache_key" in calls[0] and "prompt_cache_key" not in calls[1]
        assert "prompt_cache_key" not in calls[2]
    else:
        with pytest.raises(ValueError):
            router.create_message([])
        assert len(calls) == 1 and router.fork_snapshot() is None


@pytest.mark.parametrize("enabled", [False, True])
def test_anthropic_system_breakpoint_and_cache_usage(enabled):
    from app.models.anthropic_adapter import AnthropicAdapter
    cfg = _cfg()
    cfg.provider = "anthropic"
    cfg.prompt_cache_enabled = enabled
    with patch("anthropic.Anthropic") as sdk:
        stream = sdk.return_value.messages.stream.return_value.__enter__.return_value
        stream.__iter__.return_value = iter([])
        stream.get_final_message.return_value = SimpleNamespace(
            content=[SimpleNamespace(type="text", text="done")], stop_reason="end_turn", model="claude",
            usage=SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=20,
                                  cache_creation_input_tokens=0))
        result = AnthropicAdapter(cfg).create_message([], system="stable")
        system = sdk.return_value.messages.stream.call_args.kwargs["system"]
        assert isinstance(system, list) == enabled
        if enabled:
            assert system[0]["cache_control"] == {"type": "ephemeral"}
        assert result.usage == {"input_tokens": 10, "output_tokens": 5,
                                "cache_read_input_tokens": 20, "cache_creation_input_tokens": 0}


def test_repeated_read_signatures_use_output_version_and_normalized_paths(tmp_path):
    from app.agent.subagent_runtime import RestrictedToolRegistry
    from app.tools.registry import ToolRegistry, Tool
    value = ["v1"]
    source = ToolRegistry()
    source.register(Tool("read_file", "read", {"type": "object"}, lambda path: value[0]))
    registry = RestrictedToolRegistry(source, {"read_file"}, workspace=tmp_path)
    registry.execute("read_file", {"path": "x"})
    registry.execute("read_file", {"path": "./x"})
    value[0] = "v2"
    registry.execute("read_file", {"path": "x"})
    assert registry.read_signatures[0] == registry.read_signatures[1]
    assert registry.read_signatures[0] != registry.read_signatures[2]


@pytest.mark.parametrize("mode", ["direct", "task"])
def test_session_preserves_main_cache_usage_and_absent_is_unknown(mode, tmp_path):
    import json
    from app.agent.runtime import AgentSession
    from app.config.loader import use_workspace_root
    from tests.support.delegation import FakeRouter, registry, response

    answer = response("done")
    answer.usage["cache_read_input_tokens"] = 0
    responses = [answer, response("done")]
    expected = {"input_tokens": 1, "output_tokens": 1, "cache_read_input_tokens": 0}
    if mode == "task":
        plan = response(json.dumps({"tasks": [{"id": "t", "content": "总结材料"}]}))
        plan.usage.update(cache_read_input_tokens=5, cache_creation_input_tokens=3)
        next_plan = response(plan.text)
        responses = [plan, answer, next_plan, response("done")]
        expected.update(input_tokens=2, output_tokens=2,
                        cache_read_input_tokens=5, cache_creation_input_tokens=3)
    session = AgentSession(FakeRouter(responses), registry(), orchestrate=True, mode=mode)
    with use_workspace_root(tmp_path):
        assert session.chat("总结材料") == "done"
        assert session.last_turn_usage == expected
        assert session.cumulative_usage == expected
        assert session.chat("总结材料") == "done"
    assert "cache_read_input_tokens" not in session.last_turn_usage
    assert "cache_creation_input_tokens" not in session.last_turn_usage
    for key in ("cache_read_input_tokens", "cache_creation_input_tokens"):
        if key in expected:
            assert session.cumulative_usage[key] == expected[key]


def test_retiring_child_can_introduce_cache_usage_without_crashing(tmp_path):
    from app.agent.runtime import AgentSession
    from app.config.loader import use_workspace_root
    from tests.support.delegation import FakeRouter, registry, response

    previous = SimpleNamespace(usage={"input_tokens": 0, "output_tokens": 0}, children={})
    previous.pump = lambda: previous.usage.update(cache_read_input_tokens=4)
    session = AgentSession(FakeRouter([response("done")]), registry(), mode="direct")
    session._retiring_managers.append(previous)
    with use_workspace_root(tmp_path):
        assert session.chat("总结材料") == "done"
    assert session.cumulative_usage["cache_read_input_tokens"] == 4
    assert "cache_read_input_tokens" not in session.last_turn_usage
    assert session._retiring_managers == []
