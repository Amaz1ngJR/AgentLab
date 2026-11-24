import copy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.models.append_prefix import prepare_history
from app.models.chatgpt_adapter import ChatGPTAdapter
from app.models.router import ModelRouter, call_create_message
from app.models.protocol import ModelResponse
from app.models.openai_adapter import ProviderStreamError
from app.agent.cancel import CancelToken


READ = {"name": "read", "input_schema": {"type": "object", "properties": {}}}
WRITE = {"name": "write", "input_schema": {"type": "object", "properties": {}}}


def adapter():
    result = ChatGPTAdapter.__new__(ChatGPTAdapter)
    result._cfg = SimpleNamespace(model="gpt-6.1-sol", context_size=64000, chatgpt_account="default", reasoning_effort="medium")
    return result


def test_append_updates_do_not_rewrite_previous_wire_prefix():
    a = adapter()
    messages = [{"role": "user", "content": "inspect"}]
    prepare_history(messages, [READ], "base")
    first = a._params(messages, [READ], None, "medium")
    assert [item.get("role") for item in first["input"]] == ["developer", "developer", "user"]
    messages.extend([{"type": "reasoning", "id": "rs1", "summary": [], "encrypted_content": "opaque"},
                     {"role": "user", "content": "edit"}])
    before = copy.deepcopy(messages)
    prepare_history(messages, [WRITE, READ], "updated skill instructions")
    second = a._params(messages, [WRITE, READ], None, "medium")
    assert messages[:len(before)] == before
    assert second["input"][:len(first["input"])] == first["input"]
    assert "tools" not in first and "tools" not in second
    assert "instructions" not in first and "instructions" not in second
    assert messages[-1]["type"] == "additional_tools"
    assert [t["name"] for t in messages[-1]["tools"]] == ["write"]
    assert second["input"][-2]["role"] == "developer"
    assert "_agentlab_instruction" not in second["input"][-2]
    assert "替代此前" in second["input"][-2]["content"][0]["text"]
    saved = json.loads(json.dumps(messages))
    prepare_history(saved, [READ, WRITE], "updated skill instructions")
    assert saved == messages


def test_tool_removal_reordering_and_restore_only_change_allowlist():
    messages = []
    prepare_history(messages, [READ, WRITE], "base")
    original = json.loads(json.dumps(messages))
    prepare_history(messages, [WRITE], "base")
    assert messages == original
    params = adapter()._params(messages, [WRITE], None, "medium")
    assert params["tool_choice"]["tools"] == [{"type": "function", "name": "write"}]
    assert adapter()._params(messages, [], None, "medium")["tool_choice"] == "none"
    prepare_history(messages, [WRITE, READ], "base")
    assert messages == original


def test_schema_change_appends_version_without_rewriting_history():
    messages = []
    prepare_history(messages, [READ], "base")
    original = copy.deepcopy(messages)
    changed = {**READ, "description": "changed"}
    prepare_history(messages, [changed], "base")
    assert messages[:len(original)] == original
    version = messages[-1]["tools"][0]["name"]
    assert version.startswith("read__v_") and len(version) <= 64
    assert adapter()._params(messages, [changed], None, "medium")["tool_choice"]["tools"] == [{"type": "function", "name": version}]
    saved = json.loads(json.dumps(messages))
    prepare_history(saved, [changed], "base")
    assert saved == messages


@pytest.mark.parametrize("cancel", [None, "enabled"])
def test_router_commits_updates_with_or_without_request_isolation(cancel):
    a = adapter()
    a.create_message = Mock(return_value=ModelResponse("ok", [], {}, []))
    router = ModelRouter(a)
    messages = [{"role": "user", "content": "inspect"}]
    call_create_message(router, messages, tools=[READ], system="base",
                        cancel=CancelToken() if cancel else None)
    assert [m.get("type") for m in messages] == ["message", "additional_tools", None]
    assert a.create_message.call_args.kwargs["system"] is None
    original = copy.deepcopy(messages)
    call_create_message(router, messages, tools=[READ], system="base",
                        cancel=CancelToken() if cancel else None)
    assert messages == original


def test_empty_instructions_do_not_append_forever():
    messages = []
    prepare_history(messages, [], None)
    original = copy.deepcopy(messages)
    prepare_history(messages, [], None)
    assert messages == original


def test_existing_transcript_without_managed_prefix_is_not_reordered():
    messages = [{"role": "user", "content": "inspect"}, {"role": "assistant", "content": "done"}]
    original = copy.deepcopy(messages)
    prepare_history(messages, [READ], "base")
    assert messages[:len(original)] == original


def test_failed_isolated_request_does_not_commit_prefix_updates():
    a = adapter()
    a.create_message = Mock(side_effect=ProviderStreamError("failed"))
    messages = [{"role": "user", "content": "inspect"}]
    before = copy.deepcopy(messages)
    with pytest.raises(ProviderStreamError):
        call_create_message(ModelRouter(a), messages, tools=[READ], system="base", cancel=CancelToken())
    assert messages == before


def test_dynamic_spawn_budget_maps_wire_version_to_current_local_tool():
    from app.models.protocol import ToolResult
    a = adapter()
    original = {"name": "spawn_agent", "input_schema": {"type": "object", "properties": {
        "max_steps": {"type": "integer", "maximum": 16}}}}
    changed = copy.deepcopy(original)
    changed["input_schema"]["properties"]["max_steps"]["maximum"] = 10000
    messages = [{"role": "user", "content": "delegate"}]
    prepare_history(messages, [original], "base")
    prepare_history(messages, [changed], "base")
    version = messages[-1]["tools"][0]["name"]
    value = {"type": "function_call", "id": "fc1", "name": version, "call_id": "c1", "arguments": '{"max_steps":20}'}
    result_item = SimpleNamespace(**value, model_dump=lambda **kwargs: value.copy())
    final = SimpleNamespace(status="completed", output=[result_item], usage=None, model=a.model)
    from unittest.mock import MagicMock
    client = MagicMock()
    client.__enter__.return_value = client
    client.responses.create.return_value.__enter__.return_value = iter([
        SimpleNamespace(type="response.completed", response=final)])
    a._client = lambda: client
    result = a.create_message(messages, tools=[changed])
    assert result.tool_calls[0].name == "spawn_agent"
    assert result.tool_calls[0].arguments == {"max_steps": 20}
    assert result.provider_payload[0]["name"] == version
    before = a._params(messages, [changed], None, "medium")["input"]
    messages.extend(result.provider_payload)
    messages.extend(a.format_tool_results([ToolResult("c1", "started")]))
    replay = a._params(json.loads(json.dumps(messages)), [changed], None, "medium")
    assert replay["input"][:len(before)] == before
    assert replay["input"][-2]["name"] == version
    assert replay["input"][-1]["call_id"] == "c1"
    assert replay["tool_choice"]["tools"] == [{"type": "function", "name": version}]
    # Reverting a budget selects the original version without appending duplicates.
    size = len(messages)
    prepare_history(messages, [original], "base")
    assert len(messages) == size
    assert a._params(messages, [original], None, "medium")["tool_choice"]["tools"] == [{"type": "function", "name": "spawn_agent"}]


def test_compaction_rebuilds_only_current_state_and_tail_dependencies():
    from app.agent.context_compaction import ContextCompressor
    from app.models.append_prefix import INSTRUCTION, definitions_in, tool_bindings
    from tests.support.context import _VALID_SUMMARY
    messages = [{"role": "user", "content": "old conversation " * 3000}]
    prepare_history(messages, [READ, WRITE], "obsolete instructions " * 2000)
    changed = {**READ, "description": "current schema"}
    prepare_history(messages, [changed], "current instructions")
    messages.extend([{"role": "user", "content": "continue"},
        {"type": "function_call", "call_id": "c1", "name": "read", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c1", "output": "read result"}])
    original = copy.deepcopy(messages)
    model = SimpleNamespace(create_message=Mock(return_value=ModelResponse(
        json.dumps(_VALID_SUMMARY), [], {"input_tokens": 100, "output_tokens": 10}, [])))
    result = ContextCompressor(model).compact(messages, keep_recent=3,
        current_system="current instructions", current_tools=[changed], target_message_tokens=5000)
    assert result.compacted
    instructions = [m for m in messages if m.get(INSTRUCTION)]
    assert len(instructions) == 1 and instructions[0]["content"] == "current instructions"
    definitions = definitions_in(messages)
    current_alias = next(iter(tool_bindings(original, [changed])))
    assert set(definitions) == {"read", current_alias}  # Old read is required by the retained call.
    assert "write" not in definitions
    assert messages[-3:] == original[-3:]
    before = copy.deepcopy(messages)
    prepare_history(messages, [changed], "current instructions")
    assert messages == before
    sent = model.create_message.call_args.args[0]
    assert "obsolete instructions" not in str(sent)


def test_compacted_alias_is_reused_without_retaining_obsolete_base():
    from app.models.append_prefix import rebuild_compacted_history, definitions_in, tool_bindings
    messages = []
    prepare_history(messages, [READ], "old")
    changed = {**READ, "description": "new"}
    prepare_history(messages, [changed], "new")
    alias = next(iter(tool_bindings(messages, [changed])))
    rebuilt = rebuild_compacted_history([], messages, system="new", tools=[changed])
    assert set(definitions_in(rebuilt)) == {alias}
    before = copy.deepcopy(rebuilt)
    prepare_history(rebuilt, [changed], "new")
    assert rebuilt == before
