"""Conservative native Responses request prefix snapshots (no SDK or live tool handles)."""
from __future__ import annotations

import copy
import json

from app.agent.context_budget import estimate_messages_tokens, estimate_tokens, resolve_context_limit
from app.util.redact import redact_value


def prepare_prefix(snapshot: dict | None, llm, instruction: str) -> tuple[list[dict] | None, str | None, dict]:
    diagnostics = {"requested_mode": "full_prefix", "mode": "brief"}

    def fallback(reason):
        return None, None, {**diagnostics, "fallback_reason": reason}

    if not snapshot or snapshot.get("provider") != "openai" or getattr(llm, "provider", None) != "openai":
        return fallback("unsupported_provider_or_missing_request")
    if snapshot.get("model") != getattr(llm, "model", None):
        return fallback("model_mismatch")
    messages = snapshot.get("messages", [])
    if not messages:
        return fallback("empty_history")
    if redact_value(messages) != messages or redact_value(snapshot.get("system")) != snapshot.get("system"):
        return fallback("redaction_would_change_prefix")
    # Only text messages and complete native Responses function pairs in the first version.
    calls, outputs = set(), set()
    for item in messages:
        kind = item.get("type")
        if kind == "function_call":
            calls.add(item.get("call_id"))
        elif kind == "function_call_output":
            outputs.add(item.get("call_id"))
        elif kind in (None, "message") and item.get("role") in {"user", "assistant", "system", "developer"}:
            content = item.get("content", "")
            if not isinstance(content, str) and not (
                isinstance(content, list) and all(part.get("type") in {"input_text", "output_text", "text"} for part in content)
            ):
                return fallback("non_replayable_content")
        else:
            return fallback("non_replayable_content")
    if calls != outputs or None in calls:
        return fallback("incomplete_tool_pairs")
    system = snapshot.get("system") or ""
    size = estimate_messages_tokens(messages) + estimate_tokens(system + instruction + json.dumps(snapshot.get("tools")))
    limit = resolve_context_limit(getattr(llm, "model", None), getattr(llm, "context_size", None))
    if size + max(4096, limit // 5) > limit:
        return fallback("context_capacity")
    return copy.deepcopy(messages), system, {
        **diagnostics, "mode": "full_prefix", "prefix_scope": "messages_and_system",
        "tools_match": False, "note": "child tools remain restricted; whole-request cache equivalence is not guaranteed",
        "input_chars": len(json.dumps(messages, ensure_ascii=False)), "truncated": False,
    }
