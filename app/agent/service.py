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
from app.agent.submission import SubmissionQueue, SubmissionQueueOverloaded, TurnSubmission
from app.protocol.envelopes import EventEnvelope, utc_now
from app.protocol.errors import RuntimeFailure
from app.protocol.items import TurnItem
from app.protocol.json_types import to_json_value
from app.protocol.mapping import item_event_kind, runtime_event_to_item
from app.protocol.models import TurnRecord
from app.protocol.handshake import (
    ClientInfo,
    InitializeResult,
    SUPPORTED_CAPABILITIES,
    negotiate_capabilities,
)
from app.protocol.subscription import EventQueueOverloaded, EventSubscription


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
    "session_model_switched": "thread.model_switched",
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
        self._queue_subscribers: dict[str, EventSubscription] = {}
        self._clients: dict[str, ClientInfo] = {}
        self._sequences: dict[str, int] = {}
        self._sequence_lock = threading.Lock()
        self._thread_locks: dict[str, threading.RLock] = {}
        self._submissions = SubmissionQueue(self._run_submission)
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

    def initialize_client(self, client: ClientInfo) -> InitializeResult:
        """执行一次进程内 Protocol 握手，并返回协商后的能力。"""
        with self._lock:
            self._ensure_open()
            client_id = f"client-{uuid.uuid4().hex}"
            self._clients[client_id] = client
        return InitializeResult(
            protocol_version=1,
            client_id=client_id,
            accepted_capabilities=negotiate_capabilities(client),
            server_capabilities=tuple(sorted(SUPPORTED_CAPABILITIES)),
        )

    def open_event_subscription(
        self,
        client_id: str,
        *,
        thread_id: str | None = None,
        after_sequence: int = 0,
        max_queue_size: int = 256,
    ) -> EventSubscription:
        """为已初始化客户端创建有界队列，并先写入游标之后的历史事件。"""
        with self._lock:
            self._ensure_open()
            if client_id not in self._clients:
                raise RuntimeError("client not initialized")
            subscription = EventSubscription(client_id, max_queue_size=max_queue_size)
            self._queue_subscribers[client_id] = subscription
        if thread_id:
            for event in self.replay_events(thread_id, after_sequence=after_sequence):
                subscription.put(event)
        return subscription

    def close_client(self, client_id: str) -> None:
        with self._lock:
            subscription = self._queue_subscribers.pop(client_id, None)
            self._clients.pop(client_id, None)
        if subscription:
            subscription.close()

    def _protocol_storage(self):
        """返回 Runtime Protocol 的持久化端口；测试替身可不提供该端口。"""
        return getattr(self.router, "storage", None)

    def subscribe_protocol(
        self,
        callback: Callable[[EventEnvelope], None],
        *,
        thread_id: str | None = None,
        after_sequence: int = 0,
    ) -> Callable[[], None]:
        """订阅 Protocol v1，并在注册前按游标重放已持久化事件。"""
        if thread_id:
            storage = self._protocol_storage()
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
        storage = self._protocol_storage()
        rows = storage.list_runtime_events(
            thread_id, after_sequence=after_sequence,
        ) if storage is not None else []
        return [EventEnvelope(**row) for row in rows]

    def _thread_lock(self, thread_id: str) -> threading.RLock:
        """返回某个 Thread 的写入锁。

        序号分配、落库和派发必须在同一把锁里完成，否则两个线程可能按 1、2 拿到
        序号却按 2、1 落库/派发，让 append-only 事实流出现乱序。锁按 Thread 划分，
        不同 Thread 仍可并行，也避免在订阅者回调期间持有全局锁。
        """
        with self._sequence_lock:
            lock = self._thread_locks.get(thread_id)
            if lock is None:
                lock = threading.RLock()
                self._thread_locks[thread_id] = lock
            return lock

    def _allocate_sequence(self, thread_id: str) -> int:
        """分配该 Thread 的下一个序号；调用方需持有对应的 thread 锁。"""
        storage = self._protocol_storage()
        if storage is not None:
            value = storage.allocate_runtime_sequence(thread_id)
            self._sequences[thread_id] = value + 1
            return value
        # 没有持久化端口(测试替身)时退回进程内计数器。
        value = self._sequences.get(thread_id, 1)
        self._sequences[thread_id] = value + 1
        return value

    def _record_explicit_sequence(self, thread_id: str, sequence: int) -> None:
        """同步显式序号，避免下一条自动序号回退到已占用位置。"""
        self._sequences[thread_id] = max(
            self._sequences.get(thread_id, 1), sequence + 1,
        )

    def _publish_protocol(
        self,
        *,
        kind: str,
        thread_id: str,
        sequence: int | None = None,
        turn_id: str | None = None,
        item_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> EventEnvelope:
        """落库并派发一个独立的 Protocol 事件。"""
        with self._thread_lock(thread_id):
            if sequence is None:
                sequence = self._allocate_sequence(thread_id)
            else:
                self._record_explicit_sequence(thread_id, sequence)
            event = EventEnvelope.create(
                sequence=sequence,
                thread_id=thread_id,
                turn_id=turn_id,
                item_id=item_id,
                kind=kind,
                payload=payload,
            )
            storage = self._protocol_storage()
            if storage is not None:
                storage.append_runtime_event(event)
            self._dispatch_protocol(event)
        return event

    def _dispatch_protocol(self, event: EventEnvelope) -> None:
        """把已落库的事件派发给协议订阅者和有界队列。"""
        with self._lock:
            subscribers = list(self._protocol_subscribers)
            queue_subscribers = list(self._queue_subscribers.items())
        for callback in subscribers:
            try:
                callback(event)
            except Exception:
                pass
        overloaded: list[str] = []
        for client_id, subscription in queue_subscribers:
            try:
                subscription.put(event)
            except EventQueueOverloaded:
                overloaded.append(client_id)
        for client_id in overloaded:
            self.close_client(client_id)

    def _publish_item(
        self, thread_id: str, build_item: Callable[[int], TurnItem],
    ) -> TurnItem:
        """在同一把 Thread 锁内分配序号、原子落库并派发 item 事件。

        Item 和描述它的 Event 共享同一个 sequence，并写在同一个事务里：两条事实流
        因此不会漂移，也不会留下只落一半的中间态。
        """
        with self._thread_lock(thread_id):
            sequence = self._allocate_sequence(thread_id)
            item = build_item(sequence)
            event = EventEnvelope.create(
                sequence=sequence,
                thread_id=thread_id,
                turn_id=item.turn_id,
                item_id=item.item_id,
                kind=item_event_kind(item),
                payload={"item": item.to_dict()},
            )
            storage = self._protocol_storage()
            if storage is not None:
                storage.append_runtime_item_with_event(item, event)
            self._dispatch_protocol(event)
        return item

    def _publish_runtime_item(
        self, event: Any, *, thread_id: str, turn_id: str, source: str,
    ) -> TurnItem | None:
        def build_item(sequence: int) -> TurnItem:
            item = runtime_event_to_item(
                event, thread_id=thread_id, turn_id=turn_id, sequence=sequence,
            )
            if item is not None:
                return item
            # 未知事件保留为结构化 legacy item，避免事件在协议层静默丢失。
            return TurnItem.create(
                item_id=f"item-{uuid.uuid4().hex}",
                thread_id=thread_id,
                turn_id=turn_id,
                sequence=sequence,
                kind="runtime.event",
                status="completed",
                payload={
                    "source": source,
                    "event": to_json_value(_protocol_payload(event)),
                },
            )

        item = self._publish_item(thread_id, build_item)
        # 旧 callback 仍收到原事件，避免 CLI/第三方迁移期间行为变化。
        legacy = RuntimeEvent(source, thread_id, turn_id, event)
        with self._lock:
            legacy_subscribers = list(self._subscribers)
        for callback in legacy_subscribers:
            try:
                callback(legacy)
            except Exception:
                pass
        return item

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

    def switch_model(self, model_profile: str) -> tuple[str | None, str]:
        """仅在会话空闲时切换模型，防止流式响应与历史转换并发发生。"""
        with self._lock:
            self._ensure_open()
            target = self.router.current_id
            if not target:
                raise RuntimeError("当前无活跃 session")
            if target in self._session_runs:
                raise RuntimeError("Session 正在执行，不能切换模型；请先停止当前 run")
            result = self.router.switch_model(model_profile)
        self.publish(
            "session_model_switched",
            session_id=target,
            payload={"old_profile": result[0], "model_profile": result[1]},
        )
        return result

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

    def get_thread(self, thread_id: str | None = None) -> dict | None:
        target = thread_id or self.router.current_id
        if not target:
            return None
        row = self.thread_record(target)
        return row

    def current_thread_summary(self) -> dict | None:
        thread_id = self.router.current_id
        if not thread_id:
            return None
        row = self.get_thread(thread_id) or {}
        session = self.router.current
        return {
            "thread_id": thread_id,
            "title": row.get("title", ""),
            "agent_id": row.get("agent_id", ""),
            "model_profile": row.get("model_profile", ""),
            "message_count": len(getattr(session, "messages", []) or []),
        }

    def storage(self):
        """RuntimeService 使用的持久化端口，不向 CLI 暴露 SQLite 连接。"""
        return self.router.storage

    def current_session(self):
        """返回当前 Session；仅作为 CLI 迁移期的显式 Service API。"""
        return self.router.current

    def current_model(self):
        """返回当前 Session 的模型适配器，供只读 CLI 命令展示。"""
        session = self.current_session()
        return getattr(session, "llm", None) if session is not None else None

    def session_model(self, session_id: str | None = None):
        """返回指定 Thread 的当前模型适配器，供只读 CLI 命令展示。"""
        target = session_id or self.current_id
        if not target:
            return None
        if target != self.current_id and not self.router.switch(target):
            return None
        session = self.router.current
        return getattr(session, "llm", None) if session is not None else None

    def set_loop_handler(self, handler) -> None:
        """注入 Loop 命令适配器；前端通过 handle_* 方法调用。"""
        self.router.loop_handler = handler

    def handle_goal_command(self, command: str) -> str | None:
        handler = self.router.loop_handler
        if handler is None:
            return "Loop 功能尚未初始化。"
        return handler.handle_goal_command(command)

    def handle_loop_command(self, command: str) -> str | None:
        handler = self.router.loop_handler
        if handler is None:
            return "Loop 功能尚未初始化。"
        return handler.handle_loop_command(command)

    def loop_session(self):
        """返回 Loop 适配器需要的当前 Session。"""
        return self.current_session()

    def thread_record(self, thread_id: str | None = None) -> dict | None:
        return self.router.session_record(thread_id)

    def storage_list_runtime_events(self, thread_id: str, *, after_sequence: int = 0):
        return self._protocol_storage().list_runtime_events(
            thread_id, after_sequence=after_sequence,
        )

    def storage_next_runtime_sequence(self, thread_id: str) -> int:
        return self._protocol_storage().next_runtime_sequence(thread_id)

    def storage_append_runtime_event(self, event: EventEnvelope) -> None:
        self._protocol_storage().append_runtime_event(event)

    def storage_save_runtime_turn(self, turn: TurnRecord) -> None:
        self._protocol_storage().save_runtime_turn(turn)

    def storage_append_runtime_item(self, item: TurnItem) -> None:
        self._protocol_storage().append_runtime_item(item)


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

    def submit_turn(
        self,
        thread_id: str,
        input_text: str,
        *,
        images: list[Any] | None = None,
        resume: bool = False,
        turn_id: str | None = None,
    ) -> str:
        """异步提交 Turn，立即返回 turn_id；结果和进度通过事件流获取。"""
        with self._lock:
            self._ensure_open()
            record = self.router.session_record(thread_id) if hasattr(self.router, "session_record") else None
            if hasattr(self.router, "storage") and record is None:
                raise KeyError(f"找不到 session: {thread_id}")
            if thread_id in self._session_runs:
                raise RuntimeError(f"session {thread_id} 已有 run 正在执行")
        actual_turn_id = turn_id or f"run-{uuid.uuid4().hex}"
        try:
            self._submissions.submit(TurnSubmission(
                turn_id=actual_turn_id,
                thread_id=thread_id,
                input_text=input_text,
                images=tuple(images or ()),
                resume=resume,
            ))
        except SubmissionQueueOverloaded as exc:
            failure = RuntimeFailure(
                code="server_overloaded",
                category="runtime",
                message=str(exc),
                retryable=True,
                user_action_required=False,
            )
            self.publish(
                "run_failed",
                session_id=thread_id,
                run_id=actual_turn_id,
                payload={"error": failure.to_dict()},
            )
            raise
        return actual_turn_id

    def start_turn(
        self,
        thread_id: str,
        input_text: str,
        *,
        images: list[Any] | None = None,
        resume: bool = False,
        turn_id: str | None = None,
    ) -> str:
        """协议命令入口：把 Turn 放入 Submission Queue 后立即返回。"""
        return self.submit_turn(
            thread_id,
            input_text,
            images=images,
            resume=resume,
            turn_id=turn_id,
        )

    def interrupt_turn(self, turn_id: str) -> bool:
        """协议命令入口：请求取消一个正在执行的 Turn。"""
        return self.cancel_run(turn_id)

    def resume_turn(
        self,
        thread_id: str,
        input_text: str = "继续上一轮任务",
        *,
        turn_id: str | None = None,
    ) -> str:
        """协议命令入口：以 resume 模式重新提交一个 Thread。"""
        return self.submit_turn(
            thread_id,
            input_text,
            resume=True,
            turn_id=turn_id,
        )

    def steer_turn(self, turn_id: str, input_text: str) -> bool:
        """把用户 steering 注入当前历史，供当前 Turn 的下一次模型请求读取。"""
        if not input_text.strip():
            raise ValueError("steer input must not be empty")
        with self._lock:
            self._ensure_open()
            active = self._runs.get(turn_id)
            if active is None:
                return False
            session = self.router.switch(active.session_id) and self.router.current
            if session is None:
                return False
            session.messages.append({"role": "user", "content": input_text})
        self.publish(
            "turn_steered",
            session_id=active.session_id,
            run_id=turn_id,
            payload={"text": input_text},
        )
        return True

    def answer_request(self, request_id: str, response: Any) -> bool:
        """回应异步审批请求；response 可为 approve/deny 或结构化对象。"""
        if isinstance(response, str):
            decision = response
            feedback = None
        elif isinstance(response, dict):
            decision = response.get("decision") or response.get("action")
            feedback = response.get("feedback")
        else:
            raise ValueError("response must be a decision string or object")
        if decision not in ("approve", "deny"):
            raise ValueError("decision must be approve or deny")
        return self.approval_broker.resolve(
            request_id, decision, feedback=str(feedback) if feedback else None,
        )

    def _run_submission(self, submission: TurnSubmission) -> None:
        try:
            self.send_message(
                submission.input_text,
                images=list(submission.images),
                session_id=submission.thread_id,
                resume=submission.resume,
                run_id=submission.turn_id,
            )
        except BaseException:
            # send_message 已发布失败事件；worker 不能因单个 Turn 退出。
            pass

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
                on_turn=lambda event: self._publish_runtime_item(
                    event, thread_id=target_id, turn_id=actual_run_id,
                    source="turn_event",
                ),
                on_run=lambda event: self._publish_runtime_item(
                    event, thread_id=target_id, turn_id=actual_run_id,
                    source="agent_run_event",
                ),
            )

        self.publish("run_started", session_id=target_id, run_id=actual_run_id)
        turn = TurnRecord(
            turn_id=actual_run_id,
            thread_id=target_id,
            status="running",
            input_text=message,
        )
        storage = self._protocol_storage()
        if storage is not None:
            storage.save_runtime_turn(turn)
        self._publish_item(
            target_id,
            lambda sequence: TurnItem.create(
                item_id=f"item-{uuid.uuid4().hex}",
                thread_id=target_id,
                turn_id=actual_run_id,
                sequence=sequence,
                kind="user.message",
                status="completed",
                payload={"text": message, "image_count": len(images or [])},
            ),
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
            subscriptions = list(self._queue_subscribers.values())
            self._queue_subscribers.clear()
            self._clients.clear()
        for subscription in subscriptions:
            subscription.close()
        for active in active_runs:
            active.cancel.cancel()
        self._submissions.close()
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
