"""供 CLI、HTTP 和 TUI 复用的 Runtime Service。

Service 只协调 session、run、取消、事件和资源生命周期，不负责渲染终端界面。
现阶段仍复用 SessionRouter 做持久化和多 session 管理，后续前端不应直接操作
AgentSession 或 SessionRouter 的内部字段。
"""
from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from app.agent.approval_broker import ApprovalBroker
from app.agent.cancel import CancelToken
from app.agent.session_router import SessionRouter
from app.protocol.envelopes import EventEnvelope, utc_now
from app.protocol.errors import RuntimeFailure
from app.protocol.items import TurnItem
from app.protocol.models import TurnRecord


@dataclass(frozen=True)
class RuntimeEvent:
    """旧版进程内事件；新前端应使用 EventEnvelope。"""

    kind: str
    session_id: str | None
    run_id: str | None
    payload: Any = None


_LEGACY_EVENT_KIND = {
    "session_created": "thread.created",
    "session_resumed": "thread.resumed",
    "session_switched": "thread.selected",
    "session_images_cleared": "thread.images_cleared",
    "run_started": "turn.started",
    "turn_event": "turn.legacy_event",
    "agent_run_event": "turn.agent_event",
    "run_completed": "turn.completed",
    "run_failed": "turn.failed",
    "run_cancel_requested": "turn.cancel_requested",
}


@dataclass
class _ActiveRun:
    """正在执行的 run 及其协作式取消令牌。"""

    run_id: str
    session_id: str
    cancel: CancelToken


class RuntimeService:
    """线程安全的 Agent Runtime 门面。

    同一个 session 同时只允许一个 run，避免消息历史、TaskStore 和模型流式事件
    相互穿插；不同 session 可以由不同线程并行运行。
    """

    def __init__(
        self,
        router: SessionRouter,
        *,
        approval_broker: ApprovalBroker | None = None,
    ):
        self.router = router
        self.approval_broker = approval_broker or ApprovalBroker()
        self._lock = threading.RLock()
        self._runs: dict[str, _ActiveRun] = {}
        self._session_runs: dict[str, str] = {}
        self._subscribers: list[Callable[[RuntimeEvent], None]] = []
        self._protocol_subscribers: list[Callable[[EventEnvelope], None]] = []
        self._sequences: dict[str, int] = {}
        self._closed = False

    @property
    def current(self):
        """兼容 CLI 渲染层读取当前 session；业务操作应优先调用 Service 方法。"""
        return self.router.current

    @property
    def current_id(self) -> str | None:
        return self.router.current_id

    def subscribe(self, callback: Callable[[RuntimeEvent], None]) -> Callable[[], None]:
        """订阅 Runtime 事件，并返回取消订阅函数。"""
        with self._lock:
            self._ensure_open()
            self._subscribers.append(callback)

        def unsubscribe() -> None:
            with self._lock:
                if callback in self._subscribers:
                    self._subscribers.remove(callback)

        return unsubscribe

    def subscribe_protocol(
        self,
        callback: Callable[[EventEnvelope], None],
        *,
        thread_id: str | None = None,
        after_sequence: int = 0,
    ) -> Callable[[], None]:
        """订阅 Protocol v1，并在注册前按游标重放已持久化事件。"""
        if thread_id:
            storage = getattr(self.router, "_storage", None)
            if storage is not None:
                for row in storage.list_runtime_events(
                    thread_id, after_sequence=after_sequence,
                ):
                    callback(EventEnvelope(**row))
        with self._lock:
            self._ensure_open()
            self._protocol_subscribers.append(callback)

        def unsubscribe() -> None:
            with self._lock:
                if callback in self._protocol_subscribers:
                    self._protocol_subscribers.remove(callback)

        return unsubscribe

    def replay_events(
        self, thread_id: str, *, after_sequence: int = 0,
    ) -> list[EventEnvelope]:
        storage = getattr(self.router, "_storage", None)
        rows = storage.list_runtime_events(
            thread_id, after_sequence=after_sequence,
        ) if storage is not None else []
        return [EventEnvelope(**row) for row in rows]

    def _next_sequence(self, thread_id: str) -> int:
        with self._lock:
            storage = getattr(self.router, "_storage", None)
            persisted = storage.next_runtime_sequence(thread_id) if storage else 1
            cached = self._sequences.get(thread_id, 1)
            value = max(persisted, cached)
            self._sequences[thread_id] = value + 1
            return value

    def _publish_protocol(
        self,
        *,
        kind: str,
        thread_id: str,
        turn_id: str | None = None,
        item_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> EventEnvelope:
        event = EventEnvelope.create(
            sequence=self._next_sequence(thread_id),
            thread_id=thread_id,
            turn_id=turn_id,
            item_id=item_id,
            kind=kind,
            payload=payload,
        )
        storage = getattr(self.router, "_storage", None)
        if storage is not None:
            storage.append_runtime_event(event)
        with self._lock:
            subscribers = list(self._protocol_subscribers)
        for callback in subscribers:
            try:
                callback(event)
            except Exception:
                pass
        return event

    def publish(
        self,
        kind: str,
        *,
        session_id: str | None = None,
        run_id: str | None = None,
        payload: Any = None,
    ) -> None:
        """发布兼容事件，并在有 thread_id 时同步发布 Protocol v1 事件。"""
        event = RuntimeEvent(kind, session_id, run_id, payload)
        with self._lock:
            subscribers = list(self._subscribers)
        for callback in subscribers:
            try:
                callback(event)
            except Exception:
                pass
        if session_id:
            self._publish_protocol(
                kind=_LEGACY_EVENT_KIND.get(kind, f"legacy.{kind}"),
                thread_id=session_id,
                turn_id=run_id,
                payload=_protocol_payload(payload),
            )

    def new_session(self, agent_id: str | None = None, title: str = "") -> str:
        with self._lock:
            self._ensure_open()
            session_id = self.router.new(agent_id=agent_id, title=title)
        self.publish("session_created", session_id=session_id)
        return session_id

    def switch_session(self, session_id: str) -> bool:
        with self._lock:
            self._ensure_open()
            switched = self.router.switch(session_id)
        if switched:
            self.publish("session_switched", session_id=session_id)
        return switched

    def resume_or_new(self, agent_id: str | None = None) -> tuple[str, bool]:
        with self._lock:
            self._ensure_open()
            result = self.router.resume_or_new(agent_id=agent_id)
        self.publish(
            "session_resumed" if result[1] else "session_created",
            session_id=result[0],
        )
        return result

    def clear_session_images(self, session_id: str | None = None) -> dict[str, int]:
        with self._lock:
            self._ensure_open()
            target = session_id or self.router.current_id
            if target and target in self._session_runs:
                raise RuntimeError("Session 正在执行，不能清理图片；请先停止当前 run")
            result = self.router.clear_session_images(target)
        self.publish("session_images_cleared", session_id=target, payload=result)
        return result

    def list_sessions(self) -> list[dict]:
        return self.router.list_sessions()

    def list_profiles(self):
        return self.router.list_profiles()

    def handle_session_command(self, command: str) -> str | None:
        """兼容现有斜杠命令，命令造成的 session 变更仍通过 Service 入口调用。"""
        with self._lock:
            self._ensure_open()
            before = self.router.current_id
            result = self.router.handle_command(command)
            after = self.router.current_id
        if before != after:
            self.publish("session_switched", session_id=after)
        return result

    def send_message(
        self,
        message: str,
        *,
        images: list[Any] | None = None,
        session_id: str | None = None,
        resume: bool = False,
        cancel: CancelToken | None = None,
        run_id: str | None = None,
    ) -> str:
        """在目标 session 中执行一轮消息并自动持久化。

        ``cancel`` 可由 CLI 信号处理器注入；HTTP/TUI 通常保存返回的 run_id 后调用
        ``cancel_run``。无论成功、失败还是取消，finally 都会清理 run 注册表。
        """
        with self._lock:
            self._ensure_open()
            target_id = session_id or self.router.current_id
            if not target_id:
                raise RuntimeError("没有活跃 session")
            if target_id != self.router.current_id and not self.router.switch(target_id):
                raise KeyError(f"找不到 session: {target_id}")
            if target_id in self._session_runs:
                raise RuntimeError(f"session {target_id} 已有 run 正在执行")
            session = self.router.current
            if session is None:
                raise RuntimeError("session 加载失败")
            if images and not getattr(session.llm, "supports_vision", False):
                raise RuntimeError(
                    "当前模型 profile 未声明 vision 能力，不能发送图片；"
                    "请切换到支持图片的模型。"
                )
            actual_run_id = run_id or f"run-{uuid.uuid4().hex}"
            token = cancel or CancelToken()
            active = _ActiveRun(actual_run_id, target_id, token)
            self._runs[actual_run_id] = active
            self._session_runs[target_id] = actual_run_id

        # 将 AgentSession 的细粒度 TurnEvent/RunEvent 汇入 Service 事件总线；订阅仅在
        # 当前 run 生命周期内有效，避免重复执行后累积回调。
        unsubscribe = lambda: None
        subscribe_events = getattr(session, "subscribe_events", None)
        if callable(subscribe_events):
            unsubscribe = subscribe_events(
                on_turn=lambda event: self.publish(
                    "turn_event",
                    session_id=target_id,
                    run_id=actual_run_id,
                    payload=event,
                ),
                on_run=lambda event: self.publish(
                    "agent_run_event",
                    session_id=target_id,
                    run_id=actual_run_id,
                    payload=event,
                ),
            )

        self.publish("run_started", session_id=target_id, run_id=actual_run_id)
        turn = TurnRecord(
            turn_id=actual_run_id,
            thread_id=target_id,
            status="running",
            input_text=message,
        )
        storage = getattr(self.router, "_storage", None)
        if storage is not None:
            storage.save_runtime_turn(turn)
        input_sequence = self._next_sequence(target_id)
        input_item = TurnItem.create(
            item_id=f"item-{uuid.uuid4().hex}",
            thread_id=target_id,
            turn_id=actual_run_id,
            sequence=input_sequence,
            kind="user.message",
            status="completed",
            payload={"text": message, "image_count": len(images or [])},
        )
        if storage is not None:
            storage.append_runtime_item(input_item)
        self._publish_protocol(
            kind="item.completed",
            thread_id=target_id,
            turn_id=actual_run_id,
            item_id=input_item.item_id,
            payload={"item": input_item.to_dict()},
        )
        try:
            chat_kwargs = {"cancel": token, "resume": resume}
            # 不传空的 images kwarg，兼容只实现旧 chat(message,cancel,resume) 接口的
            # 测试替身和第三方 AgentSession。
            if images:
                chat_kwargs["images"] = images
            result = session.chat(message, **chat_kwargs)
            self.router.persist(target_id)
            usage = getattr(session, "last_turn_usage", {}) or {}
            completed_turn = TurnRecord(
                turn_id=actual_run_id,
                thread_id=target_id,
                status="completed",
                input_text=message,
                started_at=turn.started_at,
                finished_at=utc_now(),
                input_tokens=int(usage.get("input_tokens", 0)),
                output_tokens=int(usage.get("output_tokens", 0)),
            )
            if storage is not None:
                storage.save_runtime_turn(completed_turn)
            self.publish(
                "run_completed",
                session_id=target_id,
                run_id=actual_run_id,
                payload={"result": result},
            )
            return result
        except BaseException as exc:
            failure = RuntimeFailure.from_exception(exc)
            if storage is not None:
                storage.save_runtime_turn(TurnRecord(
                    turn_id=actual_run_id,
                    thread_id=target_id,
                    status="cancelled" if isinstance(exc, KeyboardInterrupt) else "failed",
                    input_text=message,
                    started_at=turn.started_at,
                    finished_at=utc_now(),
                    failure_code=failure.code,
                ))
            # BaseException 保证 KeyboardInterrupt 时也能发出收尾事件并释放 run 槽位。
            self.publish(
                "run_failed",
                session_id=target_id,
                run_id=actual_run_id,
                payload={"error": exc},
            )
            raise
        finally:
            unsubscribe()
            with self._lock:
                self._runs.pop(actual_run_id, None)
                if self._session_runs.get(target_id) == actual_run_id:
                    self._session_runs.pop(target_id, None)

    def active_runs(self) -> list[str]:
        with self._lock:
            return list(self._runs)

    def cancel_run(self, run_id: str) -> bool:
        """请求协作式取消；真正停止发生在 Orchestrator 的下一个安全检查点。"""
        with self._lock:
            active = self._runs.get(run_id)
            if active is None:
                return False
            active.cancel.cancel()
        self.publish("run_cancel_requested", session_id=active.session_id, run_id=run_id)
        return True

    def approve(self, request_id: str, feedback: str | None = None) -> bool:
        return self.approval_broker.resolve(request_id, "approve", feedback=feedback)

    def deny(self, request_id: str, feedback: str | None = None) -> bool:
        return self.approval_broker.resolve(request_id, "deny", feedback=feedback)

    def close(self) -> None:
        """幂等关闭：先取消 run/唤醒审批，再释放 session、PTY 和 MCP。"""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            active_runs = list(self._runs.values())
            self._subscribers.clear()
            self._protocol_subscribers.clear()
        for active in active_runs:
            active.cancel.cancel()
        self.approval_broker.close()
        self.router.close_all()

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("runtime service is closed")

    def __getattr__(self, name: str):
        """过渡期兼容 CLI 的 loop_handler 等只读扩展，后续逐步收敛为显式 API。"""
        return getattr(self.router, name)


def _protocol_payload(payload: Any) -> dict[str, Any]:
    """将旧事件转换为公共协议 payload；异常只暴露结构化摘要。"""
    if payload is None:
        return {}
    if isinstance(payload, BaseException):
        return {"failure": RuntimeFailure.from_exception(payload).to_dict()}
    if isinstance(payload, dict):
        converted = {}
        for key, value in payload.items():
            if isinstance(value, BaseException):
                converted[key] = RuntimeFailure.from_exception(value).to_dict()
            elif hasattr(value, "to_dict") and callable(value.to_dict):
                converted[key] = value.to_dict()
            elif hasattr(value, "__dataclass_fields__"):
                from dataclasses import asdict
                converted[key] = asdict(value)
            elif isinstance(value, (str, int, float, bool, type(None), list, dict)):
                converted[key] = value
            else:
                converted[key] = str(value)
        return converted
    if hasattr(payload, "to_dict") and callable(payload.to_dict):
        return {"value": payload.to_dict()}
    if hasattr(payload, "__dataclass_fields__"):
        from dataclasses import asdict
        return {"value": asdict(payload)}
    return {"value": str(payload)}
