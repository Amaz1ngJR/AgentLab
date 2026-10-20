"""RuntimeService budget exhaustion through real sessions and SQLite."""
from app.agent.profiles import AgentProfile
from app.agent.runtime import AgentSession
from app.agent.service import RuntimeService
from app.agent.session_router import SessionRouter
from app.storage import Storage
from tests.support.runtime import FakeRouter, _echo_tool, _registry_with, _resp_text, _resp_tool


def test_direct_budget_exhaustion_is_visible_persisted_and_resumable(tmp_path):
    llm = FakeRouter([_resp_tool("c1", "echo", {"msg": "hello"}), _resp_text("done")])
    storage = Storage(tmp_path / "db")
    router = SessionRouter(
        storage,
        lambda profile, sid: AgentSession(llm, _registry_with(_echo_tool()), max_steps=1),
        {"default": AgentProfile("default", "Default", "fake")},
    )
    sid = router.new("default")
    service = RuntimeService(router)
    events = []
    service.subscribe_protocol(events.append)
    try:
        answer = service.send_message("继续", run_id="limited")
        assert "最大步数 1" in answer
        row = storage.conn.execute(
            "SELECT status FROM runtime_turns WHERE turn_id='limited'",
        ).fetchone()
        assert row[0] == "suspended"
        assert events[-1].kind == "turn.suspended"
        assert events[-1].payload == {
            "result": answer, "reason": "budget_exhausted", "resumable": True,
        }
        assert not any(event.kind == "turn.completed" for event in events)
        assert any(answer in str(event.payload) for event in events if event.kind.startswith("item."))
        assert service.active_runs() == []
        assert service.send_message("继续", run_id="resumed") == "done"
        assert events[-1].kind == "turn.completed"
    finally:
        service.close()
        storage.close()
