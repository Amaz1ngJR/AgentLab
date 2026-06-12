"""ContextBudget —— 按模型上下文窗口计算各部分的 token 预算与压缩阈值。

职责(technical_architecture.md §7.3.2):
  Runtime 在每次模型调用前需要知道"还能塞多少 token"。这里:
    1. 解析当前模型 profile 的上下文窗口(model_context_limit);
    2. 扣掉为输出预留的空间(reserved_output)与 system+tools 固定预算;
    3. 把剩余预算切给 memory / summary / recent / evidence 各项;
    4. 给出 70% 警告 / 85% 强制压缩两个阈值的判定。

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

# ── 模型上下文窗口表 ──────────────────────────────────────────────────────────
# 云端模型的窗口不写在 config/models.yaml 里(那里只有本地模型的 context_size),
# 所以按模型名前缀给一张保守的查找表。匹配不到时退回 DEFAULT_CONTEXT_LIMIT。
# 数值取"安全可用"而非厂商标称上限,给 provider 端的隐藏 token 留余量。
_MODEL_CONTEXT_LIMITS: dict[str, int] = {
    "claude-opus-4": 200_000,
    "claude-sonnet-4": 200_000,
    "claude-haiku-4": 200_000,
    "claude-3-5": 200_000,
    "claude-3": 200_000,
    "claude": 200_000,
    "gpt-5": 256_000,
    "gpt-4o": 128_000,
    "gpt-4-turbo": 128_000,
    "gpt-4": 128_000,
    "gpt-3.5": 16_000,
    "qwen2.5": 32_000,
    "qwen": 32_000,
    "deepseek": 64_000,
}

# 匹配不到任何已知模型、且 profile 未声明 context_size 时的保守默认窗口。
DEFAULT_CONTEXT_LIMIT = 8_192

# 阈值:预计输入超过窗口的这个比例就触发对应动作(§7.3.2)。
WARN_RATIO = 0.70
COMPACT_RATIO = 0.85


def resolve_context_limit(
    model: str | None = None,
    declared_context_size: Optional[int] = None,
) -> int:
    """决定当前模型可用的上下文窗口(token 数)。

    优先级:
      1. profile 显式声明的 context_size(本地模型在 models.yaml 里有)——最可信,
         因为本地模型的窗口是用户在 Ollama/部署时定死的。
      2. 按模型名前缀查 _MODEL_CONTEXT_LIMITS(云端模型用)。
      3. 都没有 → DEFAULT_CONTEXT_LIMIT(偏小,促使更早压缩,安全)。
    """
    if declared_context_size and declared_context_size > 0:
        return int(declared_context_size)
    name = (model or "").lower().replace(".", "-").replace("_", "-")
    # 前缀匹配:取最长匹配前缀,避免 "claude" 抢在 "claude-opus-4" 前面命中
    best: tuple[int, int] | None = None  # (前缀长度, limit)
    for prefix, limit in _MODEL_CONTEXT_LIMITS.items():
        norm_prefix = prefix.replace(".", "-").replace("_", "-")
        if name.startswith(norm_prefix):
            if best is None or len(norm_prefix) > best[0]:
                best = (len(norm_prefix), limit)
    return best[1] if best else DEFAULT_CONTEXT_LIMIT


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
    """估算一条对话消息的 token 数。

    消息的 content 可能是字符串,也可能是 provider 原生的 block 列表
    (Anthropic 的 tool_use / tool_result 是 dict)。统一序列化成文本再估,
    并为角色标记 / 结构开销加一个小的固定常数。
    """
    content = message.get("content", "")
    if isinstance(content, str):
        body = content
    else:
        try:
            body = json.dumps(content, ensure_ascii=False)
        except (TypeError, ValueError):
            body = str(content)
    return estimate_tokens(body) + 4  # 4: role / 分隔符等结构开销的粗略常数


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

    @property
    def input_budget(self) -> int:
        """留给"输入"(system+历史+摘要+证据)的总预算 = 窗口 - 预留输出。"""
        return max(0, self.model_context_limit - self.reserved_output_tokens)

    @property
    def warn_threshold(self) -> int:
        """预计输入超过这个值就该发 context_budget_warning。"""
        return int(self.model_context_limit * WARN_RATIO)

    @property
    def compact_threshold(self) -> int:
        """预计输入超过这个值就必须在下次模型调用前压缩旧历史。"""
        return int(self.model_context_limit * COMPACT_RATIO)

    def status_for(self, estimated_input_tokens: int) -> str:
        """根据预计输入 token 给出预算状态:ok / warn / compact。"""
        if estimated_input_tokens >= self.compact_threshold:
            return "compact"
        if estimated_input_tokens >= self.warn_threshold:
            return "warn"
        return "ok"

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
        usable = max(0, limit - reserved)
        # 输入预算的经验切分(总和 = usable):
        #   system+tools 15% / memory 10% / summary 15% / recent 50% / evidence 10%
        return cls(
            model_context_limit=limit,
            reserved_output_tokens=reserved,
            system_and_tools_budget=int(usable * 0.15),
            memory_budget=int(usable * 0.10),
            summary_budget=int(usable * 0.15),
            recent_messages_budget=int(usable * 0.50),
            evidence_budget=int(usable * 0.10),
        )
