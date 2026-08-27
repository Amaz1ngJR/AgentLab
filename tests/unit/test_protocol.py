"""Runtime Protocol v1 与 append-only Event/Item 存储测试。"""
import sqlite3
import threading
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


def test_allocate_runtime_sequence_never_hands_out_the_same_value(tmp_path):
    storage = Storage(tmp_path / "db")
    storage.create_session("s1", "default", "fake", "test")
    allocated = [storage.allocate_runtime_sequence("s1") for _ in range(5)]
    assert allocated == [1, 2, 3, 4, 5]
    # 计数器持久化，新的 Storage 实例(等价于重启)不会重发已用过的序号。
    assert Storage(tmp_path / "db").allocate_runtime_sequence("s1") == 6


def test_allocate_runtime_sequence_is_serialized_across_threads(tmp_path):
    storage = Storage(tmp_path / "db")
    storage.create_session("s1", "default", "fake", "test")
    allocated: list[int] = []
    lock = threading.Lock()

    def worker():
        for _ in range(20):
            value = storage.allocate_runtime_sequence("s1")
            with lock:
                allocated.append(value)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(allocated) == list(range(1, 81))


def test_allocate_runtime_sequence_respects_manually_written_rows(tmp_path):
    """直接 append 指定 sequence 的路径也要参与取值，避免撞 UNIQUE 约束。"""
    storage = Storage(tmp_path / "db")
    storage.create_session("s1", "default", "fake", "test")
    turn = TurnRecord(turn_id="r1", thread_id="s1", status="running", input_text="hi")
    storage.save_runtime_turn(turn)
    storage.append_runtime_item(TurnItem.create(
        item_id="i9", thread_id="s1", turn_id="r1", sequence=9,
        kind="user.message", status="completed",
    ))
    assert storage.next_runtime_sequence("s1") == 10
    assert storage.allocate_runtime_sequence("s1") == 10


def test_item_and_event_share_sequence_and_commit_together(tmp_path):
    storage = Storage(tmp_path / "db")
    storage.create_session("s1", "default", "fake", "test")
    storage.save_runtime_turn(
        TurnRecord(turn_id="r1", thread_id="s1", status="running", input_text="hi"),
    )
    sequence = storage.allocate_runtime_sequence("s1")
    item = TurnItem.create(
        item_id="i1", thread_id="s1", turn_id="r1", sequence=sequence,
        kind="user.message", status="completed", payload={"text": "hi"},
    )
    event = EventEnvelope.create(
        sequence=sequence, thread_id="s1", turn_id="r1", item_id="i1",
        kind="item.completed", payload={"item": item.to_dict()},
    )
    storage.append_runtime_item_with_event(item, event)
    stored_item = storage.list_runtime_items("r1")[0]
    stored_event = storage.list_runtime_events("s1")[0]
    assert stored_item["sequence"] == stored_event["sequence"] == sequence
    assert stored_event["item_id"] == stored_item["item_id"]


def test_append_item_with_event_rejects_drifted_sequence(tmp_path):
    storage = Storage(tmp_path / "db")
    storage.create_session("s1", "default", "fake", "test")
    item = TurnItem.create(
        item_id="i1", thread_id="s1", turn_id="r1", sequence=1,
        kind="user.message", status="completed",
    )
    event = EventEnvelope.create(
        sequence=2, thread_id="s1", turn_id="r1", item_id="i1", kind="item.completed",
    )
    with pytest.raises(ValueError, match="same sequence"):
        storage.append_runtime_item_with_event(item, event)


def test_append_item_with_event_rolls_back_both_rows_on_failure(tmp_path):
    """事件写失败时 Item 不能留在库里，否则事实流出现半条记录。"""
    storage = Storage(tmp_path / "db")
    storage.create_session("s1", "default", "fake", "test")
    storage.save_runtime_turn(
        TurnRecord(turn_id="r1", thread_id="s1", status="running", input_text="hi"),
    )
    taken = EventEnvelope.create(
        sequence=1, thread_id="s1", turn_id="r1", kind="turn.started",
    )
    storage.append_runtime_event(taken)
    item = TurnItem.create(
        item_id="i1", thread_id="s1", turn_id="r1", sequence=1,
        kind="user.message", status="completed",
    )
    clashing = EventEnvelope.create(
        sequence=1, thread_id="s1", turn_id="r1", item_id="i1", kind="item.completed",
    )
    with pytest.raises(sqlite3.IntegrityError):
        storage.append_runtime_item_with_event(item, clashing)
    assert storage.list_runtime_items("r1") == []
    assert [row["kind"] for row in storage.list_runtime_events("s1")] == ["turn.started"]


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
    # 每个 sequence 只能出现一次，且必须严格递增。
    sequences = [event.sequence for event in replayed]
    assert sequences == sorted(sequences)
    assert len(set(sequences)) == len(sequences)
    # 每个 item 事件都能在 runtime_items 里找到同 sequence 的 Item。
    item_events = [event for event in replayed if event.kind.startswith("item.")]
    assert item_events
    items = {
        row["sequence"]: row
        for row in storage.list_runtime_items(item_events[0].turn_id)
    }
    for event in item_events:
        assert event.sequence in items
        assert items[event.sequence]["item_id"] == event.item_id
