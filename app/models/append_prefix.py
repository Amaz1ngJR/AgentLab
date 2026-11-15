"""Persist append-only OAuth instructions/tools in the owning conversation."""
import copy
import hashlib
import json
import re

from app.models.openai_adapter import ProviderStreamError, _to_responses_tool

INSTRUCTION = "_agentlab_instruction"
UPDATE_PREFIX = "以下是当前完整的 AgentLab 指令，替代此前的 AgentLab 指令及临时 Skill 工作流：\n\n"


def definitions_in(messages):
    return {tool["name"]: tool for item in messages if item.get("type") == "additional_tools"
            for tool in item.get("tools", [])}


def wire_definition(tool, definitions):
    definition = _to_responses_tool(tool)
    original = definition["name"]
    if definitions.get(original) == definition:
        return definition
    digest = hashlib.sha256(json.dumps(definition, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
    alias = original[:44] + "__v_" + digest
    if original in definitions or alias in definitions:
        definition["name"] = alias
        if alias in definitions and definitions[alias] != definition:
            raise ProviderStreamError("工具版本名冲突，无法安全映射工具调用")
    return definition


def tool_bindings(messages, tools):
    definitions = definitions_in(messages)
    return {wire_definition(tool, definitions)["name"]: tool["name"] for tool in tools or []}


def rebuild_compacted_history(candidate, original, *, system=None, tools=None):
    """Compaction is an explicit prefix boundary: retain only effective state.

    Tail calls may still reference an old version, so retain those definitions
    until their call/output pairs also leave the recent window.
    """
    managed = [m for m in original if m.get(INSTRUCTION)]
    if not managed:
        return candidate
    definitions = definitions_in(original)
    if tools is None:
        latest = {}
        for name in definitions:
            latest[re.sub(r"__v_[0-9a-f]{16}$", "", name)] = name
        needed = set(latest.values())
    else:
        needed = set(tool_bindings(original, tools))
    needed.update(m["name"] for m in candidate if m.get("type") == "function_call" and m.get("name"))
    effective = system if system is not None else managed[-1]["content"].removeprefix(UPDATE_PREFIX)
    state = [{"type": "message", "role": "developer", INSTRUCTION: True,
              "content": effective or "按当前任务执行。"}]
    selected = [copy.deepcopy(value) for name, value in definitions.items() if name in needed]
    # Newly enabled tools may not yet have appeared in the old history.
    for tool in tools or []:
        value = wire_definition(tool, definitions)
        if value["name"] not in {t["name"] for t in selected}:
            selected.append(copy.deepcopy(value))
    if selected:
        state.append({"type": "additional_tools", "role": "developer", "tools": selected})
    rest = [m for m in candidate if not m.get(INSTRUCTION) and m.get("type") != "additional_tools"]
    return [*state, *rest]


def prepare_history(messages, tools, system):
    desired_system = system or "按当前任务执行。"
    definitions = definitions_in(messages)
    previous_system = None
    initialized = False
    for item in messages:
        if item.get(INSTRUCTION):
            previous_system = item["content"].removeprefix(UPDATE_PREFIX)
            initialized = True
    new = []
    for tool in tools or []:
        definition = wire_definition(tool, definitions)
        old = definitions.get(definition["name"])
        if old is None:
            new.append(definition)
            definitions[definition["name"]] = definition
    updates = []
    if not initialized or previous_system != desired_system:
        updates.append({"type": "message", "role": "developer", INSTRUCTION: True,
                        "content": (UPDATE_PREFIX if initialized else "") + desired_system})
    if new:
        updates.append({"type": "additional_tools", "role": "developer", "tools": copy.deepcopy(new)})
    messages.extend(updates)
