import json
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

import pytest

from app.auth.chatgpt import AuthError, PERMISSION, TokenManager, _atomic_json
from app.models.chatgpt_adapter import ChatGPTAdapter, ChatGPTError
from app.models.openai_adapter import ProviderStreamError
from app.models.protocol import ToolResult
from app.config.loader import load_config


def record(**overrides):
    return {"client_id": "oaiapp_test", "subject": "user1", "access_token": "old",
            "refresh_token": "refresh", "scopes": [PERMISSION],
            "expires_at": time.time() + 3600, **overrides}


def test_refresh_serializes_across_managers(tmp_path, monkeypatch):
    _atomic_json(tmp_path / "default.json", record(expires_at=0))
    exchanges = []
    def exchange(data):
        exchanges.append(data)
        time.sleep(.02)
        return {"access_token": "new", "refresh_token": "rotated", "token_type": "Bearer", "expires_in": 3600}
    monkeypatch.setattr(TokenManager, "_exchange", staticmethod(exchange))
    managers = [TokenManager(root=tmp_path) for _ in range(8)]
    with ThreadPoolExecutor(8) as pool:
        assert list(pool.map(lambda m: m.access_token(), managers)) == ["new"] * 8
    assert len(exchanges) == 1
    assert managers[0].read()["refresh_token"] == "rotated"
    assert (tmp_path / "default.json").stat().st_mode & 0o777 == 0o600
    assert "scope" not in exchanges[0]


def test_refresh_serializes_across_processes(tmp_path):
    import subprocess
    import sys
    _atomic_json(tmp_path / "default.json", record(expires_at=0))
    program = '''
import sys, time
from pathlib import Path
from app.auth.chatgpt import TokenManager
root = Path(sys.argv[1])
def exchange(data):
    with (root / "refresh-count").open("a") as out:
        out.write("refresh\\n")
    time.sleep(.05)
    return {"access_token":"new", "refresh_token":"rotated", "token_type":"Bearer", "expires_in":3600}
TokenManager._exchange = staticmethod(exchange)
assert TokenManager(root=root).access_token() == "new"
'''
    children = [subprocess.Popen([sys.executable, "-c", program, str(tmp_path)]) for _ in range(4)]
    assert [child.wait(timeout=15) for child in children] == [0] * 4
    assert (tmp_path / "refresh-count").read_text().splitlines() == ["refresh"]


@pytest.mark.parametrize("terminal", [True, False])
def test_refresh_error_preserves_identity_and_handles_tokens(tmp_path, monkeypatch, terminal):
    manager = TokenManager(root=tmp_path)
    _atomic_json(manager.path, record(expires_at=0))
    def fail(data):
        error = AuthError("refresh failure")
        error.terminal = terminal
        raise error
    monkeypatch.setattr(manager, "_exchange", fail)
    with pytest.raises(AuthError):
        manager.access_token()
    saved = manager.read()
    assert saved["client_id"] == "oaiapp_test"
    assert ("refresh_token" not in saved) == terminal


def test_missing_scope_prevents_inference(tmp_path):
    manager = TokenManager(root=tmp_path)
    _atomic_json(manager.path, record(scopes=[]))
    with pytest.raises(AuthError, match="尚未授权"):
        manager.access_token()


def test_account_path_traversal_rejected(tmp_path):
    with pytest.raises(AuthError):
        TokenManager("../other", root=tmp_path)


def test_logout_clears_tokens_keeps_client_on_network_failure(tmp_path, monkeypatch):
    import httpx
    manager = TokenManager(root=tmp_path)
    _atomic_json(manager.path, record(id_token="secret"))
    def fail(*args, **kwargs):
        raise httpx.ConnectError("offline")
    monkeypatch.setattr(httpx, "get", fail)
    assert manager.logout() is False
    assert manager.read() == {"client_id": "oaiapp_test", "subject": "user1"}


@pytest.fixture
def adapter(tmp_path, monkeypatch):
    monkeypatch.setenv("CHATGPT_AUTH_DIR", str(tmp_path))
    return ChatGPTAdapter(load_config("chatgpt_codex"))


def item(**data):
    def dump(value):
        if isinstance(value, NS):
            return dump(vars(value))
        if isinstance(value, dict):
            return {key: dump(part) for key, part in value.items()}
        if isinstance(value, list):
            return [dump(part) for part in value]
        return value
    return NS(**data, model_dump=lambda **kwargs: dump(data))


def set_events(adapter, monkeypatch, events):
    client = MagicMock()
    client.responses.create.return_value.__enter__.return_value = iter(events)
    client.__enter__.return_value = client
    monkeypatch.setattr(adapter, "_client", lambda: client)
    return client


def complete(output):
    return NS(type="response.completed", response=NS(status="completed", output=output,
              usage=NS(input_tokens=10, output_tokens=3), model="gpt-6.1-sol"))


TOOLS = [{"name": "echo", "input_schema": {"type": "object", "properties": {}}}]


def test_request_constraints_and_history_namespace(adapter):
    messages = [{"role": "system", "content": "policy"},
                {"type": "function_call", "call_id": "c1", "name": "echo", "arguments": "{}", "namespace": "agentlab"},
                {"type": "function_call_output", "call_id": "c1", "output": "approved result"}]
    params = adapter._params(messages, TOOLS, "instructions", "high")
    assert params["store"] is False and params["stream"] is True
    assert not {"temperature", "top_p", "max_output_tokens", "previous_response_id"} & params.keys()
    assert params["input"][0]["role"] == "developer"
    assert params["input"][1]["namespace"] == "agentlab"
    assert params["tools"][0]["type"] == "namespace"
    assert params["input"][2]["output"] == "approved result"
    assert "access_token" not in vars(adapter._cfg)


def test_completed_tool_then_result_replay(adapter, monkeypatch):
    call = item(type="function_call", name="echo", namespace="agentlab", call_id="c1", arguments='{"value": 1}')
    set_events(adapter, monkeypatch, [complete([call])])
    first = adapter.create_message([], tools=TOOLS)
    assert first.tool_calls[0].arguments == {"value": 1}
    results = adapter.format_tool_results([ToolResult("c1", "denied", True)])
    assert results[0]["output"] == "ERROR: denied"
    params = adapter._params(first.provider_payload + results, TOOLS, None, None)
    assert params["input"][0]["namespace"] == "agentlab"
    assert params["input"][1]["call_id"] == "c1"


def test_no_completed_event_never_returns_tool_calls(adapter, monkeypatch):
    call = item(type="function_call", name="echo", call_id="c1", arguments="{}")
    set_events(adapter, monkeypatch, [NS(type="response.output_item.done", output_index=0, item=call)])
    with pytest.raises(ProviderStreamError, match="未完成"):
        adapter.create_message([], tools=TOOLS)


def test_completed_recovers_streamed_text_and_replay(adapter, monkeypatch):
    seen = []
    set_events(adapter, monkeypatch, [
        NS(type="response.output_text.delta", output_index=0, delta="Hello "),
        NS(type="response.output_text.delta", output_index=0, delta="world"),
        complete([])])
    result = adapter.create_message([], on_text_delta=seen.append)
    assert seen == ["Hello ", "world"]
    assert result.text == "Hello world"
    assert adapter._params(result.provider_payload, None, None, None)["input"][0]["content"][0]["text"] == result.text


def test_completed_recovers_done_items_in_order(adapter, monkeypatch):
    msg = item(type="message", role="assistant", phase="commentary",
               content=[NS(type="output_text", text="Checking")])
    call = item(type="function_call", name="echo", namespace="agentlab", call_id="c1", arguments="{}")
    set_events(adapter, monkeypatch, [
        NS(type="response.output_item.done", output_index=1, item=call),
        NS(type="response.output_item.done", output_index=0, item=msg), complete([])])
    result = adapter.create_message([], tools=TOOLS)
    assert result.text == "Checking"
    assert result.tool_calls[0].id == "c1"
    assert [v["type"] for v in result.provider_payload] == ["message", "function_call"]
    assert result.provider_payload[0]["phase"] == "commentary"


def test_terminal_text_wins_without_duplicate_deltas(adapter, monkeypatch):
    msg = item(type="message", role="assistant", content=[NS(type="output_text", text="Final")])
    set_events(adapter, monkeypatch, [
        NS(type="response.output_text.delta", output_index=0, delta="Draft"),
        NS(type="response.output_item.done", output_index=0, item=msg), complete([msg])])
    result = adapter.create_message([])
    assert result.text == "Final"
    assert len(result.provider_payload) == 1


@pytest.mark.parametrize("terminal", [None, "response.incomplete", "response.failed"])
def test_streamed_text_does_not_mask_incomplete_response(adapter, monkeypatch, terminal):
    events = [NS(type="response.output_text.delta", output_index=0, delta="Partial")]
    if terminal:
        events.append(NS(type=terminal))
    set_events(adapter, monkeypatch, events)
    with pytest.raises(ProviderStreamError):
        adapter.create_message([])


def test_argument_deltas_are_not_recovered_as_tool_calls(adapter, monkeypatch):
    set_events(adapter, monkeypatch, [
        NS(type="response.function_call_arguments.delta", delta="{}"), complete([])])
    with pytest.raises(ProviderStreamError, match="没有返回文本或工具调用"):
        adapter.create_message([], tools=TOOLS)


@pytest.mark.parametrize("args", ["{broken", "[]", "null"])
def test_malformed_tool_arguments_are_not_executed(adapter, monkeypatch, args):
    call = item(type="function_call", name="echo", call_id="c1", arguments=args)
    set_events(adapter, monkeypatch, [complete([call])])
    with pytest.raises(ProviderStreamError, match="无效的工具参数"):
        adapter.create_message([], tools=TOOLS)


@pytest.mark.parametrize("code,retry", [("subscription_sharing_usage_limit_exceeded", False),
    ("subscription_sharing_user_not_eligible", False), ("subscription_sharing_usage_unavailable", True)])
def test_stream_errors_follow_oauth_policy(adapter, monkeypatch, code, retry):
    set_events(adapter, monkeypatch, [NS(type="response.failed", response=NS(error=NS(code=code)))])
    with pytest.raises(ChatGPTError) as error:
        adapter.create_message([])
    assert error.value.retryable == retry


def test_chat_also_uses_stream(adapter, monkeypatch):
    msg = item(type="message", role="assistant", content=[NS(type="output_text", text="OK")])
    client = set_events(adapter, monkeypatch, [complete([msg])])
    assert adapter.chat([{"role": "user", "content": "hello"}]) == "OK"
    assert client.responses.create.call_args.kwargs["stream"] is True


def test_identity_signature_nonce_and_audience(monkeypatch):
    import jwt
    import httpx
    from cryptography.hazmat.primitives.asymmetric import rsa
    from app.auth.chatgpt import validate_identity, ISSUER
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    claims = {"iss": ISSUER, "aud": "oaiapp_test", "sub": "user1", "exp": int(time.time())+300, "nonce": "test-nonce"}
    token = jwt.encode(claims, key, algorithm="RS256")
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(200,
        request=httpx.Request("GET", ISSUER), json={"jwks_uri": ISSUER+"/jwks"}))
    monkeypatch.setattr(jwt.PyJWKClient, "get_signing_key_from_jwt", lambda *a: NS(key=key.public_key()))
    assert validate_identity(token, "oaiapp_test", "test-nonce")["sub"] == "user1"
    for client_id, nonce in [("wrong", "test-nonce"), ("oaiapp_test", "wrong")]:
        with pytest.raises(AuthError):
            validate_identity(token, client_id, nonce)


def test_auth_cli_dispatch_precedes_session(monkeypatch, capsys, tmp_path):
    from app.cli import main
    monkeypatch.setenv("CHATGPT_AUTH_DIR", str(tmp_path))
    monkeypatch.setenv("ACTIVE_PROFILE", "nonexistent")
    assert main(["auth", "status", "chatgpt"]) == 0
    assert '"signed_in": false' in capsys.readouterr().out


@pytest.mark.parametrize("returning", [False, True])
def test_login_pkce_callback_and_issued_client(tmp_path, monkeypatch, returning):
    import io
    from urllib.parse import urlparse, parse_qs, urlencode
    import app.auth.chatgpt as auth
    manager = TokenManager(root=tmp_path)
    if returning:
        _atomic_json(manager.path, record())
    pending = {}
    def open_browser(url):
        pending.update({k: v[0] for k, v in parse_qs(urlparse(url).query).items()})
        return True
    class Server:
        server_port = 12345
        def __init__(self, address, handler):
            assert address == ("127.0.0.1", 0)
            self.handler = handler
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def handle_request(self):
            handler = self.handler.__new__(self.handler)
            handler.send_response = lambda code: None
            handler.send_header = lambda *a: None
            handler.end_headers = lambda: None
            handler.wfile = io.BytesIO()
            # Exercise invalid state, then a valid callback on the same listener.
            for state in ["forged", pending["state"]]:
                handler.path = "/auth/callback?" + urlencode(dict(code="test-code", state=state, client_id="oaiapp_test"))
                handler.do_GET()
    monkeypatch.setattr(auth, "HTTPServer", Server)
    monkeypatch.setattr(auth.webbrowser, "open", open_browser)
    monkeypatch.setattr(auth, "validate_identity", lambda token, client, nonce: {"sub": "user1", "email": "test@example.com"})
    def exchange(data):
        import base64, hashlib
        assert data["client_id"] == "oaiapp_test"
        assert data["redirect_uri"] == pending["redirect_uri"]
        assert base64.urlsafe_b64encode(hashlib.sha256(data["code_verifier"].encode()).digest()).decode().rstrip("=") == pending["code_challenge"]
        return {"access_token": "access", "refresh_token": "refresh", "id_token": "id",
                "expires_in": 3600, "token_type": "Bearer", "scope": auth.SCOPES}
    monkeypatch.setattr(manager, "_exchange", exchange)
    manager.login()
    assert manager.read()["client_id"] == "oaiapp_test"
    assert pending["client_id"] == ("oaiapp_test" if returning else "dynamic_agent_client")
    assert ("agent_name_hint" in pending) != returning


def test_cancel_before_request_does_not_connect(adapter, monkeypatch):
    from app.agent.cancel import CancelToken
    cancel = CancelToken()
    cancel.cancel()
    connect = MagicMock()
    monkeypatch.setattr(adapter, "_client", connect)
    with pytest.raises(Exception):
        adapter.create_message([], cancel=cancel)
    connect.assert_not_called()


def test_real_sdk_serializes_namespace_and_reads_sse(adapter, monkeypatch):
    import openai
    import openai._base_client as base
    transport_api = getattr(base, "httpx", None) or base.httpx2
    requests = []
    def handle(request):
        requests.append(request)
        response = {"id": "resp_test", "object": "response", "status": "completed",
            "model": "gpt-6.1-sol", "output": [{"type": "function_call", "id": "fc1",
            "call_id": "call1", "namespace": "agentlab", "name": "echo", "arguments": "{}"}],
            "usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12}}
        event = json.dumps({"type": "response.completed", "sequence_number": 1, "response": response})
        return transport_api.Response(200, headers={"content-type": "text/event-stream"},
                                      content=f"event: response.completed\ndata: {event}\n\ndata: [DONE]\n\n")
    client = openai.OpenAI(api_key="test-token", base_url="https://api.openai.com/v1",
        http_client=transport_api.Client(transport=transport_api.MockTransport(handle)), max_retries=0)
    monkeypatch.setattr(adapter, "_client", lambda: client)
    result = adapter.create_message([{"role": "user", "content": "call echo"}], tools=TOOLS)
    assert result.tool_calls[0].name == "echo"
    assert result.provider_payload[0]["namespace"] == "agentlab"
    body = json.loads(requests[0].content)
    assert body["tools"][0]["tools"][0]["name"] == "echo"
    assert body["store"] is False and body["stream"] is True
    assert requests[0].headers["authorization"] == "Bearer test-token"


def test_long_run_oauth_retries_remain_bounded(adapter, monkeypatch):
    import app.models.router as routing
    seen = {}
    def retry(fn, **kwargs):
        seen.update(kwargs)
        return "completed"
    monkeypatch.setattr(routing, "call_with_retry", retry)
    assert routing.ModelRouter(adapter).create_message([], retry_until_cancelled=True) == "completed"
    assert seen["max_retries"] == 2


def test_context_window_uses_account_metadata_and_caches(adapter, monkeypatch):
    import httpx
    from app.models.router import ModelRouter
    from app.agent.context_budget import ContextBudget
    get = MagicMock(return_value=httpx.Response(200,
        request=httpx.Request("GET", "https://api.openai.com/v1/models"),
        json={"models": [{"slug": "another-model", "context_window": 1000},
                          {"slug": "gpt-6.1-sol", "context_window": 500000}]}))
    monkeypatch.setattr(httpx, "get", get)
    monkeypatch.setattr(adapter.tokens, "access_token", lambda: "test-token")
    router = ModelRouter(adapter)
    budget = ContextBudget.from_llm(router)
    assert router.context_window.tokens == 500000
    assert router.context_window.source == "provider"
    assert budget.compact_threshold == 452463
    assert budget.status_for(60000) == "ok"
    assert get.call_count == 1
    assert get.call_args.kwargs["timeout"] == 3
    assert get.call_args.kwargs["follow_redirects"] is False


@pytest.mark.parametrize("value", [None, 0, -1, True, "500000"])
def test_invalid_account_window_falls_back(adapter, monkeypatch, value):
    import httpx
    from app.models.router import ModelRouter
    monkeypatch.setattr(adapter.tokens, "access_token", lambda: "test-token")
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(200,
        request=httpx.Request("GET", "https://api.openai.com/v1/models"),
        json={"models": [{"slug": "gpt-6.1-sol", "context_window": value}]}))
    router = ModelRouter(adapter)
    assert router.context_window.tokens == 64000
    assert router.context_window.source == "default"


def test_configured_window_skips_catalog(adapter, monkeypatch):
    from app.models.router import ModelRouter
    adapter._cfg.context_size = 100000
    fetch = MagicMock(side_effect=AssertionError("must not fetch"))
    monkeypatch.setattr(adapter, "fetch_context_size", fetch)
    assert ModelRouter(adapter).context_window.tokens == 100000
    fetch.assert_not_called()


def test_stream_progress_counts_arguments_and_marks_final_usage(adapter, monkeypatch):
    call = item(type="function_call", name="echo", call_id="c1", arguments="{}")
    events = [NS(type="response.reasoning_summary_text.delta", delta="Planning"),
              NS(type="response.function_call_arguments.delta", delta='{"value":"hello"}'), complete([call])]
    set_events(adapter, monkeypatch, events)
    metrics = []
    result = adapter.create_message([], tools=TOOLS, on_progress=metrics.append)
    assert result.reasoning == "Planning"
    assert metrics[0] == {"input_tokens": 0, "output_tokens": 0}
    assert metrics[2]["output_tokens"] > metrics[1]["output_tokens"]
    assert metrics[-1] == {"input_tokens": 10, "output_tokens": 3, "final": True}


def test_stream_failure_keeps_request_diagnostics(adapter, monkeypatch):
    class Events(list):
        response = NS(status_code=200, headers={"x-request-id": "req_stream"})
    events = Events([NS(type="response.failed", response=NS(error=NS(
        code="subscription_sharing_unsupported_capability", param="tools")))])
    set_events(adapter, monkeypatch, [])
    client = MagicMock()
    client.__enter__.return_value = client
    client.responses.create.return_value.__enter__.return_value = events
    monkeypatch.setattr(adapter, "_client", lambda: client)
    with pytest.raises(ChatGPTError) as exc:
        adapter.create_message([])
    assert exc.value.request_id == "req_stream"
    assert exc.value.param == "tools"
    assert exc.value.status_code == 200
    assert not exc.value.retryable


def test_http_rate_limit_preserves_retry_after(adapter, monkeypatch):
    import openai
    import openai._base_client as base
    from app.models.provider_retry import classify_provider_error
    transport_api = getattr(base, "httpx", None) or base.httpx2
    response = transport_api.Response(429, request=transport_api.Request("POST", "https://api.openai.com/v1/responses"),
        headers={"retry-after": "7", "x-request-id": "req_limit"})
    client = MagicMock()
    client.__enter__.return_value = client
    client.responses.create.side_effect = openai.RateLimitError("rate limited", response=response, body={"detail": "busy"})
    monkeypatch.setattr(adapter, "_client", lambda: client)
    with pytest.raises(ChatGPTError) as exc:
        adapter.create_message([])
    decision = classify_provider_error(exc.value)
    assert decision.retryable
    assert decision.retry_after_seconds == 7
    assert exc.value.request_id == "req_limit"


def test_duplicate_call_ids_cannot_be_executed_twice(adapter, monkeypatch):
    call = item(type="function_call", name="echo", call_id="same", arguments="{}")
    set_events(adapter, monkeypatch, [complete([call, call])])
    with pytest.raises(ProviderStreamError, match="重复"):
        adapter.create_message([], tools=TOOLS)


def test_oauth_prefix_fork_requires_same_account_and_provider(adapter):
    from app.agent.prefix_fork import prepare_prefix
    from app.models.router import ModelRouter
    adapter._cfg.context_size = 64000
    llm = ModelRouter(adapter)
    snapshot = {"provider": "openai_chatgpt", "model": llm.model,
                "chatgpt_account": adapter._cfg.chatgpt_account,
                "messages": [{"role": "user", "content": "review this task"}], "system": "policy", "tools": []}
    messages, system, diagnostics = prepare_prefix(snapshot, llm, "check")
    assert diagnostics["mode"] == "full_prefix"
    assert system == "policy" and messages == snapshot["messages"]
    assert messages is not snapshot["messages"]
    snapshot["chatgpt_account"] = "other"
    assert prepare_prefix(snapshot, llm, "check")[2]["fallback_reason"] == "account_mismatch"
    snapshot["provider"] = "openai"
    assert prepare_prefix(snapshot, llm, "check")[2]["fallback_reason"] == "unsupported_provider_or_missing_request"


def test_auth_smoke_profile_never_uses_api_key_billing(tmp_path, monkeypatch, capsys):
    from app.auth.cli import main
    import app.config.loader as loader
    monkeypatch.setenv("CHATGPT_AUTH_DIR", str(tmp_path))
    requested = []
    def load(name):
        requested.append(name)
        return NS(provider="openai")
    monkeypatch.setattr(loader, "load_config", load)
    assert main(["test", "chatgpt", "--profile", "other"]) == 1
    assert requested == ["other"]
    assert "不会切换到 API Key" in capsys.readouterr().out


def test_cancel_during_transport_is_not_reported_as_network_failure(adapter, monkeypatch):
    from app.agent.cancel import CancelToken, Cancelled
    cancel = CancelToken()
    def connect():
        cancel.cancel()
        raise RuntimeError("stream closed")
    monkeypatch.setattr(adapter, "_client", connect)
    with pytest.raises(Cancelled):
        adapter.create_message([], cancel=cancel)


def test_main_shows_oauth_repair_action_without_traceback(monkeypatch, capsys):
    from app import cli
    def build(**kwargs):
        raise AuthError("请先运行 agentlab auth login chatgpt")
    monkeypatch.setattr(cli, "_build_session", build)
    assert cli.main([]) == 1
    error = capsys.readouterr().err
    assert "agentlab auth login chatgpt" in error
    assert "Traceback" not in error


def test_reasoning_replay_survives_storage_and_keeps_original_order(adapter):
    import copy
    state = {"type": "reasoning", "id": "rs_1", "summary": [],
             "encrypted_content": "opaque_ciphertext", "status": "completed", "content": None}
    messages = [{"role": "user", "content": "inspect"}, state,
                {"type": "message", "role": "assistant", "phase": "commentary", "content": "Checking"},
                {"type": "function_call", "call_id": "c1", "namespace": "agentlab", "name": "echo", "arguments": "{}"},
                {"type": "function_call_output", "call_id": "c1", "output": "done"},
                {"type": "message", "role": "assistant", "phase": "final_answer", "content": "Finished"}]
    original = copy.deepcopy(messages)
    restored = json.loads(json.dumps(messages))
    inputs = adapter._params(restored, TOOLS, None, "medium")["input"]
    assert [m["type"] for m in inputs] == ["message", "reasoning", "message", "function_call", "function_call_output", "message"]
    assert inputs[1] == {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "opaque_ciphertext"}
    assert inputs[2]["phase"] == "commentary"
    assert inputs[3]["namespace"] == "agentlab"
    assert inputs[5]["phase"] == "final_answer"
    inputs[1]["summary"].append({"type": "summary_text", "text": "changed"})
    assert messages == original and restored == original


def test_missing_encrypted_reasoning_is_not_replayed_as_plain_text(adapter):
    history = [{"type": "reasoning", "id": "rs_empty", "summary": [{"type": "summary_text", "text": "summary only"}]},
               {"role": "user", "content": "continue"}]
    assert [m["type"] for m in adapter._params(history, None, None, None)["input"]] == ["message"]


def test_compaction_does_not_send_opaque_reasoning_to_summary_model():
    from app.agent.context_compaction import ContextCompressor
    history = [{"type": "reasoning", "id": "rs1", "encrypted_content": "opaque_ciphertext"},
               {"role": "user", "content": "preserve this request"}]
    rendered = ContextCompressor._render_transcript(history)
    assert "opaque_ciphertext" not in rendered and "encrypted_content" not in rendered
    assert "preserve this request" in rendered


def test_oauth_child_prefix_preserves_encrypted_reasoning(adapter):
    from app.models.router import ModelRouter
    from app.agent.prefix_fork import prepare_prefix
    adapter._cfg.context_size = 64000
    snapshot = {"provider": "openai_chatgpt", "model": adapter.model,
        "chatgpt_account": adapter._cfg.chatgpt_account, "system": "policy", "tools": [],
        "messages": [{"role": "user", "content": "inspect"},
                     {"type": "reasoning", "id": "rs1", "summary": [], "encrypted_content": "opaque_ciphertext"}]}
    messages, _, diagnostic = prepare_prefix(snapshot, ModelRouter(adapter), "continue")
    assert diagnostic["mode"] == "full_prefix"
    assert messages[-1]["encrypted_content"] == "opaque_ciphertext"


def test_real_sdk_sends_reasoning_back_after_tool_call(adapter, monkeypatch):
    import openai
    import openai._base_client as base
    transport_api = getattr(base, "httpx", None) or base.httpx2
    bodies = []
    def handle(request):
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            output = [
                {"type": "reasoning", "id": "rs1", "summary": [], "encrypted_content": "opaque_ciphertext"},
                {"type": "message", "id": "m1", "role": "assistant", "phase": "commentary",
                 "content": [{"type": "output_text", "text": "Checking", "annotations": []}], "status": "completed"},
                {"type": "function_call", "id": "fc1", "call_id": "call1", "namespace": "agentlab", "name": "echo", "arguments": "{}"}]
        else:
            output = [{"type": "message", "id": "m2", "role": "assistant", "phase": "final_answer",
                "content": [{"type": "output_text", "text": "Done", "annotations": []}], "status": "completed"}]
        response = {"id": "resp_test", "object": "response", "status": "completed", "model": adapter.model,
                    "output": output, "usage": {"input_tokens": 20, "output_tokens": 10, "total_tokens": 30}}
        event = json.dumps({"type": "response.completed", "sequence_number": 1, "response": response})
        return transport_api.Response(200, headers={"content-type": "text/event-stream"},
                                      content=f"data: {event}\n\ndata: [DONE]\n\n")
    def client():
        return openai.OpenAI(api_key="test-token", base_url="https://api.openai.com/v1", max_retries=0,
            http_client=transport_api.Client(transport=transport_api.MockTransport(handle)))
    monkeypatch.setattr(adapter, "_client", client)
    history = [{"role": "user", "content": "inspect"}]
    first = adapter.create_message(history, tools=TOOLS)
    history.extend(json.loads(json.dumps(first.provider_payload)))
    history.extend(adapter.format_tool_results([ToolResult("call1", "local result")]))
    second = adapter.create_message(history, tools=TOOLS)
    assert second.text == "Done"
    sent = bodies[1]["input"]
    assert [m["type"] for m in sent] == ["message", "reasoning", "message", "function_call", "function_call_output"]
    assert sent[1]["encrypted_content"] == "opaque_ciphertext"
    assert sent[2]["phase"] == "commentary"
    assert sent[3]["namespace"] == "agentlab"
