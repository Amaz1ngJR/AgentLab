"""Runtime 握手、有界订阅和 TurnItem 映射测试。"""
from types import SimpleNamespace

import pytest

from app.agent.service import RuntimeService
from app.protocol.handshake import ClientInfo
from app.protocol.mapping import runtime_event_to_item
from app.protocol.subscription import EventQueueOverloaded, EventSubscription


class _Router:
    current_id = "s1"
    current = None

    def list_sessions(self):
        return []

    def list_profiles(self):
        return {}

    def close_all(self):
        pass


def test_initialize_negotiates_known_capabilities():
    service = RuntimeService(_Router())
    result = service.initialize_client(ClientInfo.create(
        "test", "1.0", ["event_replay", "images", "unknown"],
    ))
    assert result.protocol_version == 1
    assert result.accepted_capabilities == ("event_replay", "images")
    assert "turn_items" in result.server_capabilities


def test_subscription_requires_initialized_client():
    service = RuntimeService(_Router())
    with pytest.raises(RuntimeError, match="not initialized"):
        service.open_event_subscription("missing")


def test_event_subscription_is_bounded_and_fails_slow_consumer():
    from app.protocol.envelopes import EventEnvelope

    subscription = EventSubscription("client-1", max_queue_size=1)
    subscription.put(EventEnvelope.create(
        sequence=1, thread_id="s1", kind="turn.started",
    ))
    with pytest.raises(EventQueueOverloaded):
        subscription.put(EventEnvelope.create(
            sequence=2, thread_id="s1", kind="turn.completed",
        ))
    assert subscription.overflowed
    assert subscription.drain()[0].sequence == 1


def test_run_event_maps_to_structured_item():
    event = SimpleNamespace(
        kind="tool_completed",
        task_id="t1",
        tool_name="shell",
        tool_input={"command": "pwd"},
        tool_output="ok",
        tool_error=False,
        elapsed_seconds=0.1,
        text="",
        task_content="",
        task_status="",
        payload={},
    )
    item = runtime_event_to_item(
        event, thread_id="s1", turn_id="r1", sequence=3,
    )
    assert item.kind == "tool.result"
    assert item.status == "completed"
    assert item.payload["tool_name"] == "shell"
    assert item.sequence == 3
