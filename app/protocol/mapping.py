"""旧 TurnEvent/RunEvent 到规范 TurnItem 的映射。"""
from __future__ import annotations

import uuid
from dataclasses import asdict, is_dataclass
from typing import Any

from app.protocol.items import TurnItem
from app.util.redact import redact_value

_TURN_KIND = {
    "text": ("agent.message", "completed"),
    "tool_call": ("tool.call", "started"),
    "tool_result": ("tool.result", "completed"),
    "tool_denied": ("tool.result", "failed"),
}

_RUN_KIND = {
    "mode_selected": ("turn.mode", "completed"),
    "plan_created": ("plan", "completed"),
    "task_started": ("task.execution", "started"),
    "message_delta": ("agent.message", "started"),
    "tool_requested": ("tool.call", "started"),
    "approval_required": ("approval.request", "waiting"),
    "tool_completed": ("tool.result", "completed"),
    "tool_denied": ("tool.result", "failed"),
    "task_updated": ("task.execution", "completed"),
    "run_completed": ("turn.result", "completed"),
    "run_failed": ("turn.result", "failed"),
    "context_budget_warning": ("context.budget", "waiting"),
    "context_compaction_started": ("context.compaction", "started"),
    "context_compaction_completed": ("context.compaction", "completed"),
    "context_compaction_failed": ("context.compaction", "failed"),
    "goal_defined": ("loop.goal", "completed"),
    "loop_started": ("loop.lifecycle", "started"),
    "loop_iteration_started": ("loop.iteration", "started"),
    "verification_started": ("verification", "started"),
    "verification_completed": ("verification", "completed"),
    "repair_planned": ("loop.repair", "started"),
    "learner_candidate_created": ("loop.learning", "completed"),
    "loop_completed": ("loop.lifecycle", "completed"),
    "loop_failed": ("loop.lifecycle", "failed"),
    "loop_blocked": ("loop.lifecycle", "waiting"),
    "loop_budget_exhausted": ("loop.lifecycle", "failed"),
    "worktree_prepared": ("workspace", "completed"),
    "subagent_started": ("subagent", "started"),
    "subagent_completed": ("subagent", "completed"),
}


def runtime_event_to_item(
    event: Any,
    *,
    thread_id: str,
    turn_id: str,
    sequence: int,
) -> TurnItem | None:
    kind = getattr(event, "kind", "")
    mapping = _TURN_KIND.get(kind) or _RUN_KIND.get(kind)
    if mapping is None:
        return None
    item_kind, status = mapping
    payload = _event_payload(event)
    return TurnItem.create(
        item_id=f"item-{uuid.uuid4().hex}",
        thread_id=thread_id,
        turn_id=turn_id,
        sequence=sequence,
        kind=item_kind,
        status=status,  # type: ignore[arg-type]
        payload=payload,
    )


def item_event_kind(item: TurnItem) -> str:
    if item.status == "started":
        return "item.started"
    if item.status == "waiting":
        return "item.waiting"
    if item.status == "failed":
        return "item.failed"
    if item.status == "cancelled":
        return "item.cancelled"
    return "item.completed"


def _event_payload(event: Any) -> dict[str, Any]:
    if is_dataclass(event):
        data = asdict(event)
    elif hasattr(event, "__dict__"):
        data = dict(event.__dict__)
    elif isinstance(event, dict):
        data = dict(event)
    else:
        return {"value": str(event)}
    # kind 已由 Item 表达；去除空默认值以减少事件体。
    data.pop("kind", None)
    return {
        key: redact_value(value) for key, value in data.items()
        if value not in (None, "", {}, [], False, 0, 0.0)
    }
