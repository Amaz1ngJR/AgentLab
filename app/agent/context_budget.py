"""ContextBudget —— 按模型上下文窗口计算各部分的 token 预算与压缩阈值。

职责(technical_architecture.md §7.3.2):
  Runtime 在每次模型调用前需要知道"还能塞多少 token"。这里:
    1. 解析当前模型 profile 的上下文窗口(model_context_limit);
    2. 扣掉为输出预留的空间(reserved_output)与 system+tools 固定预算;
    3. 把剩余预算切给 memory / summary / recent / evidence 各项;
    4. 在扣除输出预留后的输入预算上计算警告和压缩阈值。

为什么自己估 token 而不调 tokenizer:
  AgentLab 要保持"全离线可测、零额外依赖"。各 provider 的 tokenizer 不一,
  也不能为了估算去打网络。这里用一个对中英文都偏保守(略高估)的字符启发式,
  宁可早一点触发压缩,也不要因低估而撞真实窗口上限。真实用量以 API 回传的
  usage 为准(见 ModelResponse.usage),估算只用于"该不该压缩"的本地决策。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

from app.models.context_window import DEFAULT_CONTEXT_LIMIT, ContextWindow, positive_int

# 阈值基于扣除输出预留后的输入预算，另留 2% 给估算误差与协议开销。
INPUT_SAFETY_RATIO = 0.98
WARN_RATIO = 0.80
COMPACT_RATIO = 0.92


def resolve_context_limit(
    model: str | None = None,
    declared_context_size: Optional[int] = None,
) -> int:
    """纯离线兼容入口；模型名称不用于猜测窗口。运行时使用 from_llm。"""
    return positive_int(declared_context_size) or DEFAULT_CONTEXT_LIMIT


# ── token 估算 ────────────────────────────────────────────────────────────────

def estimate_tokens(text: str) -> int:
    """估算一段文本的 token 数(偏保守 / 略高估)。

    启发式:CJK 字符按约 1.0 token/字(中文一个字常占 1~2 token,这里取下界
    再用 ASCII 部分的高估来补),其余字符按约 0.30 token/char(英文经验值
    ~0.25,这里上调到 0.30 多留余量)。空串返回 0。

    高估是有意的:估算只用于"要不要压缩"的本地决策,早压缩比晚压缩(撞窗口)安全。
    """
    if not text:
        return 0
    cjk = 0
    other = 0
    for ch in text:
        # 常见 CJK 区段:中日韩统一表意文字 + 扩展A + 假名 + 全角标点
        o = ord(ch)
        if (0x3000 <= o <= 0x9FFF) or (0xF900 <= o <= 0xFAFF) or (0xFF00 <= o <= 0xFFEF):
            cjk += 1
        else:
            other += 1
    return int(cjk * 1.0 + other * 0.30) + (1 if (cjk or other) else 0)


def estimate_message_tokens(message: dict[str, Any]) -> int:
    """估算完整消息对象的 token 数，覆盖 Responses 顶层 input item。

    Responses API 的 function_call/function_call_output 把 arguments/output 放在
    顶层，不能只统计 ``content``，否则长工具历史会被严重低估。
    """
    try:
        body = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        body = str(message)
    return estimate_tokens(body) + 4


def estimate_messages_tokens(messages: list[dict[str, Any]]) -> int:
    return sum(estimate_message_tokens(m) for m in messages)


@dataclass
class ContextBudget:
    """当前模型窗口下的各部分 token 预算。

    由 from_model 工厂按窗口大小派生。各 *_budget 是"建议上限",上层(Builder /
    Compressor)据此裁剪;真正的硬约束只有 model_context_limit 这一根线。
    """
    model_context_limit: int
    reserved_output_tokens: int
    system_and_tools_budget: int
    memory_budget: int
    summary_budget: int
    recent_messages_budget: int
    evidence_budget: int
    context_limit_source: str = "default"

    @property
    def input_budget(self) -> int:
        """留给"输入"(system+历史+摘要+证据)的总预算 = 窗口 - 预留输出。"""
        return max(0, self.model_context_limit - self.reserved_output_tokens)

    @property
    def warn_threshold(self) -> int:
        """预计输入超过这个值就该发 context_budget_warning。"""
        return int(self.input_budget * WARN_RATIO)

    @property
    def compact_threshold(self) -> int:
        """预计输入超过这个值就必须在下次模型调用前压缩旧历史。"""
        return int(self.input_budget * COMPACT_RATIO)

    @property
    def safe_input_limit(self) -> int:
        """请求前的硬上限，含估算安全余量。"""
        return int(self.input_budget * INPUT_SAFETY_RATIO)

    def status_for(self, estimated_input_tokens: int) -> str:
        """根据预计输入 token 给出预算状态:ok / warn / compact。"""
        if estimated_input_tokens >= self.compact_threshold:
            return "compact"
        if estimated_input_tokens >= self.warn_threshold:
            return "warn"
        return "ok"

    @classmethod
    def from_llm(cls, llm) -> "ContextBudget":
        """使用路由器已解析的能力；旧测试替身仍可只提供 context_size。"""
        window = getattr(llm, "context_window", None)
        if isinstance(window, ContextWindow):
            budget = cls.from_model(declared_context_size=window.tokens)
            budget.context_limit_source = window.source
            return budget
        return cls.from_model(declared_context_size=getattr(llm, "context_size", None))

    @classmethod
    def from_model(
        cls,
        model: str | None = None,
        declared_context_size: Optional[int] = None,
        *,
        reserved_output_tokens: Optional[int] = None,
    ) -> "ContextBudget":
        """按模型窗口派生一份预算。各项按窗口大小成比例切分,小窗口也保证非负。

        reserved_output_tokens 默认取窗口的 25%(上限 8k):为最终回答、工具参数
        和重规划留输出空间。其余输入预算按经验比例分给 system+tools / memory /
        summary / recent / evidence。
        """
        limit = resolve_context_limit(model, declared_context_size)
        reserved = (
            reserved_output_tokens
            if reserved_output_tokens is not None
            else min(8_192, max(512, int(limit * 0.25)))
        )
        reserved = min(limit, max(0, reserved))
        usable = max(0, limit - reserved)
        # 输入预算的经验切分(总和 = usable):
        #   system+tools 15% / memory 10% / summary 15% / recent 50% / evidence 10%
        return cls(
            model_context_limit=limit,
            context_limit_source="config" if positive_int(declared_context_size) else "default",
            reserved_output_tokens=reserved,
            system_and_tools_budget=int(usable * 0.15),
            memory_budget=int(usable * 0.10),
            summary_budget=int(usable * 0.15),
            recent_messages_budget=int(usable * 0.50),
            evidence_budget=int(usable * 0.10),
        )
