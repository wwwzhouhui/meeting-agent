"""Due-date reminder scanning for the Task Tracker.

scan_and_notify() is a pure function over (db, notify_tool, now): it is
driven either by the optional APScheduler job wired in src/api/main.py
(only when REMINDERS_ENABLED=true) or manually via POST /reminders/run.

Rules (due_date is date-granular, so the lead window rounds up to whole
days: lead_hours=24 -> today and tomorrow count as "due soon"):
  - due within the lead window and reminder_sent_at is None
      -> send a "due soon" notification, stamp reminder_sent_at.
  - due_date already past and overdue_notified_at is None
      -> send an "overdue" notification, stamp overdue_notified_at and
         flip the item status to "overdue" (same as TaskTracker).
  - done items are never notified.
The stamps are what make repeated scans idempotent: a second scan over
the same data sends nothing. Changed meetings are saved back via
db.save(). Sends through a mock tool (result mock:true) are counted
separately in the returned "mocked" tally; a tool that raises (e.g. a
configured-but-unimplemented provider) leaves the item unstamped and is
counted under "failed" instead of crashing the scan.
"""
from __future__ import annotations

import logging
import math
from datetime import date, datetime, timedelta

logger = logging.getLogger(__name__)

# Item statuses that represent a live, not-yet-finished task.
_LIVE_STATUSES = ("pending_confirmation", "confirmed", "dispatched", "overdue")


def _recipient(item) -> str:
    return item.owner_id or item.owner_raw or "unassigned"


def scan_and_notify(db, notify_tool, now: datetime, lead_hours: float = 24) -> dict:
    """Scan every meeting in `db` and send due/overdue notifications.

    `db` is anything with the shared store interface (list_all / save).
    `notify_tool` is a BaseNotifyTool (see tools/factory.build_notify_tool);
    None falls back to the built-in MockNotifyTool.
    Returns {"due_soon_sent": int, "overdue_sent": int, "mocked": int,
             "failed": int}.
    """
    if notify_tool is None:
        from src.tools.notify.mock import MockNotifyTool

        notify_tool = MockNotifyTool()
    today = now.date()
    lead_days = max(0, math.ceil(lead_hours / 24))
    counts = {"due_soon_sent": 0, "overdue_sent": 0, "mocked": 0, "failed": 0}

    for meeting in db.list_all():
        changed = False
        for item in meeting.action_items:
            if item.status == "done" or not item.due_date:
                continue
            try:
                due = date.fromisoformat(item.due_date)
            except ValueError:
                continue
            kind = None
            if due < today and item.overdue_notified_at is None \
                    and item.status in _LIVE_STATUSES:
                kind = "overdue"
            elif today <= due <= today + timedelta(days=lead_days) \
                    and item.reminder_sent_at is None \
                    and item.status in _LIVE_STATUSES:
                kind = "due_soon"
            if kind is None:
                continue
            if kind == "overdue":
                title = f"任务已逾期：{item.title}"
                content = (f"会议《{meeting.title}》的任务「{item.title}」"
                           f"截止日期为 {item.due_date}，已经逾期，请尽快处理。")
            else:
                title = f"任务即将到期：{item.title}"
                content = (f"会议《{meeting.title}》的任务「{item.title}」"
                           f"将于 {item.due_date} 到期，请提前处理。")
            try:
                result = notify_tool.send(to=_recipient(item), title=title, content=content)
            except Exception as exc:  # one bad channel must not stop the scan
                logger.error("reminder send failed for %s via %s: %s",
                             item.id, getattr(notify_tool, "system", "?"), exc)
                counts["failed"] += 1
                continue
            if result and result.get("mock"):
                counts["mocked"] += 1
            if kind == "overdue":
                item.overdue_notified_at = now
                item.status = "overdue"
                counts["overdue_sent"] += 1
            else:
                item.reminder_sent_at = now
                counts["due_soon_sent"] += 1
            changed = True
        if changed:
            db.save(meeting)
    return counts
