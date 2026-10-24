"""Confirmation-gate timeout scanning.

scan_gate_timeouts() is a pure function over (db, notify_tool, now,
settings): it is driven either by the optional APScheduler job wired in
src/api/main.py (only when GATE_TIMEOUTS_ENABLED=true) or manually via
POST /gate-timeouts/run. It mirrors scan_and_notify() in reminders.py:
same stamp-based exactly-once bookkeeping, same honest delivery
reporting, same per-item failure isolation.

Semantics (product red line): a timeout NEVER auto-confirms a gate.
The only two actions, chosen by GATE_TIMEOUT_ACTION, are:

  - "notify" (default): send ONE escalation notice per gate entry,
    saying which meeting is stuck at which gate and for how long.
    The meeting's *_gate_timeout_notified_at stamp is set on send, so
    re-scans stay silent until the gate is re-entered (which clears the
    stamp — see flow.stamp_gate_entry). A send through the mock tool is
    reported as delivered=False with the reason in delivery_note; a
    tool that raises leaves the stamp unset (retried next scan) and is
    reported as action "notify_failed".
  - "reject": move the meeting into the EXISTING rejected state
    (minutes_rejected / tasks_rejected) and append a Rejection audit
    record whose reason states it was a confirmation timeout, so the
    normal re-run paths apply (/process for minutes, /reextract-tasks
    for tasks). No new status names are invented for this.

Timing comes from Meeting.minutes_gate_entered_at /
tasks_gate_entered_at, stamped when the flow/API lands the meeting in
an awaiting_* state. Legacy meetings whose stamp is still None are
backfilled with the first-scan time — backdating them would instantly
"expire" every old meeting, which is exactly what must not happen;
the backfill is itself reported (action "backfilled") so it is visible.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from src.shared.config import Settings, get_settings
from src.shared.schema import Rejection

logger = logging.getLogger(__name__)

# status -> (gate kind, entered_at field, notified_at field,
#           confirmed flag, rejected status, gate label for messages)
_GATES = {
    "awaiting_minutes_confirmation": (
        "minutes", "minutes_gate_entered_at", "minutes_gate_timeout_notified_at",
        "minutes_confirmed", "minutes_rejected", "纪要确认"),
    "awaiting_tasks_confirmation": (
        "tasks", "tasks_gate_entered_at", "tasks_gate_timeout_notified_at",
        "tasks_confirmed", "tasks_rejected", "任务确认"),
}


def _aware(dt: datetime) -> datetime:
    """Naive datetimes are read as UTC (stamps we write are UTC-aware;
    injected clocks in tests may be naive)."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _recipient(meeting) -> str:
    for p in meeting.participants:
        if p.role == "organizer":
            return p.user_id
    if meeting.participants:
        return meeting.participants[0].user_id
    return "unassigned"


def _fmt_hours(hours: float) -> str:
    return f"{hours:.1f}"


def scan_gate_timeouts(db, notify_tool, now: datetime,
                       settings: Settings | None = None) -> list[dict]:
    """Scan every meeting in `db` for confirmation gates that have been
    waiting longer than settings.gate_timeout_hours.

    `db` is anything with the shared store interface (list_all / save).
    `notify_tool` is a BaseNotifyTool (tools/factory.build_notify_tool);
    None falls back to the built-in MockNotifyTool. Returns the list of
    action records for THIS scan:
    {"meeting_id", "gate", "action", "delivered", "delivery_note",
     "waited_hours"} where action is one of "notified", "notify_failed",
    "rejected", "backfilled".
    """
    settings = settings or get_settings()
    # Defence in depth on the EFFECTIVE thresholds: the orchestration
    # file range-checks its own values at load, but env vars feed
    # Settings directly and can carry a zero/negative value past that
    # check. A non-positive threshold would judge EVERY waiting gate
    # timed out — with GATE_TIMEOUT_ACTION=reject that is a mass
    # auto-rejection — so refuse loudly instead of acting.
    for field_name in ("gate_timeout_hours", "minutes_gate_timeout_hours",
                       "tasks_gate_timeout_hours"):
        value = getattr(settings, field_name, None)
        if value is not None and value <= 0:
            raise ValueError(
                f"{field_name} must be > 0 hours, got {value!r}; "
                "refusing to run the gate-timeout scan")
    if notify_tool is None:
        from src.tools.notify.mock import MockNotifyTool

        notify_tool = MockNotifyTool()
    action_mode = (settings.gate_timeout_action or "notify").strip().lower()
    if action_mode not in ("notify", "reject"):
        logger.warning("GATE_TIMEOUT_ACTION %r is not notify|reject; using notify",
                       settings.gate_timeout_action)
        action_mode = "notify"
    now = _aware(now)
    records: list[dict] = []

    for meeting in db.list_all():
        spec = _GATES.get(meeting.status)
        if spec is None:
            continue
        kind, entered_field, notified_field, confirmed_field, rejected_status, label = spec
        # Per-gate threshold: settings.<kind>_gate_timeout_hours when
        # set (env MINUTES_/TASKS_GATE_TIMEOUT_HOURS, or filled from
        # the orchestration file by the API path), else the shared
        # settings.gate_timeout_hours (GATE_TIMEOUT_HOURS).
        per_gate = getattr(settings, f"{kind}_gate_timeout_hours", None)
        hours_limit = per_gate if per_gate is not None else settings.gate_timeout_hours
        timeout = timedelta(hours=hours_limit)
        entered = getattr(meeting, entered_field)
        if entered is None:
            # Legacy meeting: first sight of a gate we never stamped.
            # Stamp "now" — never backdate — and report the backfill.
            setattr(meeting, entered_field, now)
            db.save(meeting)
            records.append({
                "meeting_id": meeting.id, "gate": kind, "action": "backfilled",
                "delivered": False,
                "delivery_note": ("no gate-entry stamp (legacy meeting); "
                                    "stamped with first-scan time, timeout clock starts now"),
                "waited_hours": 0.0,
            })
            continue
        waited = now - _aware(entered)
        if waited < timeout:
            continue
        waited_hours = waited.total_seconds() / 3600.0

        if action_mode == "reject":
            reason = (f"confirmation timeout: {label} gate not confirmed within "
                      f"{_fmt_hours(hours_limit)}h "
                      f"(waited {_fmt_hours(waited_hours)}h)")
            meeting.status = rejected_status
            setattr(meeting, confirmed_field, False)
            meeting.rejections.append(Rejection(gate=kind, reason=reason, at=now))
            db.save(meeting)
            records.append({
                "meeting_id": meeting.id, "gate": kind, "action": "rejected",
                "delivered": False,
                "delivery_note": f"auto-rejected by GATE_TIMEOUT_ACTION=reject; {reason}",
                "waited_hours": waited_hours,
            })
            logger.info("gate timeout: meeting %s %s gate auto-rejected (%s)",
                        meeting.id, kind, reason)
            continue

        # action_mode == "notify": exactly once per gate entry.
        if getattr(meeting, notified_field) is not None:
            continue
        title = f"确认等待超时：{meeting.title}"
        content = (f"会议《{meeting.title}》的{label}已等待 "
                   f"{_fmt_hours(waited_hours)} 小时（超时阈值 "
                   f"{_fmt_hours(hours_limit)} 小时，进入闸门时间 "
                   f"{_aware(entered).isoformat()}），仍未确认，请尽快处理。")
        try:
            result = notify_tool.send(to=_recipient(meeting), title=title, content=content)
        except Exception as exc:  # one bad channel must not stop the scan
            logger.error("gate timeout notify failed for %s via %s: %s",
                         meeting.id, getattr(notify_tool, "system", "?"), exc)
            records.append({
                "meeting_id": meeting.id, "gate": kind, "action": "notify_failed",
                "delivered": False,
                "delivery_note": f"notify tool raised {type(exc).__name__}: {exc}; "
                                 "stamp left unset, will retry next scan",
                "waited_hours": waited_hours,
            })
            continue
        if result and result.get("mock"):
            delivered, note = False, "notify provider is mock; nothing was actually delivered"
        elif result and result.get("delivered") is False:
            delivered, note = False, "notify tool reported delivered=false"
        else:
            delivered, note = True, "notify tool accepted the escalation"
        setattr(meeting, notified_field, now)
        db.save(meeting)
        records.append({
            "meeting_id": meeting.id, "gate": kind, "action": "notified",
            "delivered": delivered, "delivery_note": note,
            "waited_hours": waited_hours,
        })
    return records
