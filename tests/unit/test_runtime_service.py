"""RuntimeService 的 session、run、取消、事件与资源生命周期测试。"""
import threading
import time

import pytest

from app.agent.cancel import CancelToken
from app.agent.service import RuntimeService
from app.protocol.handshake import ClientInfo


class _Session:
    def __init__(self, gate=None, *, supports_vision=False):
        self.gate = gate
        self.messages = []
        self.closed = False
        self.cancel_seen = False
        self.llm = type("LLM", (), {"supports_vision": supports_vision})()

    def subscribe_events(self, *, on_turn=None, on_run=None):
        self.on_turn = on_turn
        self.on_run = on_run

        def unsubscribe():
            self.on_turn = None
            self.on_run = None

        return unsubscribe

    def chat(self, message, *, cancel, resume=False, images=None):
        self.messages.append({"role": "user", "content": message})
        if getattr(self, "on_turn", None):
            self.on_turn({"kind": "text", "text": message})
        if self.gate is not None:
            while not self.gate.is_set():
                if cancel.cancelled:
                    self.cancel_seen = True
                    return "cancelled"
                time.sleep(0.001)
        return f"ok:{message}:{resume}"

    def close(self):
        self.closed = True


class _Router:
    def __init__(self, sessions):
        self._sessions = sessions
        self.current_id = next(iter(sessions), None)
        self.persisted = []
        self.switched_models = []
        self.closed = False

    @property
    def current(self):
        return self._sessions.get(self.current_id)

    def new(self, agent_id=None, title=""):
        sid = f"s{len(self._sessions) + 1}"
        self._sessions[sid] = _Session()
        self.current_id = sid
        return sid

    def switch(self, session_id):
        if session_id not in self._sessions:
            return False
        self.current_id = session_id
        return True

    def switch_model(self, model_profile):
        self.switched_models.append(model_profile)
        return "old", model_profile

    def resume_or_new(self, agent_id=None):
        if self.current_id:
            return self.current_id, True
        return self.new(agent_id), False

    def list_sessions(self):
        return [{"id": sid} for sid in self._sessions]

    def list_profiles(self):
        return {}

    def handle_command(self, command):
        return command

    def persist(self, session_id):
        self.persisted.append(session_id)

    def close_all(self):
        self.closed = True
        for session in self._sessions.values():
            session.close()


def test_send_message_persists_target_and_publishes_events():
    router = _Router({"s1": _Session()})
    service = RuntimeService(router)
    events = []
    service.subscribe(events.append)

    assert service.send_message("hello", run_id="run-1") == "ok:hello:False"
    assert router.persisted == ["s1"]
    assert [event.kind for event in events] == [
        "run_started", "turn_event", "run_completed",
    ]
    assert all(event.run_id == "run-1" for event in events)


def test_images_require_vision_capability():
    service = RuntimeService(_Router({"s1": _Session()}))
    with pytest.raises(RuntimeError, match="vision"):
        service.send_message("看图", images=[object()])


def test_switch_model_uses_runtime_guard_and_publishes_event():
    router = _Router({"s1": _Session()})
    service = RuntimeService(router)
    events = []
    service.subscribe(events.append)

    assert service.switch_model("local") == ("old", "local")
    assert router.switched_models == ["local"]
    assert events[-1].kind == "session_model_switched"

    service._session_runs["s1"] = "run-active"
    with pytest.raises(RuntimeError, match="正在执行"):
        service.switch_model("other")


def test_images_forwarded_to_vision_session():
    session = _Session(supports_vision=True)
    captured = {}

    def chat(message, **kwargs):
        captured.update(kwargs)
        return "ok"

    session.chat = chat
    service = RuntimeService(_Router({"s1": session}))
    image = object()
    assert service.send_message("看图", images=[image]) == "ok"
    assert captured["images"] == [image]


    gate = threading.Event()
    router = _Router({"s1": _Session(gate)})
    service = RuntimeService(router)
    worker = threading.Thread(target=lambda: service.send_message("first", run_id="r1"))
    worker.start()
    while "r1" not in service.active_runs():
        time.sleep(0.001)

    with pytest.raises(RuntimeError, match="已有 run"):
        service.send_message("second")
    gate.set()
    worker.join(1)


def test_cancel_run_reaches_session_token():
    gate = threading.Event()
    session = _Session(gate)
    service = RuntimeService(_Router({"s1": session}))
    result = []
    worker = threading.Thread(
        target=lambda: result.append(service.send_message("wait", run_id="r1"))
    )
    worker.start()
    while "r1" not in service.active_runs():
        time.sleep(0.001)

    assert service.cancel_run("r1")
    worker.join(1)
    assert result == ["cancelled"]
    assert session.cancel_seen
    assert service.active_runs() == []


def test_different_sessions_can_run_concurrently_and_persist_correct_ids():
    gates = {"s1": threading.Event(), "s2": threading.Event()}
    router = _Router({sid: _Session(gate) for sid, gate in gates.items()})
    service = RuntimeService(router)
    workers = [
        threading.Thread(
            target=lambda sid=sid: service.send_message(sid, session_id=sid, run_id=f"r-{sid}")
        )
        for sid in gates
    ]
    for worker in workers:
        worker.start()
    while len(service.active_runs()) < 2:
        time.sleep(0.001)
    for gate in gates.values():
        gate.set()
    for worker in workers:
        worker.join(1)
    assert sorted(router.persisted) == ["s1", "s2"]


def test_submit_turn_returns_immediately_and_publishes_completion():
    gate = threading.Event()
    service = RuntimeService(_Router({"s1": _Session(gate)}))
    events = []
    completed = threading.Event()

    def capture(event):
        events.append(event)
        if event.kind == "run_completed":
            completed.set()

    service.subscribe(capture)
    started = time.monotonic()
    turn_id = service.submit_turn("s1", "async hello", turn_id="async-1")
    assert time.monotonic() - started < 0.2
    assert turn_id == "async-1"
    while turn_id not in service.active_runs():
        time.sleep(0.001)
    gate.set()
    assert completed.wait(1)
    assert [event.kind for event in events][-1] == "run_completed"
    service.close()


def test_runtime_service_protocol_commands_cover_submission_and_event_queue():
    gate = threading.Event()
    service = RuntimeService(_Router({"s1": _Session(gate)}))
    client = service.initialize_client(ClientInfo.create("test", "1", ["jsonl"]))
    subscription = service.open_event_subscription(client.client_id, thread_id="s1")
    turn_id = service.start_turn("s1", "hello", turn_id="turn-1")
    assert turn_id == "turn-1"
    while "turn-1" not in service.active_runs():
        time.sleep(0.001)
    assert service.interrupt_turn("turn-1")
    gate.set()
    assert subscription.get(timeout=1).kind == "turn.started"
    service.close()

    class FailingSession(_Session):
        def chat(self, message, **kwargs):
            if message == "fail":
                raise RuntimeError("boom")
            return super().chat(message, **kwargs)

    session = FailingSession()
    service = RuntimeService(_Router({"s1": session}))
    completed = threading.Event()
    service.subscribe(lambda event: completed.set() if event.kind == "run_completed" else None)
    service.submit_turn("s1", "fail", turn_id="bad")
    deadline = time.time() + 1
    while service.active_runs() and time.time() < deadline:
        time.sleep(0.001)
    service.submit_turn("s1", "ok", turn_id="good")
    assert completed.wait(1)
    service.close()


def test_service_exposes_thread_summary_without_cli_storage_access(tmp_path):
    from app.agent.profiles import AgentProfile
    from app.agent.runtime import AgentSession
    from app.agent.session_router import SessionRouter
    from app.models.protocol import ModelResponse
    from app.storage import Storage
    from app.tools.registry import ToolRegistry

    class LLM:
        model = "fake"
        provider = "fake"
        supports_vision = False
        def create_message(self, messages, **kwargs):
            return ModelResponse(text="ok", tool_calls=[], usage={}, provider_payload=[])
        def format_tool_results(self, results):
            return []

    storage = Storage(tmp_path / "db")
    router = SessionRouter(
        storage,
        lambda profile, sid: AgentSession(LLM(), ToolRegistry()),
        {"default": AgentProfile("default", "Default", "fake")},
    )
    sid = router.new("default", "My Thread")
    router.current.messages.append({"role": "user", "content": "hello"})
    summary = RuntimeService(router).current_thread_summary()
    assert summary == {
        "thread_id": sid,
        "title": "My Thread",
        "agent_id": "default",
        "model_profile": "fake",
        "message_count": 1,
    }


    session = _Session()
    router = _Router({"s1": session})
    service = RuntimeService(router)
    service.close()
    service.close()
    assert router.closed
    assert session.closed
    with pytest.raises(RuntimeError, match="closed"):
        service.new_session()
