"""Runtime Protocol v1 与 append-only Event/Item 存储测试。"""
from datetime import datetime, timezone

import pytest

from app.agent.service import RuntimeService
from app.protocol.envelopes import EventEnvelope
from app.protocol.errors import RuntimeFailure
from app.protocol.items import TurnItem
from app.protocol.models import TurnRecord
from app.storage import Storage


def test_protocol_envelope_requires_namespaced_kind_and_json_payload():
    event = EventEnvelope.create(
        sequence=1,
        thread_id="s1",
        turn_id="r1",
        kind="turn.started",
        payload={"path": __file__},
    )
    assert event.schema_version == 1
    assert event.to_dict()["payload"]["path"] == __file__
    with pytest.raises(ValueError):
        EventEnvelope.create(sequence=1, thread_id="s1", kind="bad", payload={})


def test_runtime_failure_is_structured_and_serializable():
    failure = RuntimeFailure(
        code="provider_overloaded",
        category="provider",
        message="503",
        retryable=True,
        retry_after_seconds=2.0,
        details={"status": 503},
    )
    assert failure.to_dict()["retryable"] is True
    assert failure.to_dict()["details"]["status"] == 503


def test_storage_round_trips_turn_items_and_events(tmp_path):
    storage = Storage(tmp_path / "db")
    storage.create_session("s1", "default", "fake", "test")
    turn = TurnRecord(turn_id="r1", thread_id="s1", status="running", input_text="hello")
    storage.save_runtime_turn(turn)
    item = TurnItem.create(
        item_id="i1", thread_id="s1", turn_id="r1", sequence=1,
        kind="user.message", status="completed", payload={"text": "hello"},
    )
    storage.append_runtime_item(item)
    event = EventEnvelope.create(
        sequence=1, thread_id="s1", turn_id="r1", item_id="i1",
        kind="item.completed", payload={"item": item.to_dict()},
    )
    storage.append_runtime_event(event)
    assert storage.get_runtime_turn("r1")["status"] == "running"
    assert storage.list_runtime_items("r1")[0]["payload"]["text"] == "hello"
    assert storage.list_runtime_events("s1")[0]["kind"] == "item.completed"
    assert storage.next_runtime_sequence("s1") == 2


def test_runtime_service_publishes_and_replays_protocol_events(tmp_path):
    from app.agent.profiles import AgentProfile
    from app.agent.runtime import AgentSession
    from app.models.protocol import ModelResponse
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
    profiles = {"default": AgentProfile("default", "Default", "fake")}
    def factory(profile, session_id):
        return AgentSession(LLM(), ToolRegistry(), system_prompt="test")
    from app.agent.session_router import SessionRouter
    router = SessionRouter(storage, factory, profiles)
    sid = router.new("default")
    service = RuntimeService(router)
    seen = []
    service.subscribe_protocol(seen.append)
    service.send_message("hello")
    assert seen
    assert seen[0].sequence == 1
    assert all(event.thread_id == sid for event in seen)
    replayed = service.replay_events(sid)
    assert [event.sequence for event in replayed] == [event.sequence for event in seen]
