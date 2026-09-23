"""ModelRouter 重试边界、部分输出作废、协议映射的回归测试。

对应三处真实缺陷：
  1. 流式中途断连触发重试时，同一段正文会被推两遍，残文还可能被当成最终答案；
  2. 共享 Router 的重试事件必须归属于当前调用，不能广播给其他会话；
  3. provider_retry 没进协议映射表，前端只能靠 runtime.event 兜底。
"""
from __future__ import annotations

from contextlib import contextmanager

import pytest

from app.agent.approval import AutoApprove
from app.agent.events import PROVIDER_RETRY, RunEvent
from app.agent.runtime import AgentSession
from app.models.protocol import ModelResponse
from app.models.router import ModelRouter, call_create_message
from app.protocol.mapping import runtime_event_to_item
from app.tools.registry import ToolRegistry


class RemoteProtocolError(Exception):
    """同名模拟 httpx.RemoteProtocolError（不依赖 httpx 的类名判定）。"""


def make_router(adapter) -> ModelRouter:
    """直接组装真实 Router，并用无等待 sleep 保持测试快速。"""
    return ModelRouter(adapter, retry_sleep=lambda _delay: None)


class _Progress:
    """记录流式输出与作废调用的 progress 替身（对应 CLI spinner 的两个钩子）。"""

    def __init__(self):
        self.chunks: list[str] = []
        self.discards = 0

    def update(self, _metrics) -> None:
        pass

    def on_text(self, delta: str) -> None:
        self.chunks.append(delta)

    def discard_text(self) -> None:
        self.discards += 1


def progress_factory(holder: dict):
    @contextmanager
    def _progress(label: str):
        holder["p"] = _Progress()
        try:
            yield holder["p"]
        finally:
            pass
    return _progress


class StreamingCutThenOk:
    """第一跳流出半段后断连，第二跳从头流完整内容。"""

    provider = "fake"
    model = "cut-then-ok"
    full = "完整答案。"

    def __init__(self):
        self.calls = 0

    def create_message(self, messages, *, on_text_delta=None, **_kw):
        self.calls += 1
        if self.calls == 1:
            for ch in self.full[:3]:
                if on_text_delta:
                    on_text_delta(ch)
            raise RemoteProtocolError("Server disconnected")
        for ch in self.full:
            if on_text_delta:
                on_text_delta(ch)
        return ModelResponse(
            text=self.full, tool_calls=[], usage={},
            provider_payload=[{"role": "assistant", "content": self.full}],
            actual_model=self.model,
        )

    def format_tool_results(self, results):
        return []


class EmptyRetryAfterPartial:
    """第一跳流出残文后断连；第二跳"成功"但正文为空（只有工具调用等场景）。"""

    provider = "fake"
    model = "partial-leak"

    def __init__(self):
        self.calls = 0

    def create_message(self, messages, *, on_text_delta=None, **_kw):
        self.calls += 1
        if self.calls == 1:
            if on_text_delta:
                on_text_delta("这段本来要被丢弃")
            raise RemoteProtocolError("Server disconnected")
        return ModelResponse(
            text="", tool_calls=[], usage={},
            provider_payload=[{"role": "assistant", "content": "真实内容"}],
            actual_model=self.model,
        )

    def format_tool_results(self, results):
        return []


# ── 1. 重试不会把同一段正文推两遍 ─────────────────────────────────────────────

def test_streamed_text_not_duplicated_after_retry():
    """重试后 UI 拿到的应该是新那一遍的正文；返回给用户的答案必须是干净的。

    说明：已经写进终端的字符无法回滚，所以屏幕上难免看到残文 + 重试内容的拼接。
    这里断言的是能做到的保证 —— 返回值不带残文、UI 被明确告知这段作废。
    """
    adapter = StreamingCutThenOk()
    router = make_router(adapter)
    holder: dict = {}
    session = AgentSession(
        llm=router, tools=ToolRegistry(), approval=AutoApprove(),
        max_steps=1, progress=progress_factory(holder),
    )

    answer = session.chat("说点什么")

    assert adapter.calls == 2, "第一跳断连后应当自动重试一次"
    assert answer == adapter.full, f"返回的答案带了残文: {answer!r}"
    chunk_text = "".join(holder["p"].chunks)
    # 重试那一遍完整出现过，且残文之后被 discard 明确切断
    assert chunk_text.endswith(adapter.full)
    assert holder["p"].discards >= 1


def test_partial_output_discarded_before_retry():
    """重试前必须调用 discard_text 收掉屏幕上的残文（否则会拼在一行里）。"""
    adapter = StreamingCutThenOk()
    router = make_router(adapter)
    holder: dict = {}
    session = AgentSession(
        llm=router, tools=ToolRegistry(), approval=AutoApprove(),
        max_steps=1, progress=progress_factory(holder),
    )
    session.chat("说点什么")
    assert holder["p"].discards >= 1


def test_partial_text_not_returned_as_final_answer_after_retry():
    """重试成功后正文为空时，绝不能把被中断那一跳的残文当成最终答案返回。

    修复前这里会返回 "这段本来要被丢弃"（`resp.text or "".join(streamed_parts)`
    取到了被中断那一跳的残文）。修复后 buffered 残文已在重试前作废，本轮既没有
    正文也没有工具调用，runtime 如实报错而不是把垃圾当答案。
    """
    adapter = EmptyRetryAfterPartial()
    router = make_router(adapter)
    holder: dict = {}
    emitted: list = []
    session = AgentSession(
        llm=router, tools=ToolRegistry(), approval=AutoApprove(),
        max_steps=1, progress=progress_factory(holder),
        on_event=emitted.append,
    )

    with pytest.raises(RuntimeError) as caught:
        session.chat("说点什么")

    assert "这段本来要被丢弃" not in str(caught.value)
    texts = [e.text for e in emitted if e.kind == "text"]
    assert all("这段本来要被丢弃" not in t for t in texts)


# ── 2. 共享 Router 时，重试通知仍归属于发起调用的 Session ─────────────────────

def test_shared_router_does_not_broadcast_retry_to_other_sessions():
    """一个会话的网络重试不能污染共享同一 Router 的其他会话。"""
    adapter = StreamingCutThenOk()
    router = make_router(adapter)
    seen_a: list = []
    seen_b: list = []
    session_a = AgentSession(
        llm=router, tools=ToolRegistry(), approval=AutoApprove(),
        max_steps=1, on_run_event=seen_a.append,
    )
    session_b = AgentSession(
        llm=router, tools=ToolRegistry(), approval=AutoApprove(),
        max_steps=1, on_run_event=seen_b.append,
    )

    assert session_a.chat("触发一次瞬时故障") == adapter.full
    assert [e.kind for e in seen_a].count(PROVIDER_RETRY) == 1
    assert PROVIDER_RETRY not in [e.kind for e in seen_b]

    assert session_b.chat("正常调用") == adapter.full
    assert PROVIDER_RETRY not in [e.kind for e in seen_b]


# ── 3. 签名探测：新参数不能打挂旧替身 ────────────────────────────────────────

def test_call_create_message_skips_unsupported_optional_kwargs():
    class OldAdapter:
        def __init__(self):
            self.got: dict = {}

        def create_message(self, messages, **kwargs):
            self.got = kwargs
            return "ok"

    class StrictOldAdapter:
        """不接受 cancel/discard_partial，但能收基础关键字的替身。"""

        def create_message(self, messages, tools=None, system=None,
                           temperature=None, max_tokens=None, on_progress=None,
                           on_text_delta=None, on_thinking_delta=None):
            return "ok"

    dest = OldAdapter()
    assert call_create_message(
        dest, [], cancel="token", discard_partial=lambda _e: None,
        on_retry=lambda *_: None,
    ) == "ok"
    assert dest.got["cancel"] == "token"
    assert callable(dest.got["discard_partial"])
    assert callable(dest.got["on_retry"])

    # 旧签名替身不该因为多传 cancel/discard_partial 而 TypeError
    assert call_create_message(
        StrictOldAdapter(), [], cancel="token", discard_partial=lambda _e: None,
        on_retry=lambda *_: None,
    ) == "ok"


# ── 4. 协议映射 ──────────────────────────────────────────────────────────────

def test_provider_retry_maps_to_structured_item():
    item = runtime_event_to_item(
        RunEvent(kind=PROVIDER_RETRY, text="x", payload={"attempt": 1, "delay": 1.5}),
        thread_id="s", turn_id="r", sequence=1,
    )
    assert item is not None, "provider_retry 未映射，前端只能拿到 runtime.event 兜底"
    assert item.kind == "provider.retry"
    assert item.status == "waiting"
    # RunEvent.payload 会被整体收进 item.payload 的 "payload" 字段
    assert item.payload["payload"]["attempt"] == 1
    assert item.payload["payload"]["delay"] == 1.5
