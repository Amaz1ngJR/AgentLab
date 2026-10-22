"""Preserve optional cache accounting; absent is unknown, explicit zero is known."""
from __future__ import annotations


def cache_usage(usage, *, provider: str) -> dict[str, int]:
    def field(obj, key):
        return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)

    if usage is None:
        return {}
    if provider == "anthropic":
        values = {key: field(usage, key) for key in ("cache_read_input_tokens", "cache_creation_input_tokens")}
    else:
        details = field(usage, "input_tokens_details" if provider == "openai" else "prompt_tokens_details")
        values = {"cache_read_input_tokens": field(details, "cached_tokens")}
    return {key: value for key, value in values.items() if isinstance(value, int) and not isinstance(value, bool) and value >= 0}
