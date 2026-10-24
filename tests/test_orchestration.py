"""Orchestration config: loading, validation, env overrides, the
switches genuinely changing flow behaviour, scheduler registration
from the file, and per-gate timeout thresholds from the file."""
import copy

import pytest

from src.api.main import build_scheduler
from src.orchestrator.flow import MeetingFlow
from src.orchestrator.gate_timeouts import scan_gate_timeouts
from src.shared.config import Settings
from src.shared.db import InMemoryDB
from src.shared.orchestration import (
    OrchestrationError,
    load_orchestration,
)
from src.shared.schema import Meeting
from src.tools.notify.mock import MockNotifyTool


def _write(tmp_path, text, name="orchestration.yaml"):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


# ------------------------------------------------------------- loading
def test_default_file_loads_with_expected_structure():
    orch = load_orchestration()
    assert orch.source.endswith("orchestration.yaml")
    assert set(orch.steps) == {"brief", "transcribe", "minutes", "tasks", "schedule", "dispatch"}
    assert all(s.enabled for s in orch.steps.values())
    assert orch.gate("minutes").enabled and orch.gate("minutes").timeout_hours == 48.0
    assert orch.gate("tasks").enabled and orch.gate("tasks").timeout_hours == 48.0
    assert set(orch.scheduler) == {"reminders", "gate_timeouts", "weekly_summary"}
    assert not any(j.enabled for j in orch.scheduler.values())  # jobs default off
    assert orch.scheduler["weekly_summary"].cron == "0 9 * * mon"
    assert orch.providers == {"calendar": "mock", "task": "mock", "notify": "mock"}
    assert orch.engines == {"asr": "mock", "llm_mode": "auto"}


def test_missing_file_falls_back_to_builtin_defaults(tmp_path):
    orch = load_orchestration(tmp_path / "does-not-exist.yaml")
    assert orch.source == "builtin-default"
    assert all(s.enabled for s in orch.steps.values())
    assert not any(j.enabled for j in orch.scheduler.values())


def test_bad_yaml_raises_readable_error(tmp_path):
    p = _write(tmp_path, "pipeline:\n  steps: [unclosed\n")
    with pytest.raises(OrchestrationError) as excinfo:
        load_orchestration(p)
    assert "orchestration.yaml" in str(excinfo.value)


def test_bad_value_type_raises_readable_error(tmp_path):
    p = _write(tmp_path, "pipeline:\n  steps:\n    minutes: { enabled: maybe }\n")
    with pytest.raises(OrchestrationError) as excinfo:
        load_orchestration(p)
    assert "minutes" in str(excinfo.value)


def test_env_override_beats_file(tmp_path, monkeypatch):
    p = _write(tmp_path, "pipeline:\n  steps:\n    dispatch: { enabled: true }\n")
    monkeypatch.setenv("ORCH_STEP_DISPATCH_ENABLED", "false")
    orch = load_orchestration(p)
    assert orch.steps["dispatch"].enabled is False


# ---------------------------------------------------- switches in flow
def _flow_with(tmp_path, yaml_text):
    orch = load_orchestration(_write(tmp_path, yaml_text))
    return MeetingFlow(orchestration=orch)


def test_brief_step_disabled(tmp_path, sample_meeting):
    flow = _flow_with(tmp_path, "pipeline:\n  steps:\n    brief: { enabled: false }\n")
    m = copy.deepcopy(sample_meeting)
    out = flow.prepare_brief(m, history=[])
    assert out.brief is None
    assert any("brief step disabled" in w for w in out.warnings)
    assert not any(e.type == "brief.generated" for e in out.events)


def test_dispatch_step_disabled_stops_at_tasks_confirmed(tmp_path, sample_meeting):
    flow = _flow_with(tmp_path, "pipeline:\n  steps:\n    dispatch: { enabled: false }\n")
    m = flow.run(copy.deepcopy(sample_meeting),
                 auto_approve_minutes=True, auto_approve_tasks=True)
    assert m.status == "tasks_confirmed"
    assert m.action_items, "tasks are still extracted and confirmed"
    assert all(a.external_task_id is None for a in m.action_items)
    assert any("dispatch step disabled" in w for w in m.warnings)
    types = [e.type for e in m.events]
    assert "tasks.confirmed" in types
    assert "tasks.dispatched" not in types


def test_minutes_gate_disabled_auto_passes_with_trace(tmp_path, sample_meeting):
    flow = _flow_with(tmp_path, "gates:\n  minutes: { enabled: false }\n")
    m = flow.run(copy.deepcopy(sample_meeting))  # no human/auto approval at all
    assert m.minutes_confirmed is True
    assert m.action_items, "gate1 auto-pass must still extract tasks via the bus"
    assert m.status == "awaiting_tasks_confirmation"  # gate2 is still a real gate
    assert any("gate disabled" in w and "auto-passed" in w for w in m.warnings)
    confirmed = [e for e in m.events if e.type == "minutes.confirmed"]
    assert confirmed and confirmed[0].payload["via"] == "config_gate_disabled"


def test_transcribe_step_disabled_stops_at_needs_transcript(tmp_path, sample_meeting):
    flow = _flow_with(tmp_path, "pipeline:\n  steps:\n    transcribe: { enabled: false }\n")
    m = copy.deepcopy(sample_meeting)
    m.transcript = []
    m.audio_url = "https://example.com/recording.wav"
    out = flow.run(m, auto_approve_minutes=True, auto_approve_tasks=True)
    assert out.status == "needs_transcript"
    assert any("transcribe step disabled" in w for w in out.warnings)


# ------------------------------------------------------------ scheduler
def _clean_job_env(monkeypatch):
    for name in ("REMINDERS_ENABLED", "GATE_TIMEOUTS_ENABLED", "WEEKLY_SUMMARY_ENABLED",
                 "REMINDER_SCAN_INTERVAL_MIN", "GATE_TIMEOUT_SCAN_INTERVAL_MIN"):
        monkeypatch.delenv(name, raising=False)


def test_scheduler_registration_from_orchestration(tmp_path, monkeypatch):
    _clean_job_env(monkeypatch)
    orch = load_orchestration(_write(
        tmp_path,
        "scheduler:\n  reminders: { enabled: true, interval_min: 45 }\n"
        "  weekly_summary: { enabled: true, cron: '15 8 * * tue' }\n"))
    sched = build_scheduler(Settings(), orchestration=orch)
    assert sched is not None
    job_ids = {j.id for j in sched.get_jobs()}
    assert job_ids == {"reminder-scan", "weekly-summary"}
    # the scheduler was only built, never started: nothing to shut down


def test_scheduler_env_beats_orchestration(tmp_path, monkeypatch):
    _clean_job_env(monkeypatch)
    orch = load_orchestration(_write(
        tmp_path, "scheduler:\n  reminders: { enabled: true }\n"))
    monkeypatch.setenv("REMINDERS_ENABLED", "false")
    sched = build_scheduler(Settings(), orchestration=orch)
    assert sched is None  # env explicitly off wins over the file


def test_scheduler_default_orchestration_registers_nothing(monkeypatch):
    _clean_job_env(monkeypatch)
    assert build_scheduler(Settings(), orchestration=load_orchestration()) is None


# ------------------------------------------------- per-gate thresholds
def test_scanner_honours_per_gate_timeout_hours(sample_meeting):
    from datetime import datetime, timedelta, timezone

    m = copy.deepcopy(sample_meeting)
    m.status = "awaiting_minutes_confirmation"
    m.minutes_gate_entered_at = datetime.now(timezone.utc) - timedelta(hours=2)
    db = InMemoryDB()
    db.save(m)
    tool = MockNotifyTool()
    now = datetime.now(timezone.utc)
    # shared threshold 48h: nothing happens yet
    shared = Settings(gate_timeout_hours=48.0)
    assert scan_gate_timeouts(db, tool, now, shared) == []
    # minutes-gate threshold 1h (as filled from the orchestration file
    # by the API path): the same meeting now escalates
    per_gate = Settings(gate_timeout_hours=48.0, minutes_gate_timeout_hours=1.0)
    records = scan_gate_timeouts(db, tool, now, per_gate)
    assert len(records) == 1
    assert records[0]["gate"] == "minutes" and records[0]["action"] == "notified"
