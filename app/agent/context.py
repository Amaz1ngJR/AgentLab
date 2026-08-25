"""ContextManager —— 把 ContextBudget 与 ContextCompressor 黏合到编排路径。

定位:
  Orchestrator / AgentSession 在"稳定点"(规划后、每个任务完成后)调一次
  maybe_compact(messages)。ContextManager 据当前预算判断:
    - ok      : 什么都不做。
    - warn    : 发 context_budget_warning(下一个稳定点准备压缩)。
    - compact : 调 ContextCompressor 压缩旧历史,发 context_compaction_* 事件,
                把审计记录(ContextSummary)攒起来等持久化。

为什么压缩放在"稳定点"而不是"每次模型调用前":
  §7.3.5 要求不破坏未闭合的 tool_use/tool_result 对、不动正在执行的任务。任务
  执行中(Executor 的工具循环里)消息处于半成品状态;任务之间才是干净的边界。
  在稳定点压缩,天然满足这些约束(切点只会落在已闭合的历史里)。

无 ContextManager 时(默认 / 既有测试):Orchestrator 不调本类,行为完全不变。
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from app.agent import events
from app.agent.context_budget import ContextBudget, estimate_messages_tokens
from app.agent.context_compaction import ContextCompressor, ContextSummary
from app.agent.events import RunEvent

# 压缩时至少保留的最近消息条数:保护最后一条用户请求 + 当前任务的近距上下文。
DEFAULT_KEEP_RECENT = 6


class ContextManager:
    """单个 session 的上下文预算与压缩协调者。"""

    def __init__(
        self,
        budget: ContextBudget,
        compressor: ContextCompressor,
        *,
        keep_recent: int = DEFAULT_KEEP_RECENT,
        auto_compact: bool = True,
        on_event: Optional[Callable[[RunEvent], None]] = None,
    ):
        self.budget = budget
        self._compressor = compressor
        self.keep_recent = keep_recent
        self.auto_compact = auto_compact
        self._emit = on_event or (lambda e: None)
        # 攒下本会话产生的压缩摘要,等 SessionRouter.persist_current flush 到 storage。
        self._pending_records: list[ContextSummary] = []
        # 最近一次有效摘要(供 /context summary 查看)。
        self.last_summary: Optional[ContextSummary] = None
        self._warned = False  # 避免同一段历史反复发 warning

    # ── 查询 ──────────────────────────────────────────────────────────────────

    def estimate(
        self,
        messages: list[dict[str, Any]],
        *,
        system: str = "",
        tools: Optional[list[dict[str, Any]]] = None,
    ) -> int:
        """估算模型输入：system + 历史消息 + Tool Schema。"""
        import json
        from app.agent.context_budget import estimate_tokens
        tool_text = json.dumps(tools, ensure_ascii=False) if tools else ""
        return (
            estimate_tokens(system)
            + estimate_messages_tokens(messages)
            + estimate_tokens(tool_text)
        )

    def report(
        self,
        messages: list[dict[str, Any]],
        *,
        system: str = "",
        tools: Optional[list[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        """给 /context 看的预算 + recent window + summary 状态快照。"""
        from app.agent.context_budget import estimate_tokens
        import json
        message_tokens = estimate_messages_tokens(messages)
        system_tokens = estimate_tokens(system)
        tool_tokens = estimate_tokens(json.dumps(tools, ensure_ascii=False)) if tools else 0
        est = system_tokens + message_tokens + tool_tokens
        b = self.budget
        return {
            "model_context_limit": b.model_context_limit,
            "reserved_output_tokens": b.reserved_output_tokens,
            "estimated_input_tokens": est,
            "message_tokens": message_tokens,
            "system_tokens": system_tokens,
            "tool_schema_tokens": tool_tokens,
            "warn_threshold": b.warn_threshold,
            "compact_threshold": b.compact_threshold,
            "status": b.status_for(est),
            "usage_ratio": (est / b.model_context_limit) if b.model_context_limit else 0.0,
            "messages": len(messages),
            "keep_recent": self.keep_recent,
            "auto_compact": self.auto_compact,
            "summaries": len(self._pending_records),
            "has_summary": self.last_summary is not None,
        }

    # ── 压缩 ──────────────────────────────────────────────────────────────────

    def maybe_compact(
        self,
        messages: list[dict[str, Any]],
        *,
        system: str = "",
        source_run_ids: Optional[list[str]] = None,
        force: bool = False,
        tools: Optional[list[dict[str, Any]]] = None,
        on_progress=None,
    ) -> bool:
        """在稳定点检查预算并按需压缩。返回是否发生了压缩。

        force=True 时无视阈值与 auto_compact 直接尝试压缩(供 /context compact)。
        """
        est = self.estimate(messages, system=system, tools=tools)
        status = self.budget.status_for(est)

        if not force:
            if not self.auto_compact:
                return False
            if status == "warn" and not self._warned:
                self._warned = True
                self._emit(RunEvent(
                    kind=events.CONTEXT_BUDGET_WARNING,
                    text="上下文接近窗口上限,下个稳定点将压缩旧历史",
                    payload=self.report(messages, system=system, tools=tools),
                ))
                return False
            if status != "compact":
                return False

        # 触发压缩
        self._emit(RunEvent(
            kind=events.CONTEXT_COMPACTION_STARTED,
            payload={"token_before": est},
        ))
        # 手动 compact 的语义是“尽可能压缩旧历史”。不能继续传 recent_budget，
        # 否则当整段历史本身低于 recent_messages_budget 时，选段算法会把全部消息
        # 都判为 recent tail，split=0，最终错误报告“无可安全压缩的前缀”。
        effective_recent_budget = None if force else self.budget.recent_messages_budget
        result = self._compressor.compact(
            messages, keep_recent=self.keep_recent,
            recent_budget=effective_recent_budget,
            source_run_ids=source_run_ids, on_progress=on_progress,
        )
        if not result.compacted or result.summary is None:
            self._emit(RunEvent(
                kind=events.CONTEXT_COMPACTION_FAILED,
                text=result.reason or "压缩未发生",
            ))
            return False

        if result.reason.startswith("local_fallback:"):
            self._emit(RunEvent(
                kind=events.CONTEXT_COMPACTION_FAILED,
                text=f"摘要模型不可用，已使用本地兜底压缩 ({result.reason.split(':', 1)[1]})",
            ))

        self._pending_records.append(result.summary)
        self.last_summary = result.summary
        self._warned = False  # 压缩后重置,下一轮增长可再次预警
        after = self.estimate(messages, system=system, tools=tools)
        self._emit(RunEvent(
            kind=events.CONTEXT_COMPACTION_COMPLETED,
            text="已压缩旧历史",
            payload={
                "token_before": est,
                "token_after": after,
                "source_message_range": list(result.summary.source_message_range),
                "summary": result.summary.summary,
            },
        ))
        return True

    # ── 审计记录 flush ─────────────────────────────────────────────────────────

    def drain_records(self) -> list[ContextSummary]:
        """取走并清空待持久化的压缩摘要(由 SessionRouter.persist_current 调用)。"""
        out = self._pending_records
        self._pending_records = []
        return out
