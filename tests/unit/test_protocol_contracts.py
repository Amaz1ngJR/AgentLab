"""Pure protocol envelope and failure serialization contracts."""
import pytest

from app.protocol.envelopes import EventEnvelope
from app.protocol.errors import RuntimeFailure


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
