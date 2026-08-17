"""RuntimeService 的 session、run、取消、事件与资源生命周期测试。"""
import threading
import time

import pytest

from app.agent.cancel import CancelToken
from app.agent.service import RuntimeService


class _Session:
    def __init__(self, gate=None):
        self.gate = gate
        self.messages = []
        self.closed = False
        self.cancel_seen = False

    def subscribe_events(self, *, on_turn=None, on_run=None):
        self.on_turn = on_turn
        self.on_run = on_run

        def unsubscribe():
            self.on_turn = None
            self.on_run = None

        return unsubscribe

    def chat(self, message, *, cancel, resume=False):
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


def test_same_session_rejects_concurrent_run():
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


def test_close_is_idempotent_and_releases_router_resources():
    session = _Session()
    router = _Router({"s1": session})
    service = RuntimeService(router)
    service.close()
    service.close()
    assert router.closed
    assert session.closed
    with pytest.raises(RuntimeError, match="closed"):
        service.new_session()
