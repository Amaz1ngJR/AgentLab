"""ContextManager —— 把 ContextBudget 与 ContextCompressor 黏合到编排路径。

在模型调用前和任务稳定点检查预算。压缩仅替换通过完整输入预算校验的候选历史；
工具调用与结果按关联 ID 保护。无法容纳受保护的用户请求或尾部时，在发送前明确失败。

无 ContextManager 时(默认 / 既有测试):Orchestrator 不调本类,行为完全不变。
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from app.agent import events
from app.agent.cancel import Cancelled, CancelToken
from app.agent.context_budget import ContextBudget, estimate_messages_tokens
from app.agent.context_compaction import ContextCompressor, ContextSummary
from app.agent.events import RunEvent

# 压缩时至少保留的最近消息条数:保护最后一条用户请求 + 当前任务的近距上下文。
DEFAULT_KEEP_RECENT = 6


class ContextBudgetExceeded(RuntimeError):
    """受保护的上下文无法装入模型，阻止重复发送必然超限的请求。"""


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
            "safe_input_limit": b.safe_input_limit,
            "input_budget": b.input_budget,
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

    def compact_before_model_call(
        self,
        messages: list[dict[str, Any]],
        *,
        system: str = "",
        tools: Optional[list[dict[str, Any]]] = None,
        on_progress=None,
        cancel: CancelToken | None = None,
    ) -> bool:
        """在任何模型请求前压缩超限历史，返回是否发生压缩。"""
        if cancel is not None:
            cancel.raise_if_cancelled()
        estimate = self.estimate(messages, system=system, tools=tools)
        if self.budget.status_for(estimate) != "compact":
            return False
        try:
            compacted = self.maybe_compact(
                messages, system=system, tools=tools,
                on_progress=on_progress, cancel=cancel,
            )
        except Cancelled:
            raise
        except Exception:
            # 即使摘要过程异常，也必须检查即将发送的原始请求。
            self.ensure_fits(messages, system=system, tools=tools)
            raise
        self.ensure_fits(messages, system=system, tools=tools)
        return compacted

    def ensure_fits(
        self, messages: list[dict[str, Any]], *, system: str = "",
        tools: Optional[list[dict[str, Any]]] = None,
    ) -> None:
        estimated = self.estimate(messages, system=system, tools=tools)
        if estimated > self.budget.safe_input_limit:
            raise ContextBudgetExceeded(
                f"上下文无法安全压缩：预计输入 {estimated} token，安全预算 "
                f"{self.budget.safe_input_limit} token。请缩短请求、工具定义或切换更大窗口模型。"
            )

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
        cancel: CancelToken | None = None,
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
        fixed_tokens = est - estimate_messages_tokens(messages)
        target = max(0, self.budget.safe_input_limit - fixed_tokens)
        summary_budget = max(256, min(4096, self.budget.summary_budget))
        summary_input = self.budget.safe_input_limit - self._compressor.summary_system_tokens - summary_budget - 64
        if summary_input < 256:
            self._emit(RunEvent(kind=events.CONTEXT_COMPACTION_FAILED,
                                text="模型窗口不足以容纳摘要指令和输出预留"))
            return False
        # 先尝试常规尾部预算；若仍超限，缩减到条数下限，始终对候选执行原子校验。
        for recent_budget in ([None] if force else [effective_recent_budget, None]):
            result = self._compressor.compact(
                messages, keep_recent=self.keep_recent,
                recent_budget=recent_budget,
                summary_budget=summary_budget, input_token_budget=summary_input,
                target_message_tokens=target,
                source_run_ids=source_run_ids, on_progress=on_progress, cancel=cancel,
            )
            if result.compacted or result.code != "budget_exceeded":
                break
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
