"""RuntimeService budget exhaustion through real sessions and SQLite."""
from app.agent.profiles import AgentProfile
from app.agent.runtime import AgentSession
from app.agent.service import RuntimeService
from app.agent.session_router import SessionRouter
from app.agent.tasks import Task
from app.storage import Storage
from tests.support.runtime import FakeRouter, _echo_tool, _registry_with, _resp_text, _resp_tool


def test_direct_budget_exhaustion_is_visible_persisted_and_resumable(tmp_path):
    final = _resp_text("done")
    final.actual_model = "served-model"
    final.usage.update(cache_read_input_tokens=0, reasoning_tokens=2)
    llm = FakeRouter([_resp_tool("c1", "echo", {"msg": "hello"}), final])
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
        assert storage.get_runtime_turn("resumed")["usage_details"] == {
            "provider": "fake", "requested_model": "fake-model", "actual_model": "served-model",
            "cache_read_input_tokens": 0, "reasoning_tokens": 2,
        }
    finally:
        service.close()
        storage.close()


def test_blocked_turn_keeps_answer_and_reason_without_reporting_completion(tmp_path):
    llm = FakeRouter([_resp_text("找到日志证据")])
    storage = Storage(tmp_path / "db")
    router = SessionRouter(
        storage,
        lambda profile, sid: AgentSession(llm, _registry_with()),
        {"default": AgentProfile("default", "Default", "fake")},
    )
    router.new("default")
    router.current.task_store.add(Task(id="remaining", content="等待交付"))
    service = RuntimeService(router)
    events = []
    service.subscribe_protocol(events.append)
    try:
        answer = service.send_message("看下历史日志", run_id="blocked")
        assert "找到日志证据" in answer
        turn = storage.get_runtime_turn("blocked")
        assert turn["status"] == "blocked"
        assert turn["input_tokens"] == 5
        assert events[-1].kind == "turn.blocked"
        assert events[-1].payload == {"result": answer, "reason": "任务清单尚未完成：等待交付"}
        assert not any(event.kind == "turn.completed" for event in events)
    finally:
        service.close()
        storage.close()
