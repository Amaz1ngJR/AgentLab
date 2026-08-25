"""ContextCompressor —— 把单个 session 里的旧历史压成结构化摘要,控制模型输入长度。

职责(technical_architecture.md §7.3.3 / §7.3.4 / §7.3.5):
  在接近上下文窗口上限时,选取"可压缩的旧历史段",让模型产出一段结构化摘要
  (ContextSummary),用一条合成的摘要消息替换掉那段旧历史,从而缩短下一次
  模型调用的输入。原始消息不被删除 —— 压缩只改"模型输入里的旧片段",审计与
  恢复仍可依赖原始消息(由数据保留策略管理,这里不删)。

压缩边界与安全规则(§7.3.5):
  - 不切开 tool_use / tool_result 对(否则 provider 会因悬空的 tool_call 报错)。
  - 保留最后一条用户请求 + 最近窗口(recent tail)不被压缩。
  - 摘要进云端模型前与普通上下文走同样的脱敏(redact)。
  - 摘要不自动写长期记忆(memory_candidates 只是候选)。
  - 无法确认的事实标 unknown,不为缩短上下文而编造。
  - 摘要失败时退化:保留原始尾部,不破坏对话(由调用方决定是否换更大模型)。

离线可测:只依赖 llm.create_message(...).text,FakeRouter 返回 JSON 即可测全链路。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from app.agent.context_budget import estimate_messages_tokens, estimate_message_tokens
from app.util.redact import redact

# 给压缩模型的系统指令:只输出结构化 JSON 摘要,不调用工具、不写散文。
COMPRESSION_SYSTEM = """你是上下文压缩器。把下面这段较早的对话历史压成一份结构化摘要,
让后续的模型调用在不读原始历史的情况下也能继续推进任务。

只输出一个 JSON 对象,不要任何额外文字、不要用 markdown 代码块包裹。字段:
{
  "user_goal": "当前用户目标和成功标准",
  "active_constraints": ["用户明确的约束/禁止事项/偏好"],
  "decisions": ["已确认的设计决定及原因"],
  "current_state": "当前代码/网页/远程设备/任务的状态",
  "open_tasks": ["未完成任务、阻塞原因、下一步建议"],
  "tool_evidence": [{"source": "工具名或证据引用", "finding": "可复用的结论"}],
  "files_and_artifacts": ["涉及的文件/截图/下载/外部页面引用"],
  "failed_attempts": ["已尝试但失败的方案,避免重复"],
  "approvals_and_risks": ["已授权范围、被拒动作、高风险提示"],
  "memory_candidates": ["可能值得写入长期记忆的候选(不会自动落库)"],
  "handoff_note": "给后续模型调用的简短交接说明"
}

规则:
- 忠实压缩,不要编造。无法从历史中确认的事实写成 "unknown" 或留空数组,绝不臆测。
- 引用工具结果/文件时给出来源(工具名/路径),不要把大段原文重新抄进来。
- user_goal 与 handoff_note 必填且具体;其余字段没有内容就给空字符串或空数组。
"""

# 摘要消息注入历史时的外层模板:明确告诉后续模型"这是被压缩的早期历史摘要"。
_SUMMARY_MESSAGE_TEMPLATE = (
    "【上下文摘要 · 早期历史已压缩】\n"
    "以下是本会话较早部分的结构化摘要(原始消息已从模型输入中省略,但仍保留在审计记录中)。"
    "请把它当作已经发生的事实继续推进,不要重复已完成的工作。\n\n{body}"
)

# 摘要必填字段:缺这些视为无效摘要,触发兜底(不压缩)。
_REQUIRED_FIELDS = ("user_goal", "handoff_note")


@dataclass
class ContextSummary:
    """一次压缩产出的结构化摘要 + 审计元数据(§7.3.3)。

    summary 是结构化字典(见 COMPRESSION_SYSTEM 的字段)。其余是审计字段:
    压缩覆盖的消息下标区间、来源 run、压缩前后 token、压缩所用模型 profile。
    """
    summary: dict[str, Any]
    source_message_range: tuple[int, int]      # [start, end) 半开区间,原始 messages 下标
    source_run_ids: list[str] = field(default_factory=list)
    token_count_before: int = 0
    token_count_after: int = 0
    compression_model_profile: str = ""

    def to_message_text(self) -> str:
        """渲染成注入历史的摘要消息正文(人读 + 模型读都友好的紧凑文本)。"""
        s = self.summary
        lines: list[str] = []

        def _add(label: str, value: Any) -> None:
            if not value:
                return
            if isinstance(value, list):
                if all(isinstance(x, dict) for x in value):
                    items = "; ".join(
                        f"{x.get('source', '')}: {x.get('finding', '')}".strip(": ")
                        for x in value
                    )
                else:
                    items = "; ".join(str(x) for x in value)
                if items:
                    lines.append(f"- {label}: {items}")
            else:
                lines.append(f"- {label}: {value}")

        _add("用户目标", s.get("user_goal"))
        _add("约束", s.get("active_constraints"))
        _add("已定决策", s.get("decisions"))
        _add("当前状态", s.get("current_state"))
        _add("未完成任务", s.get("open_tasks"))
        _add("工具证据", s.get("tool_evidence"))
        _add("文件/产物", s.get("files_and_artifacts"))
        _add("失败尝试", s.get("failed_attempts"))
        _add("授权与风险", s.get("approvals_and_risks"))
        _add("交接说明", s.get("handoff_note"))
        return _SUMMARY_MESSAGE_TEMPLATE.format(body="\n".join(lines))

    def to_record(self) -> dict[str, Any]:
        """供 storage 持久化的纯数据记录(审计用)。"""
        return {
            "summary_json": json.dumps(self.summary, ensure_ascii=False),
            "source_message_range": list(self.source_message_range),
            "source_run_ids": list(self.source_run_ids),
            "token_count_before": self.token_count_before,
            "token_count_after": self.token_count_after,
            "compression_model_profile": self.compression_model_profile,
        }


def _is_tool_result_message(m: dict[str, Any]) -> bool:
    """判断一条消息是否承载 tool_result(它必须紧跟产生它的 tool_use,不能被拆散)。

    支持三种格式:
    - Anthropic 风格: role=user, content 是含 type=tool_result 的 block 列表
    - OpenAI Chat 风格: role=tool
    - OpenAI Responses API 风格: type=function_call_output (顶级字段)
    """
    # OpenAI Responses API: type=function_call_output
    if m.get("type") == "function_call_output":
        return True
    # OpenAI Chat Completions: role=tool
    if m.get("role") == "tool":
        return True
    # Anthropic: role=user + content 中有 type=tool_result
    content = m.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                return True
    return False


def _safe_split_point(
    messages: list[dict[str, Any]],
    keep_recent: int,
    recent_budget: Optional[int] = None,
) -> int:
    """求一个"干净"的压缩切点 split:把 messages[0:split) 压缩,messages[split:] 保留。

    约束:
      - 至少保留 keep_recent 条最近消息(下限,含最后一条用户请求所在的尾部窗口)。
      - 若给了 recent_budget(token 上限):在保住 keep_recent 条的前提下,继续往前
        把更多最近消息纳入"保留窗口",直到累计 token 超过 recent_budget 为止。
        这让"按 token 而非条数"决定保留多少 —— 一条超大消息会更早吃满预算,从而
        被排除出保留窗口、落进可压缩区,不再无脑霸占窗口。
      - 切点处的"第一条被保留消息"不能是 tool_result —— 否则它对应的 tool_use
        落在被压缩区,会变成悬空 tool_call,provider 报错。遇到就把切点往前挪。

    返回 0 表示没有可安全压缩的前缀(调用方应放弃本次压缩)。
    """
    n = len(messages)
    keep = max(0, keep_recent)
    if n - keep <= 0:
        return 0

    # 从尾部向前累计:前 keep 条无条件纳入保留窗口(下限);超过 keep 条后,只有在
    # 给了 recent_budget 且累计 token 仍不超预算时才继续纳入。无 budget 则停在 keep
    # 条(纯条数模式,向后兼容)。
    acc = 0
    count = 0
    i = n
    while i > 0:
        t = estimate_message_tokens(messages[i - 1])
        if count >= keep:
            # 已达下限:无预算就停(条数模式);有预算则在不超支时继续扩。
            if recent_budget is None or recent_budget <= 0:
                break
            if acc + t > recent_budget:
                break
        acc += t
        count += 1
        i -= 1
    split = i
    if split <= 0:
        return 0
    # 若切点处第一条保留消息是 tool_result,说明它的 tool_use 在压缩区 → 往前挪,
    # 直到保留侧以非 tool_result 开头(把整对都留在保留侧)。
    while split > 0 and _is_tool_result_message(messages[split]):
        split -= 1
    return max(0, split)


def _find_balanced_json(text: str, start: int) -> Optional[str]:
    """从 text[start] 处的 '{' 起,做括号配平扫描,返回完整的 JSON 对象子串。

    正确处理嵌套对象/数组,并跳过字符串字面量里的花括号(以及转义)。配不平
    (花括号没闭合)返回 None。这是比 `\\{.*?\\}` 正则更可靠的抠取方式 —— 后者
    非贪婪会停在第一个 '}',贪婪又可能多吞;配平扫描才能精确切出整个对象。
    """
    depth = 0
    in_str = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _bound_transcript(text: str, max_chars: int = 80_000) -> str:
    """限制摘要模型看到的历史文本，保留头尾并明确中间省略。"""
    if len(text) <= max_chars:
        return text
    head = int(max_chars * 0.60)
    tail = max_chars - head
    omitted = len(text) - max_chars
    return (
        text[:head]
        + f"\n\n[中间历史已省略 {omitted} 个字符，仅用于摘要输入；原始历史仍保留]\n\n"
        + text[-tail:]
    )


def _extract_json(text: str) -> Optional[dict]:
    """从模型输出抠出第一个 JSON 对象(容忍 markdown 围栏 / 前后散文 / 嵌套结构)。

    用括号配平扫描而非非贪婪正则:模型若把含嵌套对象(如 tool_evidence)的摘要包进
    ```json``` 围栏,`\\{.*?\\}` 会停在第一个内层 '}' 抠出残缺片段,导致必填字段丢失、
    摘要被误判为非法。配平扫描从第一个 '{' 切到与之匹配的 '}',不受嵌套影响。
    """
    if not text:
        return None
    # 优先在 markdown 围栏内找(围栏存在时,JSON 通常完整地落在里面)
    fenced = re.search(r"```(?:json)?\s*", text)
    search_from = fenced.end() if fenced else 0
    start = text.find("{", search_from)
    if start == -1 and search_from != 0:
        start = text.find("{")  # 围栏后没找到,退回全文找
    if start == -1:
        return None
    candidate = _find_balanced_json(text, start)
    if candidate is None:
        # 配平失败(JSON 被截断等):退回"首 { 到末 }"的粗略切法兜底
        end = text.rfind("}")
        if end > start:
            candidate = text[start : end + 1]
        else:
            return None
    try:
        obj = json.loads(candidate)
        return obj if isinstance(obj, dict) else None
    except (ValueError, TypeError):
        return None


def _redact_summary(obj: dict[str, Any]) -> dict[str, Any]:
    """对摘要里的字符串值递归脱敏(摘要进云端模型前必须脱敏,§7.3.5)。"""
    def _walk(v: Any) -> Any:
        if isinstance(v, str):
            return redact(v)
        if isinstance(v, list):
            return [_walk(x) for x in v]
        if isinstance(v, dict):
            return {k: _walk(x) for k, x in v.items()}
        return v
    return _walk(obj)


def _is_valid_summary(obj: Optional[dict]) -> bool:
    """必填字段(user_goal / handoff_note)非空才算有效摘要。"""
    if not isinstance(obj, dict):
        return False
    for f in _REQUIRED_FIELDS:
        if not str(obj.get(f) or "").strip():
            return False
    return True


class CompactionResult:
    """maybe 一次压缩的结果。compacted=False 表示本次未压缩(预算够 / 无可压段 / 失败)。"""

    def __init__(self, compacted: bool, summary: Optional[ContextSummary] = None,
                 reason: str = ""):
        self.compacted = compacted
        self.summary = summary
        self.reason = reason


def _fallback_summary(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """模型摘要失败时的本地安全兜底，不编造事实，只保留可追溯摘要。"""
    user_messages = [
        str(m.get("content", "")) for m in messages if m.get("role") == "user"
    ]
    assistant_messages = [
        str(m.get("content", "")) for m in messages if m.get("role") == "assistant"
    ]
    tool_count = sum(1 for m in messages if m.get("type") in {
        "function_call", "function_call_output",
    } or m.get("role") == "tool")
    last_user = user_messages[-1][-2000:] if user_messages else "unknown"
    last_assistant = assistant_messages[-1][-2000:] if assistant_messages else "unknown"
    return {
        "user_goal": last_user or "unknown",
        "active_constraints": [],
        "decisions": [],
        "current_state": f"本地兜底摘要：压缩前包含 {len(messages)} 条消息、约 {tool_count} 条工具记录。",
        "open_tasks": ["请基于最近消息和任务面板继续确认未完成工作。"],
        "tool_evidence": [{
            "source": "local_compaction_fallback",
            "finding": f"最近一条助手输出：{last_assistant}",
        }],
        "files_and_artifacts": [],
        "failed_attempts": [],
        "approvals_and_risks": [],
        "memory_candidates": [],
        "handoff_note": "模型摘要生成失败，以上为本地有界兜底；不要把未知内容当作已确认事实。",
    }


class ContextCompressor:
    """选段 → 调模型摘要 → 校验/脱敏 → 产出摘要消息并就地替换旧历史。"""

    def __init__(self, llm, system: str = COMPRESSION_SYSTEM,
                 model_profile: str = "", allow_local_fallback: bool = True):
        self._llm = llm
        self._system = system
        self._model_profile = model_profile
        self._allow_local_fallback = allow_local_fallback
        self.last_usage: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}

    def compact(
        self,
        messages: list[dict[str, Any]],
        *,
        keep_recent: int,
        recent_budget: Optional[int] = None,
        source_run_ids: Optional[list[str]] = None,
        on_progress=None,
    ) -> CompactionResult:
        """就地压缩 messages 的可压缩前缀。成功时 messages 被原地改短。

        keep_recent: 至少保留的最近消息条数(下限,保护最后用户请求与正在执行的任务)。
        recent_budget: 最近窗口的 token 上限(可选);给了就按 token 决定保留多少,
            超大单条消息会被挤出保留窗口、纳入可压缩区。
        返回 CompactionResult;失败/无可压段时 messages 不变、compacted=False。
        """
        self.last_usage = {"input_tokens": 0, "output_tokens": 0}
        split = _safe_split_point(messages, keep_recent, recent_budget)
        if split <= 0:
            return CompactionResult(False, reason="无可安全压缩的前缀")

        old = messages[:split]
        token_before = estimate_messages_tokens(old)

        # 长历史不能作为一条 user message 原样发给摘要模型；先限制摘要输入大小。
        # 旧消息仍保留在 SQLite，只有摘要请求的 transcript 做有界采样，避免压缩请求
        # 本身再次触发 context overflow。优先保留最早目标和最近证据。
        transcript = self._render_transcript(old)
        transcript = _bound_transcript(transcript)
        fallback_reason = ""
        try:
            resp = self._llm.create_message(
                messages=[{"role": "user", "content": transcript}],
                tools=None,
                system=self._system,
                max_tokens=1024,
                on_progress=on_progress,
            )
            if getattr(resp, "usage", None):
                for k in ("input_tokens", "output_tokens"):
                    self.last_usage[k] = resp.usage.get(k, 0)
            obj = _extract_json(getattr(resp, "text", "") or "")
        except Exception as exc:  # 模型异常:进入本地兜底,不让手动 compact 失效
            if not self._allow_local_fallback:
                return CompactionResult(False, reason=f"压缩模型调用失败: {exc}")
            # 保留诊断原因供 ContextManager/UI 展示，但继续构造本地摘要。
            obj = _fallback_summary(old)
            fallback_reason = f"local_fallback:{type(exc).__name__}"

        # 当前 CRS 模型可能把 JSON 摘要请求误判成普通 coding prompt，甚至返回工具调用。
        if not _is_valid_summary(obj):
            # 强制手动压缩不能因为 CRS 返回 Markdown/截断/非 JSON 就彻底失效；
            # 本地兜底只抽取历史中真实存在的最近用户/助手文本，不要求模型编造摘要。
            if self._allow_local_fallback:
                obj = _fallback_summary(old)
                fallback_reason = "local_fallback:invalid_summary"
            else:
                return CompactionResult(False, reason="摘要缺必填字段或非法 JSON")

        obj = _redact_summary(obj)
        summary = ContextSummary(
            summary=obj,
            source_message_range=(0, split),
            source_run_ids=list(source_run_ids or []),
            token_count_before=token_before,
            compression_model_profile=self._model_profile,
        )
        summary_msg = {"role": "user", "content": summary.to_message_text()}
        summary.token_count_after = estimate_messages_tokens([summary_msg])

        # 兜底:若摘要并不比被压缩的原前缀短(短前缀 + 结构化摘要骨架开销可能反而
        # 更长),压缩没有收益,不替换 —— 否则白白消耗一次模型调用还可能让上下文变大。
        if summary.token_count_after >= token_before:
            return CompactionResult(
                False,
                reason=f"压缩后 token 数({summary.token_count_after})未减少,跳过",
            )

        # 就地替换:用一条摘要消息顶掉被压缩的前缀,保留 recent tail。
        messages[:split] = [summary_msg]
        result = CompactionResult(True, summary=summary)
        if fallback_reason:
            result.reason = fallback_reason
        return result

    @staticmethod
    def _render_transcript(messages: list[dict[str, Any]]) -> str:
        """把一段历史消息渲染成给压缩模型看的纯文本(脱敏,紧凑)。"""
        parts: list[str] = []
        for m in messages:
            role = m.get("role", "?")
            content = m.get("content", "")
            if isinstance(content, str):
                body = content
            else:
                try:
                    body = json.dumps(content, ensure_ascii=False)
                except (TypeError, ValueError):
                    body = str(content)
            parts.append(f"[{role}] {body}")
        return redact("\n".join(parts))
