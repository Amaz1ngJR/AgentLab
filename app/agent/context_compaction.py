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
import math
import threading
import time
import queue
import contextvars
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Optional

from app.agent.cancel import Cancelled, CancelToken
from app.agent.context_budget import estimate_messages_tokens, estimate_message_tokens, estimate_tokens
from app.models.router import call_create_message
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
- 输入可能是历史分块或多份分块摘要。合并时保留约束、授权和失败证据；较晚状态优先，未覆盖的部分不要声称已完成。
"""

# 摘要消息注入历史时的外层模板:明确告诉后续模型"这是被压缩的早期历史摘要"。
_SUMMARY_MESSAGE_TEMPLATE = (
    "【上下文摘要 · 早期历史已压缩】\n"
    "以下是本会话较早部分的结构化摘要(原始消息已从模型输入中省略,但仍保留在审计记录中)。"
    "摘要是有损的历史参考；用户原文及当前指令优先。未知或降级内容须重新核实，不要重复已确认完成的工作。\n\n{body}\n\n"
    "{skills_reminder}"
)

# 摘要必填字段：缺失时先重试，仍失败则按配置进入本地降级。
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

    def to_message_text(self, available_skills: list[str] | None = None) -> str:
        """渲染成注入历史的摘要消息正文(人读 + 模型读都友好的紧凑文本)。

        Args:
            available_skills: 可用的 skill 列表，压缩后重新提醒模型
        """
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
        _add("信息降级", s.get("degradation"))

        # 生成 skills 提醒
        skills_reminder = ""
        if available_skills:
            skills_list = ", ".join(f"/{s}" for s in available_skills)
            skills_reminder = (
                f"【可用技能】\n"
                f"本项目配置了以下 skills：{skills_list}\n"
                f"执行相关任务时，优先查看对应的 skill 定义。"
            )

        return _SUMMARY_MESSAGE_TEMPLATE.format(
            body="\n".join(lines),
            skills_reminder=skills_reminder
        )

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
    # 并行调用的多个结果可能跨越边界，不能只看第一条尾部消息。
    calls: dict[str, int] = {}
    spans: list[tuple[int, int]] = []
    completed: set[str] = set()
    for index, message in enumerate(messages):
        kind = message.get("type")
        if kind == "function_call" and message.get("call_id"):
            calls[message["call_id"]] = index
        elif kind == "function_call_output" and message.get("call_id") in calls:
            spans.append((calls[message["call_id"]], index))
            completed.add(message["call_id"])
        for call in message.get("tool_calls", []) or []:
            if call.get("id"):
                calls[call["id"]] = index
        if message.get("role") == "tool" and message.get("tool_call_id") in calls:
            spans.append((calls[message["tool_call_id"]], index))
            completed.add(message["tool_call_id"])
        content = message.get("content")
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("id"):
                calls[block["id"]] = index
            elif block.get("type") == "tool_result" and block.get("tool_use_id") in calls:
                spans.append((calls[block["tool_use_id"]], index))
                completed.add(block["tool_use_id"])
    # 尚未返回结果的调用也不能进入已压缩前缀。
    split = min([split, *(index for key, index in calls.items() if key not in completed)])
    while True:
        crossing = [start for start, end in spans if start < split <= end]
        if not crossing:
            break
        split = min(crossing)
    return max(0, split)


def _is_real_user(message: dict[str, Any]) -> bool:
    return (message.get("role") == "user"
            and not _is_tool_result_message(message)
            and not str(message.get("content", "")).startswith("【上下文摘要"))


def _split_transcript(text: str, token_budget: int) -> list[str]:
    """按 token 预算分块，覆盖全文，绝不静默丢弃中段。"""
    chunks = []
    while text:
        lo, hi = 1, min(len(text), token_budget * 4)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if estimate_tokens(text[:mid]) <= token_budget:
                lo = mid
            else:
                hi = mid - 1
        chunks.append(text[:lo])
        text = text[lo:]
    return chunks


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
        if not isinstance(obj.get(f), str) or not obj[f].strip():
            return False
    return True


class CompactionResult:
    """maybe 一次压缩的结果。compacted=False 表示本次未压缩(预算够 / 无可压段 / 失败)。"""

    def __init__(self, compacted: bool, summary: Optional[ContextSummary] = None,
                 reason: str = "", code: str = ""):
        self.code = code
        self.compacted = compacted
        self.summary = summary
        self.reason = reason


def _fallback_summary(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """模型摘要失败时的本地安全兜底，不编造事实，只保留可追溯摘要。"""
    user_messages = [
        str(m.get("content", "")) for m in messages if _is_real_user(m)
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
            "finding": f"最近一条助手输出（未重新验证）：{last_assistant}",
        }] + [{"source": str(m.get("call_id") or m.get("tool_call_id") or "tool_result"),
               "finding": json.dumps(m, ensure_ascii=False, default=str)[-600:]}
              for m in messages if _is_tool_result_message(m)][-3:],
        "files_and_artifacts": [],
        "failed_attempts": [],
        "approvals_and_risks": [],
        "memory_candidates": [],
        "handoff_note": "模型摘要生成失败，以上为本地有界兜底；不要把未知内容当作已确认事实。",
    }


class ContextCompressor:
    """选段 → 调模型摘要 → 校验/脱敏 → 产出摘要消息并就地替换旧历史。

    timeout_seconds 可显式限制整次压缩；默认按分片及合并请求数预留时间，
    每次请求预留 120 秒，整次最多 600 秒，开始后不续期。
    超时不使用有损本地兜底，保留原始历史；后台迟到响应由请求隔离层丢弃。
    默认利用调用方的完整安全输入预算；未知窗口时使用 4096。
    必须分片时最多并发两个摘要请求，合并仍按原始历史顺序。
    """

    def __init__(self, llm, system: str = COMPRESSION_SYSTEM,
                 model_profile: str = "", allow_local_fallback: bool = True,
                 available_skills: list[str] | None = None,
                 summary_input_tokens: int | None = None,
                 timeout_seconds: float | None = None,
                 max_parallel_requests: int = 2):
        self._llm = llm
        self._system = system
        self._model_profile = model_profile
        self._allow_local_fallback = allow_local_fallback
        self._available_skills = available_skills or []
        if summary_input_tokens is not None and summary_input_tokens < 256:
            raise ValueError("summary_input_tokens must be at least 256")
        if timeout_seconds is not None and (not math.isfinite(timeout_seconds) or timeout_seconds <= 0):
            raise ValueError("timeout_seconds must be finite and positive")
        if max_parallel_requests < 1:
            raise ValueError("max_parallel_requests must be positive")
        self.max_parallel_requests = max_parallel_requests
        self.timeout_seconds = timeout_seconds
        self._summary_input_tokens = summary_input_tokens
        self.last_usage: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}

    def _input_limit(self, input_token_budget: int | None) -> int:
        preferred = self._summary_input_tokens or (input_token_budget or 4096)
        return min(preferred, input_token_budget or preferred)

    def timeout_for(self, messages: list[dict[str, Any]], input_token_budget: int | None) -> float:
        """一次性规划时间预算；包含合并轮次，不随重试或进度事件续期。"""
        if self.timeout_seconds is not None:
            return self.timeout_seconds
        tokens = estimate_tokens(self._render_transcript(messages))
        limit = self._input_limit(input_token_budget)
        chunks = max(1, math.ceil(tokens / limit))
        calls = chunks
        # 按每份摘要最多 4096 tokens 预留后续合并；小窗口无法收敛时交给硬上限。
        while chunks > 1 and calls < 5:
            reduced = max(1, math.ceil(chunks * min(4096, limit) / limit))
            calls += reduced
            if reduced >= chunks:
                break
            chunks = reduced
        return min(600, max(120, calls * 120))

    def compact(
        self,
        messages: list[dict[str, Any]],
        *,
        keep_recent: int,
        recent_budget: Optional[int] = None,
        source_run_ids: Optional[list[str]] = None,
        summary_budget: int = 1024,
        input_token_budget: int | None = None,
        target_message_tokens: int | None = None,
        on_progress=None,
        cancel: CancelToken | None = None,
        deadline: float | None = None,
        on_compaction_progress=None,
    ) -> CompactionResult:
        """就地压缩 messages 的可压缩前缀。成功时 messages 被原地改短。

        keep_recent: 至少保留的最近消息条数(下限,保护最后用户请求与正在执行的任务)。
        recent_budget: 最近窗口的 token 上限(可选);给了就按 token 决定保留多少,
            超大单条消息会被挤出保留窗口、纳入可压缩区。
        返回 CompactionResult;失败/无可压段时 messages 不变、compacted=False。
        """
        started = time.monotonic()
        deadline = min(deadline or float("inf"), started + self.timeout_for(messages, input_token_budget))
        if cancel is not None:
            cancel.raise_if_cancelled()
        self.last_usage = {"input_tokens": 0, "output_tokens": 0}
        split = _safe_split_point(messages, keep_recent, recent_budget)
        if split <= 0:
            return CompactionResult(False, reason="无可安全压缩的前缀")

        old = messages[:split]
        token_before = estimate_messages_tokens(old)

        # 独立保留最近真实用户请求；即使工具循环很长也不会被摘要转述替代。
        last_user = next((i for i in range(len(messages) - 1, -1, -1)
                          if _is_real_user(messages[i])), None)
        pinned = [m for i, m in enumerate(old)
                  if m.get("role") in {"system", "developer"} or i == last_user]
        if (target_message_tokens is not None
                and estimate_messages_tokens([*pinned, *messages[split:]]) > target_message_tokens):
            return CompactionResult(False, reason="受保护消息超出安全输入预算", code="budget_exceeded")
        transcript = self._render_transcript(old)
        fallback_reason = ""
        request_cancel = CancelToken()
        unsubscribe = (cancel.add_callback(lambda: request_cancel.cancel("user"))
                       if cancel is not None else lambda: None)
        timer = threading.Timer(max(0, deadline - time.monotonic()),
                                lambda: request_cancel.cancel("compaction_timeout"))
        timer.daemon = True
        timer.start()
        try:
            obj = self._summarize(
                transcript, summary_budget, on_progress, request_cancel, input_token_budget,
                on_compaction_progress, started, deadline,
            )
            request_cancel.raise_if_cancelled()
        except Cancelled:
            if cancel is not None:
                cancel.raise_if_cancelled()
            if request_cancel.reason == "compaction_timeout":
                return CompactionResult(False, reason="压缩达到整体耗时上限，保留原始历史",
                                        code="timeout")
            raise
        except Exception as exc:  # 模型异常:进入本地兜底,不让手动 compact 失效
            if not self._allow_local_fallback:
                return CompactionResult(False, reason=f"压缩模型调用失败: {exc}")
            # 保留诊断原因供 ContextManager/UI 展示，但继续构造本地摘要。
            obj = _fallback_summary(old)
            fallback_reason = f"local_fallback:{type(exc).__name__}"
        finally:
            timer.cancel()
            unsubscribe()

        if time.monotonic() >= deadline:
            return CompactionResult(False, reason="压缩达到整体耗时上限，保留原始历史",
                                    code="timeout")

        if fallback_reason:
            pinned = [m for m in old if m.get("role") in {"system", "developer", "user"}
                      and not _is_tool_result_message(m)]
            obj["degradation"] = (
                "模型摘要不可用；用户原文和已有摘要另行保留。工具历史仅保留片段，"
                "未收录证据与执行状态须从原始审计记录重新核实。"
            )
        obj = _redact_summary(obj)
        summary = ContextSummary(
            summary=obj,
            source_message_range=(0, split),
            source_run_ids=list(source_run_ids or []),
            token_count_before=token_before,
            compression_model_profile=self._model_profile,
        )
        # 传入 skills 列表，在压缩摘要中重新提醒模型
        summary_msg = {"role": "user", "content": summary.to_message_text(self._available_skills)}
        summary.token_count_after = estimate_messages_tokens([summary_msg, *pinned])

        # 兜底:若摘要并不比被压缩的原前缀短(短前缀 + 结构化摘要骨架开销可能反而
        # 更长),压缩没有收益,不替换 —— 否则白白消耗一次模型调用还可能让上下文变大。
        if summary.token_count_after >= token_before:
            return CompactionResult(
                False,
                reason=f"压缩后 token 数({summary.token_count_after})未减少,跳过",
            )

        candidate = [summary_msg, *pinned, *messages[split:]]
        if target_message_tokens is not None and estimate_messages_tokens(candidate) > target_message_tokens:
            return CompactionResult(False, reason="压缩后仍超出安全输入预算，保留原始历史", code="budget_exceeded")
        # 验证完整候选后才提交，失败或取消都不会破坏历史。
        if cancel is not None:
            cancel.raise_if_cancelled()
        if time.monotonic() >= deadline:
            return CompactionResult(False, reason="压缩达到整体耗时上限，保留原始历史",
                                    code="timeout")
        messages[:] = candidate
        result = CompactionResult(True, summary=summary)
        if fallback_reason:
            result.reason = fallback_reason
        return result

    @property
    def summary_system_tokens(self) -> int:
        return estimate_tokens(self._system)

    @staticmethod
    def _render_transcript(messages: list[dict[str, Any]]) -> str:
        """把一段历史消息渲染成给压缩模型看的纯文本(脱敏,紧凑)。"""
        return redact("\n".join(
            json.dumps(m, ensure_ascii=False, default=str, separators=(",", ":"))
            for m in messages
        ))

    def _summarize(self, transcript: str, output_tokens: int, on_progress,
                   cancel: CancelToken | None, input_token_budget: int | None = None,
                   on_compaction_progress=None, started: float = 0,
                   deadline: float = float("inf")) -> dict:
        output_tokens = max(256, min(4096, output_tokens))
        text = transcript
        # 已知安全窗口时利用可用预算，避免长历史被固定 4k 切成几十次串行请求。
        # 未声明窗口的直接调用保持保守；显式分片限制和小模型预算仍然生效。
        input_limit = self._input_limit(input_token_budget)
        last_report = 0.0
        state = {}

        def report(force=False, **updates):
            nonlocal last_report
            cancel.raise_if_cancelled()
            now = time.monotonic()
            if now >= deadline:
                cancel.cancel("compaction_timeout")
                cancel.raise_if_cancelled()
            state.update(updates)
            if on_compaction_progress is not None and (force or now - last_report >= 5):
                last_report = now
                on_compaction_progress({**state, "elapsed_seconds": now - started,
                                        "remaining_seconds": max(0, deadline - now)})

        def request(index, chunk, emit, token):
            usage = {"input_tokens": 0, "output_tokens": 0}
            for attempt in range(2):
                token.raise_if_cancelled()
                emit("status", {"chunk": index, "attempt": attempt + 1, "phase": "request"})
                response = call_create_message(
                    self._llm, [{"role": "user", "content": chunk}],
                    tools=None, system=self._system, max_tokens=output_tokens,
                    on_progress=lambda metrics: emit("metrics", (index, metrics)), cancel=token,
                    on_wait=lambda: emit("wait", {}),
                    on_retry=lambda attempt, delay, exc: emit("status", {
                        "chunk": index, "phase": "retry", "retry": attempt, "retry_delay": delay}),
                )
                for key in usage:
                    usage[key] += (getattr(response, "usage", None) or {}).get(key, 0)
                obj = _extract_json(getattr(response, "text", "") or "")
                if (_is_valid_summary(obj)
                        and getattr(response, "finish_reason", None) not in {"length", "max_tokens"}):
                    return obj, usage
            raise ValueError("invalid_summary")

        for round_index in range(8):
            chunks = _split_transcript(text, input_limit)
            results = [None] * len(chunks)
            snapshots = {}
            finished_indices = set()
            base_usage = dict(self.last_usage)
            completed = 0
            report(True, round=round_index + 1, chunk=1, chunks=len(chunks),
                   completed=0, parallel=min(len(chunks), self.max_parallel_requests),
                   attempt=1, phase="request")

            def emit(kind, value):
                if kind == "metrics":
                    index, metrics = value
                    if index in finished_indices:
                        return
                    snapshots[index] = metrics
                    if on_progress is not None:
                        on_progress({key: base_usage[key] + sum(
                            metrics.get(key, 0) for metrics in snapshots.values())
                            for key in base_usage})
                elif kind == "status":
                    if value.get("chunk") not in finished_indices:
                        report(True, **value)
                else:
                    report()

            def accept(index, result):
                nonlocal completed
                obj, usage = result
                results[index - 1] = obj
                for key in self.last_usage:
                    self.last_usage[key] += usage[key]
                completed += 1
                # 最终实际用量覆盖该分片的流式估算，其他分片继续保留。
                emit("metrics", (index, usage))
                finished_indices.add(index)
                report(True, chunk=index, completed=completed, phase="completed")

            try:
                if len(chunks) == 1 or self.max_parallel_requests == 1:
                    for index, chunk in enumerate(chunks, 1):
                        accept(index, request(index, chunk, emit, cancel))
                else:
                    mailbox = queue.Queue()
                    batch_cancel = CancelToken()
                    unsubscribe = cancel.add_callback(lambda: batch_cancel.cancel("parent"))
                    pool = ThreadPoolExecutor(max_workers=self.max_parallel_requests,
                                              thread_name_prefix="agentlab-summary")
                    pending = {}
                    next_index = 1

                    def launch(index):
                        context = contextvars.copy_context()
                        pending[pool.submit(context.run, request, index, chunks[index - 1],
                                            lambda kind, value: mailbox.put((kind, value)),
                                            batch_cancel)] = index

                    try:
                        while next_index <= min(len(chunks), self.max_parallel_requests):
                            launch(next_index)
                            next_index += 1
                        while pending:
                            report()
                            try:
                                emit(*mailbox.get(timeout=0.02))
                            except queue.Empty:
                                pass
                            for future in list(pending):
                                if future.done():
                                    index = pending.pop(future)
                                    accept(index, future.result())
                                    if next_index <= len(chunks):
                                        launch(next_index)
                                        next_index += 1
                        # 丢弃最后已完成分片的旧流式快照，避免覆盖实际累计用量。
                    finally:
                        batch_cancel.cancel("batch_finished")
                        unsubscribe()
                        pool.shutdown(wait=False, cancel_futures=True)
            except Cancelled:
                raise
            except Exception as exc:
                code = str(getattr(exc, "code", "")) + " " + str(exc)
                if any(marker in code.lower() for marker in (
                    "context_length_exceeded", "context_window_exceeded",
                    "maximum context length", "context window exceeded",
                )) and input_limit > 256:
                    input_limit = max(256, input_limit // 2)
                    continue
                raise
            if len(results) == 1:
                return results[0]
            merged = redact(json.dumps(results, ensure_ascii=False))
            if estimate_tokens(merged) >= estimate_tokens(text):
                raise ValueError("summary_did_not_shrink")
            text = merged
        raise ValueError("summary_reduction_limit")
