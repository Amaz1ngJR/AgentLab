"""Run-scoped delegation policy. Execution mode and tool approval are independent."""
from __future__ import annotations

import re

POLICIES = frozenset({"off", "explicit", "auto"})


def delegation_allowed(policy: str, goal: str, *, workflow: str = "") -> bool:
    if policy not in POLICIES:
        raise ValueError("delegation_policy must be off/explicit/auto")
    if policy == "off":
        return False
    # User veto takes precedence over auto and any applicable local workflow.
    if re.search(r"不要.*(?:agent|委派|代理)|不使用.*agent|\b(?:no|without)\s+(?:sub.?agents?|delegation)", goal, re.I):
        return False
    if policy == "auto":
        return True
    # workflow must come only from enabled, applicable local Skills, never retrieval.
    if workflow and delegation_allowed("explicit", workflow):
        return True
    return bool(re.search(
        r"(?:多[个]?|子|并行|多个)\s*agents?|委派|子代理|多代理|"
        r"\b(?:spawn_agent|subagents?|multi[ -]?agents?|delegate|delegation)\b|"
        r"\b(?:multiple|parallel)\s+agents?\b", goal, re.I,
    ))
