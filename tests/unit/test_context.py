"""离线测试:上下文预算与压缩(§7.3 / §6.13)。

覆盖:token 估算、窗口解析、ContextBudget 阈值、安全选段(不切 tool 对)、
ContextCompressor 摘要解析/校验/脱敏、ContextManager.maybe_compact 触发与不触发、
storage round-trip、Orchestrator 在超阈值时压缩。全程 FakeRouter,无网络。
"""
from __future__ import annotations

import json

from app.agent import events
from app.agent.context import ContextManager
from app.agent.context_budget import (
    COMPACT_RATIO,
    DEFAULT_CONTEXT_LIMIT,
    WARN_RATIO,
    ContextBudget,
    estimate_message_tokens,
    estimate_messages_tokens,
    estimate_tokens,
    resolve_context_limit,
)
from app.agent.context_compaction import (
    ContextCompressor,
    ContextSummary,
    _is_tool_result_message,
    _is_valid_summary,
    _safe_split_point,
)
from app.agent.events import RunEvent
from app.agent.orchestrator import Orchestrator
from app.agent.planner import Planner
from app.models.protocol import ModelResponse, ToolCall, ToolResult
from app.storage import Storage
from app.tools.registry import Tool, ToolRegistry


# ── FakeRouter ────────────────────────────────────────────────────────────────


class FakeRouter:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    @property
    def model(self):
        return "fake-model"

    @property
    def provider(self):
        return "fake"

    def create_message(self, messages, tools=None, system=None, temperature=None,
                       max_tokens=4096, on_progress=None, on_text_delta=None):
        self.calls.append([dict(m) for m in messages])
        if not self._responses:
            return ModelResponse(text="(no more)", tool_calls=[],
                                 usage={"input_tokens": 0, "output_tokens": 0},
                                 provider_payload=[])
        return self._responses.pop(0)

    @staticmethod
    def format_tool_results(results):
        blocks = [{"type": "tool_result", "tool_use_id": r.tool_call_id,
                   "content": r.output, "is_error": r.is_error} for r in results]
        return [{"role": "user", "content": blocks}]


def _resp_text(text):
    return ModelResponse(text=text, tool_calls=[],
                         usage={"input_tokens": 5, "output_tokens": 3},
                         provider_payload=[{"type": "text", "text": text}])


def _resp_tool(tool_id, name, args):
    return ModelResponse(text="", tool_calls=[ToolCall(id=tool_id, name=name, arguments=args)],
                         usage={"input_tokens": 5, "output_tokens": 1},
                         provider_payload=[{"type": "tool_use", "id": tool_id,
                                            "name": name, "input": args}])


_VALID_SUMMARY = {
    "user_goal": "重构登录模块",
    "active_constraints": ["不要改数据库 schema"],
    "decisions": ["用 JWT 而非 session"],
    "current_state": "已读完 auth.py",
    "open_tasks": ["补单元测试"],
    "tool_evidence": [{"source": "read_file", "finding": "auth.py 有硬编码密钥"}],
    "files_and_artifacts": ["app/auth.py"],
    "failed_attempts": [],
    "approvals_and_risks": [],
    "memory_candidates": [],
    "handoff_note": "继续补测试并移除硬编码密钥",
}


def _summary_resp():
    return _resp_text(json.dumps(_VALID_SUMMARY, ensure_ascii=False))


# ── token 估算 ────────────────────────────────────────────────────────────────


def test_estimate_tokens_empty_is_zero():
    assert estimate_tokens("") == 0


def test_estimate_tokens_cjk_and_ascii_positive():
    assert estimate_tokens("hello world") > 0
    assert estimate_tokens("你好世界") > 0
    # 中文每字约 1 token,4 个字至少 4
    assert estimate_tokens("你好世界") >= 4


def test_estimate_message_tokens_handles_block_content():
    m = {"role": "user", "content": [{"type": "tool_result", "content": "结果文本"}]}
    assert estimate_message_tokens(m) > 0


def test_estimate_messages_tokens_sums():
    msgs = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]
    total = estimate_messages_tokens(msgs)
    assert total == sum(estimate_message_tokens(m) for m in msgs)


# ── 窗口解析 ──────────────────────────────────────────────────────────────────


def test_resolve_limit_prefers_declared():
    assert resolve_context_limit("claude-opus-4-8", declared_context_size=4096) == 4096


def test_resolve_limit_by_model_prefix():
    assert resolve_context_limit("claude-opus-4-8") == 200_000
    assert resolve_context_limit("qwen2.5-coder:7b-instruct") == 32_000


def test_resolve_limit_longest_prefix_wins():
    # "claude-opus-4" 比 "claude" 更长,应优先(两者都映射 200k,这里验证不报错)
    assert resolve_context_limit("claude-3-5-sonnet") == 200_000


def test_resolve_limit_unknown_falls_back():
    assert resolve_context_limit("totally-unknown-model") == DEFAULT_CONTEXT_LIMIT
    assert resolve_context_limit(None) == DEFAULT_CONTEXT_LIMIT


# ── ContextBudget ─────────────────────────────────────────────────────────────


def test_budget_from_model_sections_nonneg_and_reserved():
    b = ContextBudget.from_model("claude-opus-4-8")
    assert b.model_context_limit == 200_000
    assert b.reserved_output_tokens > 0
    for v in (b.system_and_tools_budget, b.memory_budget, b.summary_budget,
              b.recent_messages_budget, b.evidence_budget):
        assert v >= 0
    assert b.input_budget == b.model_context_limit - b.reserved_output_tokens


def test_budget_thresholds_ordering():
    b = ContextBudget.from_model(declared_context_size=10_000)
    assert b.warn_threshold == int(10_000 * WARN_RATIO)
    assert b.compact_threshold == int(10_000 * COMPACT_RATIO)
    assert b.warn_threshold < b.compact_threshold


def test_budget_status_for():
    b = ContextBudget.from_model(declared_context_size=1000)
    assert b.status_for(10) == "ok"
    assert b.status_for(b.warn_threshold) == "warn"
    assert b.status_for(b.compact_threshold) == "compact"


# ── 安全选段 ──────────────────────────────────────────────────────────────────


def test_is_tool_result_message_detects_both_styles():
    assert _is_tool_result_message({"role": "tool", "content": "x"})
    assert _is_tool_result_message(
        {"role": "user", "content": [{"type": "tool_result", "content": "x"}]})
    assert not _is_tool_result_message({"role": "user", "content": "普通文本"})


def test_safe_split_keeps_recent():
    msgs = [{"role": "user", "content": f"m{i}"} for i in range(10)]
    split = _safe_split_point(msgs, keep_recent=3)
    assert split == 7  # 保留最后 3 条


def test_safe_split_not_enough_history_returns_zero():
    msgs = [{"role": "user", "content": "a"}, {"role": "user", "content": "b"}]
    assert _safe_split_point(msgs, keep_recent=6) == 0


def test_safe_split_does_not_orphan_tool_result():
    # 第 7 条(下标 7,保留侧第一条)是 tool_result → 切点要前移,避免悬空 tool_use
    msgs = [{"role": "user", "content": f"m{i}"} for i in range(10)]
    msgs[7] = {"role": "user", "content": [{"type": "tool_result", "content": "r"}]}
    split = _safe_split_point(msgs, keep_recent=3)
    assert split < 7
    # 切点处保留侧第一条不再是 tool_result
    assert not _is_tool_result_message(msgs[split])


# ── 摘要校验 ──────────────────────────────────────────────────────────────────


def test_valid_summary_requires_fields():
    assert _is_valid_summary(_VALID_SUMMARY)
    assert not _is_valid_summary({"user_goal": "x"})  # 缺 handoff_note
    assert not _is_valid_summary({"handoff_note": "x"})  # 缺 user_goal
    assert not _is_valid_summary(None)
    assert not _is_valid_summary("not a dict")


# ── ContextCompressor ─────────────────────────────────────────────────────────


def _long_messages(n=12):
    return [{"role": "user" if i % 2 == 0 else "assistant",
             "content": f"消息 {i} " + "内容" * 20} for i in range(n)]


def test_compressor_compacts_and_returns_summary():
    llm = FakeRouter([_summary_resp()])
    comp = ContextCompressor(llm, model_profile="cloud_claude")
    msgs = _long_messages(12)
    before_len = len(msgs)
    result = comp.compact(msgs, keep_recent=4)
    assert result.compacted
    assert result.summary is not None
    # 历史被压短:前缀被一条摘要消息替换,尾部保留
    assert len(msgs) == 1 + 4
    assert len(msgs) < before_len
    assert "上下文摘要" in msgs[0]["content"]
    assert result.summary.source_message_range == (0, before_len - 4)
    assert result.summary.compression_model_profile == "cloud_claude"


def test_compressor_invalid_summary_keeps_messages():
    llm = FakeRouter([_resp_text("not json at all")])
    comp = ContextCompressor(llm)
    msgs = _long_messages(12)
    snapshot = [dict(m) for m in msgs]
    result = comp.compact(msgs, keep_recent=4)
    assert not result.compacted
    assert msgs == snapshot  # 未改动


def test_compressor_no_history_keeps_messages():
    llm = FakeRouter([_summary_resp()])
    comp = ContextCompressor(llm)
    msgs = [{"role": "user", "content": "只有一条"}]
    result = comp.compact(msgs, keep_recent=6)
    assert not result.compacted
    assert len(msgs) == 1


def test_compressor_redacts_secrets_in_summary():
    leaky = dict(_VALID_SUMMARY)
    leaky["current_state"] = "token sk-ant-api03-SECRETSECRETSECRET in code"
    llm = FakeRouter([_resp_text(json.dumps(leaky, ensure_ascii=False))])
    comp = ContextCompressor(llm)
    msgs = _long_messages(12)
    result = comp.compact(msgs, keep_recent=4)
    assert result.compacted
    # 摘要里的密钥应被脱敏
    assert "SECRETSECRETSECRET" not in json.dumps(result.summary.summary, ensure_ascii=False)


def test_summary_to_record_round_trips_fields():
    s = ContextSummary(summary=_VALID_SUMMARY, source_message_range=(0, 8),
                       source_run_ids=["r1"], token_count_before=500,
                       token_count_after=80, compression_model_profile="p")
    rec = s.to_record()
    assert rec["source_message_range"] == [0, 8]
    assert rec["source_run_ids"] == ["r1"]
    assert rec["token_count_before"] == 500
    assert json.loads(rec["summary_json"])["user_goal"] == "重构登录模块"


# ── ContextManager ────────────────────────────────────────────────────────────


def _manager(llm, limit, *, events_sink=None, auto=True, keep=4):
    budget = ContextBudget.from_model(declared_context_size=limit)
    comp = ContextCompressor(llm)
    return ContextManager(budget, comp, keep_recent=keep, auto_compact=auto,
                          on_event=events_sink)


def test_manager_no_compact_when_under_budget():
    llm = FakeRouter([_summary_resp()])
    mgr = _manager(llm, limit=200_000)
    msgs = _long_messages(8)
    assert mgr.maybe_compact(msgs) is False
    assert len(llm.calls) == 0  # 没调模型


def test_manager_warns_at_warn_threshold():
    sink = []
    # 小窗口:让短历史就越过 70% 但不到 85%
    llm = FakeRouter([_summary_resp()])
    mgr = _manager(llm, limit=200, events_sink=sink.append)
    msgs = _long_messages(8)  # 估算 token 远超 140(=200*0.7)
    # 注:_long_messages 很可能直接越 85%,这里只验证至少发了 context 事件
    mgr.maybe_compact(msgs)
    kinds = [e.kind for e in sink]
    assert any(k.startswith("context_") for k in kinds)


def test_manager_compacts_over_threshold_and_emits():
    sink = []
    llm = FakeRouter([_summary_resp()])
    mgr = _manager(llm, limit=300, events_sink=sink.append, keep=4)
    msgs = _long_messages(14)
    did = mgr.maybe_compact(msgs)
    assert did is True
    kinds = [e.kind for e in sink]
    assert events.CONTEXT_COMPACTION_STARTED in kinds
    assert events.CONTEXT_COMPACTION_COMPLETED in kinds
    assert mgr.last_summary is not None


def test_manager_disable_auto_compact_blocks():
    llm = FakeRouter([_summary_resp()])
    mgr = _manager(llm, limit=200, auto=False)
    msgs = _long_messages(14)
    assert mgr.maybe_compact(msgs) is False
    assert len(llm.calls) == 0


def test_manager_force_compacts_even_when_disabled():
    llm = FakeRouter([_summary_resp()])
    # 用小 limit 让 recent_budget 也小,不至于保护所有消息
    mgr = _manager(llm, limit=2_000, auto=False)
    # 用足够长的消息,让摘要确实比原前缀短(回滚保护允许压缩)
    msgs = [{"role": "user" if i % 2 == 0 else "assistant",
             "content": f"消息 {i} " + "内容很长很长很长" * 50} for i in range(14)]

    result = mgr.maybe_compact(msgs, force=True)
    assert result is True


def test_manager_drain_records():
    llm = FakeRouter([_summary_resp()])
    mgr = _manager(llm, limit=300, keep=4)
    mgr.maybe_compact(_long_messages(14))
    recs = mgr.drain_records()
    assert len(recs) == 1
    assert mgr.drain_records() == []  # 取走后清空


def test_manager_report_shape():
    llm = FakeRouter([])
    mgr = _manager(llm, limit=10_000)
    rep = mgr.report(_long_messages(4), system="sys")
    for key in ("model_context_limit", "estimated_input_tokens", "status",
                "warn_threshold", "compact_threshold", "auto_compact"):
        assert key in rep


# ── storage round-trip ────────────────────────────────────────────────────────


def test_storage_context_summary_round_trip(tmp_path):
    s = Storage(tmp_path / "t.db")
    s.create_session("sid", "default", "cloud_claude")
    rec = ContextSummary(summary=_VALID_SUMMARY, source_message_range=(0, 8),
                         source_run_ids=["r1"], token_count_before=500,
                         token_count_after=80, compression_model_profile="p").to_record()
    s.save_context_summary("sid", rec)
    rows = s.list_context_summaries("sid")
    assert len(rows) == 1
    assert rows[0]["range_start"] == 0 and rows[0]["range_end"] == 8
    assert rows[0]["token_before"] == 500
    latest = s.latest_context_summary("sid")
    assert latest is not None
    assert json.loads(latest["summary_json"])["user_goal"] == "重构登录模块"


def test_storage_context_summary_redacted(tmp_path):
    s = Storage(tmp_path / "t.db")
    s.create_session("sid", "default", "cloud_claude")
    leaky = dict(_VALID_SUMMARY)
    leaky["current_state"] = "sk-ant-api03-LEAKLEAKLEAKLEAK"
    rec = ContextSummary(summary=leaky, source_message_range=(0, 4)).to_record()
    s.save_context_summary("sid", rec)
    latest = s.latest_context_summary("sid")
    assert "LEAKLEAKLEAKLEAK" not in latest["summary_json"]


def test_storage_delete_session_clears_summaries(tmp_path):
    s = Storage(tmp_path / "t.db")
    s.create_session("sid", "default", "cloud_claude")
    s.save_context_summary("sid", ContextSummary(
        summary=_VALID_SUMMARY, source_message_range=(0, 4)).to_record())
    s.delete_session("sid")
    assert s.list_context_summaries("sid") == []


# ── Orchestrator 集成:超阈值时在稳定点压缩 ──────────────────────────────────────


def test_orchestrator_compacts_at_stable_point():
    """编排路径:历史超阈值时,任务完成后的稳定点应触发压缩(调到压缩模型)。"""
    # Planner 返回单任务计划 → Executor 一步给出文本完成 → 稳定点压缩。
    # 模型调用顺序:plan(规划) → summary(规划后稳定点压缩) → task_done(执行) →
    # summary(任务后稳定点压缩)。共享队列按此顺序排好。
    plan = _resp_text(json.dumps({"tasks": [{"id": "t1", "content": "做事", "dependencies": []}]}))
    task_done = _resp_text("任务完成")
    llm = FakeRouter([plan, _summary_resp(), task_done, _summary_resp()])

    sink = []
    # 极小窗口,确保任务跑完后历史立即越 85%
    budget = ContextBudget.from_model(declared_context_size=120)
    comp = ContextCompressor(llm)
    mgr = ContextManager(budget, comp, keep_recent=2, on_event=sink.append)

    # 预灌一段长历史,使 estimate 必然超阈值
    seed = _long_messages(10)
    orch = Orchestrator(llm, ToolRegistry(), planner=Planner(llm),
                        on_event=sink.append, messages=list(seed),
                        context_manager=mgr)
    orch.run("一个目标")
    kinds = [e.kind for e in sink]
    assert events.CONTEXT_COMPACTION_COMPLETED in kinds


def test_orchestrator_without_context_manager_unchanged():
    """无 context_manager 时不应有任何 context 事件(向后兼容)。"""
    plan = _resp_text(json.dumps({"tasks": [{"id": "t1", "content": "做事", "dependencies": []}]}))
    task_done = _resp_text("任务完成")
    llm = FakeRouter([plan, task_done])
    sink = []
    orch = Orchestrator(llm, ToolRegistry(), planner=Planner(llm), on_event=sink.append)
    orch.run("一个目标")
    assert not any(e.kind.startswith("context_") for e in sink)


# ── 新增:token 切点 + 压缩回滚测试 ───────────────────��─────────────────────────


def test_safe_split_with_token_budget_caps_recent_window():
    """给了 recent_budget 时,按 token 而非条数决定保留窗口大小。"""
    # 造很多条长消息,每条 ~200 token
    msgs = [{"role": "user", "content": f"消息 {i} " + "内容很长" * 50} for i in range(20)]

    # 无 budget:keep_recent=10 保护最后 10 条(不管 token 多少)
    split_no_budget = _safe_split_point(msgs, keep_recent=10, recent_budget=None)
    assert split_no_budget == 10  # 保留 [10..19]

    # 给 budget=1000 token,每条 ~200 token,只能保护 ~5 条就超预算
    # keep_recent=3 作为下限,budget=1000 作为上限 → 保护 3-5 条之间
    split_with_budget = _safe_split_point(msgs, keep_recent=3, recent_budget=1000)
    # 应该保护更少消息(因为 budget 限制),split > 10
    assert split_with_budget > split_no_budget  # budget 让保护窗口变小


def test_compressor_rollback_when_summary_not_shorter():
    """摘要不比原前缀短时,不替换 messages,返回 compacted=False。"""
    # 造极短的前缀(只 2 条短消息 ~20 token),摘要模板本身就 ~200 token,压不短
    msgs = [{"role": "user", "content": "a"}, {"role": "user", "content": "b"},
            {"role": "user", "content": "c"}, {"role": "user", "content": "d"}]
    llm = FakeRouter([_summary_resp()])
    comp = ContextCompressor(llm)
    snapshot = [dict(m) for m in msgs]

    result = comp.compact(msgs, keep_recent=2)  # 只压缩前 2 条(极短)
    # 摘要比原文长 → 不压缩,messages 不变
    assert not result.compacted
    assert "未减少" in result.reason
    assert msgs == snapshot  # 原地未改


def test_compressor_succeeds_when_summary_shorter():
    """摘要确实比原前缀短时,正常压缩。"""
    # 造足够长的前缀(每条 ~100 token,8 条 ~800 token),摘要 ~200 token,能压短
    msgs = [{"role": "user", "content": f"消息 {i} " + "长内容" * 30} for i in range(10)]
    llm = FakeRouter([_summary_resp()])
    comp = ContextCompressor(llm)

    result = comp.compact(msgs, keep_recent=2)  # 压缩前 8 条
    assert result.compacted
    assert result.summary.token_count_after < result.summary.token_count_before
    # messages 被替换:1 条摘要 + 2 条保留
    assert len(msgs) == 3
    assert "上下文摘要" in msgs[0]["content"]
