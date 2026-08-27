"""本地 Direct/Task/Loop 模式选择器。

选择器只依赖用户输入、附件和会话状态，不调用模型；它负责把明显简单的
请求留在低延迟 Direct 路径，把需要规划或验收的请求交给对应策略。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping


class ExecutionMode(StrEnum):
    AUTO = "auto"
    DIRECT = "direct"
    TASK = "task"
    LOOP = "loop"


@dataclass(frozen=True)
class SessionState:
    """模式选择所需的最小会话快照，避免读取 Session 私有字段。"""

    has_active_goal: bool = False
    has_open_tasks: bool = False
    orchestrate_enabled: bool = True


# 明确要求持续验证/成功标准的语句进入 Loop。
_LOOP_MARKERS = (
    "/goal",
    "/loop",
    "goalspec",
    "success criteria",
    "成功标准",
    "验收标准",
    "反复修复",
    "持续验证",
)

# 通常需要拆解多个任务的动作词。只匹配中文/英文语义，不把普通提问误判为 Task。
_TASK_MARKERS = (
    "多个文件",
    "多文件",
    "多个步骤",
    "分步骤",
    "先…再",
    "先...再",
    "实现并测试",
    "修改并测试",
    "修复并验证",
    "refactor",
    "implement and test",
    "fix and verify",
    "多个模块",
    "multi-file",
)

# 中文动作词按子串匹配；英文动作词按整词匹配，避免 "read" 命中 "already"、
# "test" 命中 "latest" 这类误判。
_ACTION_MARKERS_ZH = (
    "整理",
    "修改",
    "编辑",
    "写入",
    "创建",
    "删除",
    "修复",
    "实现",
    "运行",
    "执行",
    "测试",
    "读取",
    "查看",
    "搜索",
)

_ACTION_WORDS_EN = frozenset({
    "edit",
    "edits",
    "write",
    "writes",
    "create",
    "creates",
    "delete",
    "deletes",
    "fix",
    "fixes",
    "implement",
    "implements",
    "run",
    "runs",
    "test",
    "tests",
    "read",
    "reads",
    "search",
    "searches",
})


def select_mode(
    user_input: str,
    attachments: Any = None,
    session_state: SessionState | Mapping[str, Any] | None = None,
) -> ExecutionMode:
    """根据本地规则选择执行模式。

    规则优先级：Loop（显式验收） > Task（明显多步骤） > Direct。
    已有未完成任务只会在用户明确要求继续时进入 Task，避免普通聊天意外恢复旧任务。
    当 profile 禁用编排时始终返回 Direct，保持 legacy 行为。
    """
    text = (user_input or "").strip().lower()
    state = _coerce_state(session_state)
    if not state.orchestrate_enabled:
        return ExecutionMode.DIRECT
    if any(marker.lower() in text for marker in _LOOP_MARKERS):
        return ExecutionMode.LOOP
    # 已有活跃 GoalSpec 且本轮带明确操作时继续留在 Loop，避免验收中途掉回 Direct。
    if state.has_active_goal and _has_action(text):
        return ExecutionMode.LOOP
    if _has_resume_intent(text) and state.has_open_tasks:
        return ExecutionMode.TASK
    if any(marker.lower() in text for marker in _TASK_MARKERS):
        return ExecutionMode.TASK
    # 附件本身不强制 Planner；单张图片问答仍是 Direct，避免无谓规划。
    # 多附件 + 明确操作通常需要任务拆解。
    if _attachment_count(attachments) > 1 and _has_action(text):
        return ExecutionMode.TASK
    return ExecutionMode.DIRECT


def _coerce_state(value: SessionState | Mapping[str, Any] | None) -> SessionState:
    if value is None:
        return SessionState()
    if isinstance(value, SessionState):
        return value
    return SessionState(
        has_active_goal=bool(value.get("has_active_goal", value.get("active_goal", False))),
        has_open_tasks=bool(value.get("has_open_tasks", value.get("open_tasks", False))),
        orchestrate_enabled=bool(value.get("orchestrate_enabled", value.get("orchestrate", True))),
    )


def _words(text: str) -> set[str]:
    """按非字母数字切词，用于英文整词匹配。"""
    return set(re.findall(r"[a-z0-9]+", text))


def _has_resume_intent(text: str) -> bool:
    if any(marker in text for marker in ("/resume", "继续上一轮", "继续未完成")):
        return True
    return "resume" in _words(text)


def _has_action(text: str) -> bool:
    if any(marker in text for marker in _ACTION_MARKERS_ZH):
        return True
    return bool(_ACTION_WORDS_EN & _words(text))


def _attachment_count(attachments: Any) -> int:
    if attachments is None:
        return 0
    if isinstance(attachments, (str, bytes, bytearray)):
        return 1
    try:
        return len(attachments)
    except TypeError:
        return 1


class ModeRouter:
    """无模型调用的本地模式路由入口。"""

    @staticmethod
    def select(
        user_input: str,
        attachments: Any = None,
        session_state: SessionState | Mapping[str, Any] | None = None,
    ) -> ExecutionMode:
        return select_mode(user_input, attachments, session_state)


__all__ = ["ExecutionMode", "ModeRouter", "SessionState", "select_mode"]
