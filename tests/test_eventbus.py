"""Event bus: pub/sub semantics, exception isolation, the audit log on
Meeting.events, bus-driven cross-agent handoffs in the flow, and the
API confirm/reject paths publishing through the same bus."""
import copy

from fastapi.testclient import TestClient

from src.api.main import app
from src.orchestrator.eventbus import Event, EventBus
from src.orchestrator.flow import MeetingFlow
from src.shared.db import InMemoryDB

client = TestClient(app)


# ------------------------------------------------------------ bus unit
def test_publish_order_and_wildcard():
    bus = EventBus()
    seen = []
    bus.subscribe("x", lambda e: seen.append("h1"))
    bus.subscribe("x", lambda e: seen.append("h2"))

    @bus.subscribe("*")
    def star(e):
        seen.append("star")

    bus.publish(Event(type="x", meeting_id="m"))
    assert seen == ["h1", "h2", "star"]  # subscription order, wildcard last
    bus.publish(Event(type="other", meeting_id="m"))
    assert seen == ["h1", "h2", "star", "star"]  # wildcard sees everything


def test_handler_exception_is_isolated_and_recorded():
    bus = EventBus()
    seen = []

    def boom(e):
        raise RuntimeError("handler exploded")

    bus.subscribe("x", boom)
    bus.subscribe("x", lambda e: seen.append("survivor"))
    event = bus.publish(Event(type="x", meeting_id="m1"))
    assert seen == ["survivor"]  # the failure did not block the rest
    assert event.type == "x"  # publish returns normally
    assert len(bus.errors) == 1
    assert bus.errors[0]["event_type"] == "x"
    assert "handler exploded" in bus.errors[0]["error"]


def test_emit_appends_audit_record_and_redacts_credentials(sample_meeting):
    bus = EventBus()
    m = copy.deepcopy(sample_meeting)
    event = bus.emit(m, "minutes.generated", {"count": 2, "api_key": "sk-should-not-leak"})
    assert len(m.events) == 1
    record = m.events[0]
    assert record.type == "minutes.generated"
    assert record.at == event.at
    assert record.payload["count"] == 2
    assert record.payload["api_key"] == "***"  # redacted in the log...
    assert event.payload["api_key"] == "***"  # ...and on the bus itself


# ---------------------------------------------------------- flow wiring
def _types(meeting):
    return [e.type for e in meeting.events]


def test_flow_full_run_event_sequence(sample_meeting):
    flow = MeetingFlow()
    m = flow.run(sample_meeting, auto_approve_minutes=True, auto_approve_tasks=True)
    assert m.status == "dispatched"
    types = _types(m)
    expected_order = [
        "meeting.created", "minutes.generated", "minutes.confirmed",
        "tasks.extracted", "tasks.confirmed", "schedule.proposed",
        "tasks.dispatched",
    ]
    positions = [types.index(t) for t in expected_order]
    assert positions == sorted(positions), f"event order wrong: {types}"
    stamps = [e.at for e in m.events]
    assert stamps == sorted(stamps)  # audit log is chronological


def test_flow_stop_at_gate1_events(sample_meeting):
    flow = MeetingFlow()
    m = flow.run(sample_meeting)  # no approvals
    assert m.status == "awaiting_minutes_confirmation"
    types = _types(m)
    assert "gate.minutes.entered" in types
    assert "minutes.confirmed" not in types
    assert "tasks.extracted" not in types  # extraction is bus-driven; no confirm, no extraction


def test_flow_events_have_no_transcript_text(sample_meeting):
    flow = MeetingFlow()
    m = flow.run(sample_meeting, auto_approve_minutes=True, auto_approve_tasks=True)
    secret_sentence = "支付渠道证书11月1日就过期了"
    for e in m.events:
        assert secret_sentence not in str(e.payload), f"payload leaked transcript text: {e}"


def test_events_survive_store_roundtrip(sample_meeting):
    flow = MeetingFlow()
    m = flow.run(sample_meeting)  # stops at gate 1
    db = InMemoryDB()
    db.save(m)
    back = db.get(m.id)
    assert [e.type for e in back.events] == [e.type for e in m.events]
    assert [e.payload for e in back.events] == [e.payload for e in m.events]


# ------------------------------------------------------------ API paths
def _api_meeting(meeting_id, sample_meeting):
    payload = copy.deepcopy(sample_meeting).model_dump(mode="json")
    payload["id"] = meeting_id
    payload["events"] = []
    r = client.post("/meetings", json=payload)
    assert r.status_code == 200, r.text
    return payload


def test_api_confirm_and_reject_publish_events(sample_meeting):
    _api_meeting("bus-evt-confirm", sample_meeting)
    r = client.post("/meetings/bus-evt-confirm/process", json={})
    assert r.status_code == 200, r.text
    r = client.post("/meetings/bus-evt-confirm/confirm-minutes")
    assert r.status_code == 200, r.text
    types = [e["type"] for e in r.json()["events"]]
    # minutes.confirmed was published and the bus handler extracted the
    # tasks (the endpoint itself contains no extraction call anymore).
    assert "minutes.confirmed" in types
    assert "tasks.extracted" in types
    assert "gate.tasks.entered" in types
    assert r.json()["action_items"], "bus handler must have extracted tasks"

    _api_meeting("bus-evt-reject", sample_meeting)
    client.post("/meetings/bus-evt-reject/process", json={})
    r = client.post("/meetings/bus-evt-reject/reject-minutes",
                    json={"reason": "纪要漏了灰度议题"})
    assert r.status_code == 200, r.text
    rejected = [e for e in r.json()["events"] if e["type"] == "minutes.rejected"]
    assert rejected and rejected[0]["payload"]["reason"] == "纪要漏了灰度议题"
