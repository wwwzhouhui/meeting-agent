"""Gate timeouts: scan_gate_timeouts with a fake clock (notify exactly
once per gate entry, honest delivery, reject mode + audit + re-run,
independent per-gate clocks, legacy None backfill), scheduler
registration, and the POST /gate-timeouts/run endpoint."""
import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.api.main import app, build_scheduler
from src.orchestrator.gate_timeouts import scan_gate_timeouts
from src.shared.config import Settings
from src.shared.db import InMemoryDB, store
from src.shared.schema import Meeting, Participant
from src.tools.notify.base import BaseNotifyTool
from src.tools.notify.mock import MockNotifyTool

client = TestClient(app)
NOW = datetime(2026, 10, 26, 9, 0, 0, tzinfo=timezone.utc)  # a Monday
SAMPLE = json.loads(
    (Path(__file__).resolve().parents[1] / "examples" / "sample_meeting.json")
    .read_text(encoding="utf-8"))


class RecordingNotifyTool(BaseNotifyTool):
    system = "recording"

    def __init__(self):
        self.sends = []

    def send(self, to, title, content, **kw):
        self.sends.append({"to": to, "title": title, "content": content})
        return {"mock": False, "delivered": True, "system": self.system, "to": to}


class FailingNotifyTool(BaseNotifyTool):
    system = "failing"

    def send(self, to, title, content, **kw):
        raise RuntimeError("channel down")


def _settings(**overrides) -> Settings:
    base = dict(gate_timeout_hours=48.0, gate_timeout_action="notify")
    base.update(overrides)
    return Settings(**base)


def _meeting(mid, status, minutes_entered=None, tasks_entered=None):
    return Meeting(
        id=mid, title=f"超时测试会{mid}", status=status,
        participants=[Participant(user_id="u-zhangsan", name="张三", role="organizer")],
        minutes_gate_entered_at=minutes_entered,
        tasks_gate_entered_at=tasks_entered)


def _db_with(*meetings):
    db = InMemoryDB()
    for m in meetings:
        db.save(m)
    return db


@pytest.fixture(autouse=True)
def _clean_store():
    store._data.clear()
    yield
    store._data.clear()


# ------------------------------------------------------------- notify mode
def test_not_timed_out_no_action():
    db = _db_with(
        _meeting("g-1", "awaiting_minutes_confirmation",
                 minutes_entered=NOW - timedelta(hours=47)),
        _meeting("g-2", "dispatched",
                 minutes_entered=NOW - timedelta(hours=500)),
    )
    tool = RecordingNotifyTool()
    assert scan_gate_timeouts(db, tool, NOW, _settings()) == []
    assert tool.sends == []


def test_notify_fires_once_with_honest_record():
    db = _db_with(_meeting("g-1", "awaiting_minutes_confirmation",
                           minutes_entered=NOW - timedelta(hours=72)))
    tool = RecordingNotifyTool()
    records = scan_gate_timeouts(db, tool, NOW, _settings())
    assert len(records) == 1
    rec = records[0]
    assert rec["meeting_id"] == "g-1" and rec["gate"] == "minutes"
    assert rec["action"] == "notified" and rec["delivered"] is True
    assert rec["waited_hours"] == pytest.approx(72.0)
    assert len(tool.sends) == 1
    assert tool.sends[0]["to"] == "u-zhangsan"  # organizer
    assert "超时测试会g-1" in tool.sends[0]["content"]
    assert "纪要确认" in tool.sends[0]["content"] and "72.0" in tool.sends[0]["content"]
    # stamped -> a second scan is silent (exactly once per gate entry)
    assert db.get("g-1").minutes_gate_timeout_notified_at == NOW
    assert scan_gate_timeouts(db, tool, NOW, _settings()) == []
    assert len(tool.sends) == 1


def test_notify_mock_reports_not_delivered():
    db = _db_with(_meeting("g-1", "awaiting_tasks_confirmation",
                           tasks_entered=NOW - timedelta(hours=50)))
    records = scan_gate_timeouts(db, MockNotifyTool(), NOW, _settings())
    assert len(records) == 1
    assert records[0]["action"] == "notified"
    assert records[0]["delivered"] is False  # mock must not pretend delivery
    assert "mock" in records[0]["delivery_note"]
    assert db.get("g-1").tasks_gate_timeout_notified_at == NOW  # mock sends still stamp


def test_notify_failure_leaves_unstamped_and_retries():
    db = _db_with(_meeting("g-1", "awaiting_minutes_confirmation",
                           minutes_entered=NOW - timedelta(hours=72)))
    records = scan_gate_timeouts(db, FailingNotifyTool(), NOW, _settings())
    assert records[0]["action"] == "notify_failed"
    assert records[0]["delivered"] is False
    assert db.get("g-1").minutes_gate_timeout_notified_at is None
    tool = RecordingNotifyTool()
    records = scan_gate_timeouts(db, tool, NOW, _settings())
    assert records[0]["action"] == "notified" and len(tool.sends) == 1


def test_invalid_action_falls_back_to_notify():
    db = _db_with(_meeting("g-1", "awaiting_minutes_confirmation",
                           minutes_entered=NOW - timedelta(hours=72)))
    records = scan_gate_timeouts(db, RecordingNotifyTool(), NOW,
                                 _settings(gate_timeout_action="explode"))
    assert records[0]["action"] == "notified"
    assert db.get("g-1").status == "awaiting_minutes_confirmation"


# ------------------------------------------------------------- reject mode
def test_reject_mode_transitions_with_audit_trail():
    m = _meeting("g-1", "awaiting_minutes_confirmation",
                 minutes_entered=NOW - timedelta(hours=72))
    m.minutes_confirmed = True  # must be reset by the timeout reject
    db = _db_with(m)
    records = scan_gate_timeouts(db, RecordingNotifyTool(), NOW,
                                 _settings(gate_timeout_action="reject"))
    assert len(records) == 1
    assert records[0]["action"] == "rejected" and records[0]["gate"] == "minutes"
    saved = db.get("g-1")
    assert saved.status == "minutes_rejected"  # existing state, nothing invented
    assert saved.minutes_confirmed is False
    assert len(saved.rejections) == 1
    rej = saved.rejections[0]
    assert rej.gate == "minutes" and rej.at == NOW
    assert "confirmation timeout" in rej.reason and "72.0" in rej.reason
    # rejected meetings are out of the scanner's scope afterwards
    assert scan_gate_timeouts(db, RecordingNotifyTool(), NOW,
                              _settings(gate_timeout_action="reject")) == []


def test_reject_mode_tasks_gate():
    db = _db_with(_meeting("g-1", "awaiting_tasks_confirmation",
                           tasks_entered=NOW - timedelta(hours=49)))
    records = scan_gate_timeouts(db, RecordingNotifyTool(), NOW,
                                 _settings(gate_timeout_action="reject"))
    assert records[0]["gate"] == "tasks"
    saved = db.get("g-1")
    assert saved.status == "tasks_rejected"
    assert saved.rejections[0].gate == "tasks"


# ------------------------------------------------------- independent clocks
def test_gates_have_independent_clocks():
    db = _db_with(
        # tasks gate entered 1h ago; the stale minutes stamp from an
        # earlier phase must NOT expire the tasks gate
        _meeting("g-tasks", "awaiting_tasks_confirmation",
                 minutes_entered=NOW - timedelta(hours=500),
                 tasks_entered=NOW - timedelta(hours=1)),
        _meeting("g-min", "awaiting_minutes_confirmation",
                 minutes_entered=NOW - timedelta(hours=100)),
    )
    tool = RecordingNotifyTool()
    records = scan_gate_timeouts(db, tool, NOW, _settings())
    assert [(r["meeting_id"], r["gate"]) for r in records] == [("g-min", "minutes")]
    later = NOW + timedelta(hours=48)
    records = scan_gate_timeouts(db, tool, later, _settings())
    assert [(r["meeting_id"], r["gate"]) for r in records] == [("g-tasks", "tasks")]


# ------------------------------------------------------------- legacy None
def test_legacy_none_is_backfilled_not_killed():
    db = _db_with(_meeting("g-old", "awaiting_minutes_confirmation",
                           minutes_entered=None))
    tool = RecordingNotifyTool()
    # even with a hair-trigger timeout, first sight only backfills
    records = scan_gate_timeouts(db, tool, NOW, _settings(gate_timeout_hours=0.01))
    assert [r["action"] for r in records] == ["backfilled"]
    assert tool.sends == []
    assert db.get("g-old").minutes_gate_entered_at == NOW
    # the clock runs from the backfill, not from the dawn of time
    assert scan_gate_timeouts(db, tool, NOW, _settings(gate_timeout_hours=0.01)) == []
    records = scan_gate_timeouts(db, tool, NOW + timedelta(hours=1),
                                 _settings(gate_timeout_hours=0.01))
    assert [r["action"] for r in records] == ["notified"]


# ------------------------------------------------------------- schema round trip
def test_new_gate_fields_survive_json_round_trip():
    m = _meeting("g-ser", "awaiting_minutes_confirmation",
                 minutes_entered=NOW - timedelta(hours=3))
    m.minutes_gate_timeout_notified_at = NOW
    back = Meeting.model_validate_json(m.model_dump_json())
    assert back.model_dump() == m.model_dump()


# ------------------------------------------------------------------- API
def test_api_gate_timeouts_run_returns_action_list():
    m = _meeting("g-api-1", "awaiting_minutes_confirmation",
                 minutes_entered=datetime.now(timezone.utc) - timedelta(hours=72))
    store.save(m)
    r = client.post("/gate-timeouts/run")
    assert r.status_code == 200
    actions = r.json()["actions"]
    assert len(actions) == 1
    rec = actions[0]
    assert rec["meeting_id"] == "g-api-1" and rec["gate"] == "minutes"
    assert rec["action"] == "notified"
    assert rec["delivered"] is False  # default NOTIFY_PROVIDER=mock, honestly reported
    assert store.get("g-api-1").minutes_gate_timeout_notified_at is not None
    assert client.post("/gate-timeouts/run").json()["actions"] == []


def test_api_timeout_reject_then_process_reruns():
    payload = copy.deepcopy(SAMPLE)
    payload["id"] = "g-api-rej"
    assert client.post("/meetings", json=payload).status_code == 200
    r = client.post("/meetings/g-api-rej/process", json={})
    assert r.json()["status"] == "awaiting_minutes_confirmation"
    # age the gate entry, then let the scanner (reject mode) expire it
    m = store.get("g-api-rej")
    m.minutes_gate_entered_at = datetime.now(timezone.utc) - timedelta(hours=72)
    store.save(m)
    records = scan_gate_timeouts(store, RecordingNotifyTool(),
                                 datetime.now(timezone.utc),
                                 _settings(gate_timeout_action="reject"))
    assert records and records[0]["action"] == "rejected"
    assert store.get("g-api-rej").status == "minutes_rejected"
    # existing re-run path works and re-entering the gate restarts the clock
    r = client.post("/meetings/g-api-rej/process", json={})
    body = r.json()
    assert body["status"] == "awaiting_minutes_confirmation"
    entered = datetime.fromisoformat(body["minutes_gate_entered_at"].replace("Z", "+00:00"))
    assert datetime.now(timezone.utc) - entered < timedelta(minutes=5)
    assert body["minutes_gate_timeout_notified_at"] is None


# --------------------------------------------------------------- scheduler
def test_scheduler_registration_follows_flags():
    assert build_scheduler(Settings(reminders_enabled=False,
                                    gate_timeouts_enabled=False,
                                    weekly_summary_enabled=False)) is None
    sched = build_scheduler(Settings(gate_timeouts_enabled=True))
    assert [j.id for j in sched.get_jobs()] == ["gate-timeout-scan"]
    sched = build_scheduler(Settings(reminders_enabled=True))
    assert [j.id for j in sched.get_jobs()] == ["reminder-scan"]
    sched = build_scheduler(Settings(reminders_enabled=True,
                                     gate_timeouts_enabled=True))
    assert sorted(j.id for j in sched.get_jobs()) == ["gate-timeout-scan", "reminder-scan"]
