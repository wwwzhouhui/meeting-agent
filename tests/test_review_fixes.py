"""Round-9 code-review fixes (see AUDIT.md round 9):

1. Critical bus handoffs propagate failures (non-critical stays
   isolated); confirmation flags are set only after the handoff;
   a failed API confirm is a retryable 500 with the warning saved.
2. Tasks step disabled -> status "minutes_confirmed" (flag and status
   agree), confirm-tasks / reject-minutes stay 409, the gate-timeout
   scanner never touches that status.
3. MeetingFlow._known is bounded (512, LRU) and evicts on dispatch.
4. Meeting ids are charset-validated at the model boundary (422 on
   create) and the frontend URL-encodes ids in every path.
5. Orchestration ranges are validated at load (interval_min,
   timeout_hours, cron); the gate-timeout scan refuses non-positive
   thresholds; build_scheduler isolates per-job failures.
"""
import copy
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import src.api.main as main
from src.agents.task_agent.agent import TaskAgent
from src.api.main import app, build_scheduler
from src.orchestrator.eventbus import Event, EventBus
from src.orchestrator.flow import MeetingFlow
from src.orchestrator.gate_timeouts import scan_gate_timeouts
from src.shared.config import Settings
from src.shared.db import InMemoryDB, store
from src.shared.orchestration import (
    OrchestrationError,
    builtin_orchestration,
    load_orchestration,
)
from src.shared.schema import Meeting
from src.tools.notify.mock import MockNotifyTool

client = TestClient(app)
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _clean_store():
    store._data.clear()
    yield
    store._data.clear()


# ============================================================ fix 1: bus
def test_critical_handler_failure_propagates_and_is_recorded():
    bus = EventBus()
    seen = []
    bus.subscribe("x", lambda e: seen.append("before"))

    def boom(e):
        raise RuntimeError("handoff exploded")

    bus.subscribe("x", boom, critical=True)
    bus.subscribe("x", lambda e: seen.append("after"))
    with pytest.raises(RuntimeError, match="handoff exploded"):
        bus.publish(Event(type="x", meeting_id="m1"))
    assert seen == ["before"]  # delivery stops at the critical failure
    assert len(bus.errors) == 1
    assert bus.errors[0]["critical"] is True
    assert "handoff exploded" in bus.errors[0]["error"]


def test_noncritical_failure_stays_isolated_next_to_critical():
    bus = EventBus()
    seen = []

    def boom(e):
        raise RuntimeError("observer exploded")

    bus.subscribe("x", boom)  # non-critical
    bus.subscribe("x", lambda e: seen.append("critical-ok"), critical=True)
    event = bus.publish(Event(type="x", meeting_id="m1"))  # must not raise
    assert event.type == "x"
    assert seen == ["critical-ok"]
    assert len(bus.errors) == 1 and bus.errors[0]["critical"] is False


def test_critical_subscription_via_decorator():
    bus = EventBus()

    @bus.subscribe("y", critical=True)
    def boom(e):
        raise ValueError("nope")

    with pytest.raises(ValueError, match="nope"):
        bus.publish(Event(type="y", meeting_id="m"))


class _ExplodingTaskAgent:
    """TaskAgent stand-in whose extraction always raises."""

    def run(self, meeting):
        raise RuntimeError("extractor blew up")

    def dispatch_confirmed(self, meeting):  # pragma: no cover - unreachable
        raise AssertionError("dispatch must not run when extraction failed")


def test_flow_handoff_failure_leaves_meeting_unconfirmed(sample_meeting):
    flow = MeetingFlow(task_agent=_ExplodingTaskAgent())
    m = flow.run(copy.deepcopy(sample_meeting))  # stops at gate 1
    assert m.status == "awaiting_minutes_confirmation"
    with pytest.raises(RuntimeError, match="extractor blew up"):
        flow.confirm_minutes(m)
    # the flag was NOT stamped over the missing tasks, the failure is
    # visible on the meeting itself, and the audit log kept the event
    assert m.minutes_confirmed is False
    assert m.status == "awaiting_minutes_confirmation"
    assert any("bus handoff failed" in w and "minutes.confirmed" in w
               for w in m.warnings)
    assert any(e.type == "minutes.confirmed" for e in m.events)
    # retry with a healthy agent: the same confirm now succeeds
    flow.task_agent = TaskAgent()
    m = flow.confirm_minutes(m)
    assert m.minutes_confirmed is True
    assert m.status == "awaiting_tasks_confirmation"
    assert m.action_items


def test_api_confirm_minutes_handoff_failure_500_and_retryable(
        sample_meeting, monkeypatch):
    client500 = TestClient(app, raise_server_exceptions=False)
    payload = copy.deepcopy(sample_meeting).model_dump(mode="json")
    payload["id"] = "fix1-api-1"
    payload["events"] = []
    r = client500.post("/meetings", json=payload)
    assert r.status_code == 200, r.text
    r = client500.post("/meetings/fix1-api-1/process", json={})
    assert r.status_code == 200 and r.json()["status"] == "awaiting_minutes_confirmation"

    real_agent = main._flow.task_agent
    monkeypatch.setattr(main._flow, "task_agent", _ExplodingTaskAgent())
    r = client500.post("/meetings/fix1-api-1/confirm-minutes")
    assert r.status_code == 500
    got = client500.get("/meetings/fix1-api-1").json()
    assert got["minutes_confirmed"] is False
    assert got["status"] == "awaiting_minutes_confirmation"
    assert any("bus handoff failed" in w for w in got["warnings"])

    monkeypatch.setattr(main._flow, "task_agent", real_agent)
    r = client500.post("/meetings/fix1-api-1/confirm-minutes")
    assert r.status_code == 200, r.text
    assert r.json()["minutes_confirmed"] is True
    assert r.json()["status"] == "awaiting_tasks_confirmation"
    assert r.json()["action_items"], "retry must extract the tasks"


# ================================================== fix 2: minutes_confirmed
def _tasks_disabled_orch(tmp_path):
    p = tmp_path / "orchestration.yaml"
    p.write_text("pipeline:\n  steps:\n    tasks: { enabled: false }\n",
                 encoding="utf-8")
    return load_orchestration(p)


def test_run_tasks_disabled_lands_in_minutes_confirmed(tmp_path, sample_meeting):
    flow = MeetingFlow(orchestration=_tasks_disabled_orch(tmp_path))
    m = flow.run(copy.deepcopy(sample_meeting), auto_approve_minutes=True)
    assert m.minutes_confirmed is True
    assert m.status == "minutes_confirmed"  # flag and status agree
    assert m.action_items == []
    assert any("tasks step disabled" in w for w in m.warnings)


def test_api_tasks_disabled_status_and_guards(tmp_path, sample_meeting, monkeypatch):
    flow = MeetingFlow(orchestration=_tasks_disabled_orch(tmp_path))
    monkeypatch.setattr(main, "_flow", flow)
    payload = copy.deepcopy(sample_meeting).model_dump(mode="json")
    payload["id"] = "fix2-api-1"
    payload["events"] = []
    r = client.post("/meetings", json=payload)
    assert r.status_code == 200, r.text
    r = client.post("/meetings/fix2-api-1/process", json={})
    assert r.status_code == 200 and r.json()["status"] == "awaiting_minutes_confirmation"
    r = client.post("/meetings/fix2-api-1/confirm-minutes")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["minutes_confirmed"] is True
    assert body["status"] == "minutes_confirmed"
    assert any("tasks step disabled" in w for w in body["warnings"])
    # gate2 has nothing to confirm, and confirmed minutes cannot be
    # rejected any more — both stay 409 with an explanatory message
    r = client.post("/meetings/fix2-api-1/confirm-tasks")
    assert r.status_code == 409
    assert "tasks step is disabled" in r.json()["detail"]
    r = client.post("/meetings/fix2-api-1/reject-minutes", json={"reason": "太晚了"})
    assert r.status_code == 409


def test_minutes_confirmed_status_is_never_gate_timeout_scanned(sample_meeting):
    m = copy.deepcopy(sample_meeting)
    m.status = "minutes_confirmed"
    m.minutes_gate_entered_at = datetime.now(timezone.utc) - timedelta(hours=100)
    db = InMemoryDB()
    db.save(m)
    records = scan_gate_timeouts(db, MockNotifyTool(),
                                 datetime.now(timezone.utc),
                                 Settings(gate_timeout_hours=1.0))
    assert records == []
    back = db.get(m.id)
    assert back.status == "minutes_confirmed"
    assert back.minutes_gate_timeout_notified_at is None


# ===================================================== fix 3: _known bound
def test_known_registry_evicts_after_dispatch(sample_meeting):
    flow = MeetingFlow()
    m = flow.run(copy.deepcopy(sample_meeting),
                 auto_approve_minutes=True, auto_approve_tasks=True)
    assert m.status == "dispatched"
    assert m.id not in flow._known


def test_known_registry_is_bounded_with_lru_eviction():
    flow = MeetingFlow()
    total = MeetingFlow._KNOWN_LIMIT + 88
    for i in range(total):
        flow._emit(Meeting(id=f"bound-{i}", title="有界注册表"), "meeting.created")
    assert len(flow._known) <= MeetingFlow._KNOWN_LIMIT
    assert f"bound-{total - 1}" in flow._known  # newest kept
    assert "bound-0" not in flow._known          # oldest evicted


# ===================================================== fix 4: id charset
def test_create_meeting_rejects_ids_outside_charset():
    for bad in ("a/b", "a b", "a?b", "a#b", "", "中文id"):
        r = client.post("/meetings", json={"id": bad, "title": "坏 id 会议"})
        assert r.status_code == 422, (bad, r.text)


def test_create_meeting_accepts_charset_ids():
    r = client.post("/meetings", json={"id": "ok-id_1.2", "title": "正常会议"})
    assert r.status_code == 200, r.text
    assert r.json()["id"] == "ok-id_1.2"


def test_frontend_url_encodes_ids_in_paths():
    js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    # the encoder used at the call sites is this alias — pin what it is
    assert "const enc = encodeURIComponent;" in js
    encoded = ("encodeURIComponent(", "enc(")
    for m in re.finditer(r"/meetings/\$\{([^}]+)\}", js):
        assert m.group(1).startswith(encoded), \
            f"unencoded meeting id in path: {m.group(0)}"
    for m in re.finditer(r"/tasks/\$\{([^}]+)\}", js):
        assert m.group(1).startswith(encoded), \
            f"unencoded item id in path: {m.group(0)}"


# ===================================================== fix 5: ranges/scan
def _write(tmp_path, text):
    p = tmp_path / "orchestration.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_orchestration_rejects_zero_interval(tmp_path):
    p = _write(tmp_path, "scheduler:\n  reminders: { enabled: true, interval_min: 0 }\n")
    with pytest.raises(OrchestrationError) as excinfo:
        load_orchestration(p)
    assert "interval_min" in str(excinfo.value)


def test_orchestration_rejects_negative_timeout_hours(tmp_path):
    p = _write(tmp_path, "gates:\n  minutes: { timeout_hours: -1 }\n")
    with pytest.raises(OrchestrationError) as excinfo:
        load_orchestration(p)
    assert "timeout_hours" in str(excinfo.value)


def test_orchestration_rejects_timeout_hours_above_cap(tmp_path):
    p = _write(tmp_path, "gates:\n  tasks: { timeout_hours: 721 }\n")
    with pytest.raises(OrchestrationError) as excinfo:
        load_orchestration(p)
    assert "timeout_hours" in str(excinfo.value)


def test_orchestration_rejects_unparsable_cron(tmp_path):
    p = _write(tmp_path,
               "scheduler:\n  weekly_summary: { enabled: true, cron: 'not a cron' }\n")
    with pytest.raises(OrchestrationError) as excinfo:
        load_orchestration(p)
    assert "cron" in str(excinfo.value)


def _awaiting_minutes_meeting(sample_meeting, mid="fix5-scan-1"):
    m = copy.deepcopy(sample_meeting)
    m.id = mid
    m.status = "awaiting_minutes_confirmation"
    m.minutes_gate_entered_at = datetime.now(timezone.utc) - timedelta(hours=100)
    return m


def test_scan_refuses_nonpositive_shared_threshold(sample_meeting):
    db = InMemoryDB()
    db.save(_awaiting_minutes_meeting(sample_meeting))
    with pytest.raises(ValueError, match="gate_timeout_hours"):
        scan_gate_timeouts(db, MockNotifyTool(), datetime.now(timezone.utc),
                           Settings(gate_timeout_hours=-1.0,
                                    gate_timeout_action="reject"))
    back = db.get("fix5-scan-1")
    assert back.status == "awaiting_minutes_confirmation"  # untouched
    assert back.rejections == []


def test_scan_refuses_nonpositive_per_gate_threshold(sample_meeting):
    db = InMemoryDB()
    db.save(_awaiting_minutes_meeting(sample_meeting, mid="fix5-scan-2"))
    with pytest.raises(ValueError, match="minutes_gate_timeout_hours"):
        scan_gate_timeouts(db, MockNotifyTool(), datetime.now(timezone.utc),
                           Settings(gate_timeout_hours=48.0,
                                    minutes_gate_timeout_hours=0))


def test_build_scheduler_isolates_bad_weekly_cron(monkeypatch):
    for name in ("REMINDERS_ENABLED", "GATE_TIMEOUTS_ENABLED",
                 "WEEKLY_SUMMARY_ENABLED", "REMINDER_SCAN_INTERVAL_MIN",
                 "GATE_TIMEOUT_SCAN_INTERVAL_MIN"):
        monkeypatch.delenv(name, raising=False)
    orch = builtin_orchestration()
    orch.scheduler["reminders"].enabled = True
    orch.scheduler["weekly_summary"].enabled = True
    orch.scheduler["weekly_summary"].cron = "not a cron"  # injected post-load
    sched = build_scheduler(Settings(), orchestration=orch)
    assert sched is not None
    assert {j.id for j in sched.get_jobs()} == {"reminder-scan"}
