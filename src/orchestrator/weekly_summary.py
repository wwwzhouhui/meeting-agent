"""Weekly task summary: build + deliver (AUDIT #9 / #17 follow-up).

TaskTracker.weekly_summary() existed since the skeleton but had no
caller. build_weekly_summary() is that caller: it collects ActionItems
across EVERY meeting in the store, selects the ones belonging to the
window, feeds the open ones to TaskTracker.weekly_summary() unchanged
(its format — "本周未完成任务：" + one line per item — is the report
body; nothing new is invented), and sends the result through the
notify tool. It is driven by the optional scheduler job in
src/api/main.py (WEEKLY_SUMMARY_ENABLED=true -> Mondays 09:00) or
manually via POST /weekly-summary/run.

Window: the trailing `window_days` days ending at now.date(),
[period_start, period_end] inclusive. Buckets (disjoint):

  - due_open:       not done, due_date inside the window
  - overdue_open:   not done, due_date before the window
  - undated_open:   not done, no (parseable) due_date
  - done_in_window: done, due_date inside the window

Open items outside the window only by being due in the FUTURE are not
this week's business and are excluded; done items due outside the
window are excluded too (an item's due_date is the only date evidence
available — there is no completed_at field — so "completed this week"
honestly means "due this week and done"). Items carry their meeting
title as context, added here at the collection layer by prefixing a
copy's title with 《meeting title》; stored items are never mutated.

Delivery reporting is honest, same as the scanners: mock tool or no
tool -> delivered=False with the reason in delivery_note, while the
summary text itself is always produced in full.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from src.agents.task_agent.tracker import TaskTracker

logger = logging.getLogger(__name__)


def _parse_due(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def build_weekly_summary(db, now: datetime, window_days: int = 7,
                         notify_tool=None) -> dict:
    """Build the weekly summary over all meetings in `db` and try to
    deliver it via `notify_tool` (None -> build only).

    Returns {"period_start", "period_end", "summary", "delivered",
             "delivery_note"} — dates are ISO strings, summary is the
    full Markdown text regardless of delivery outcome.
    """
    window_days = max(1, int(window_days))
    period_end = now.date()
    period_start = period_end - timedelta(days=window_days - 1)

    due_open, overdue_open, undated_open, done_in_window = [], [], [], []
    for meeting in db.list_all():
        for item in meeting.action_items:
            shown = item.model_copy(update={"title": f"《{meeting.title}》{item.title}"})
            due = _parse_due(item.due_date)
            if item.status == "done":
                if due is not None and period_start <= due <= period_end:
                    done_in_window.append(shown)
                continue
            if due is None:
                undated_open.append(shown)
            elif due < period_start:
                overdue_open.append(shown)
            elif due <= period_end:
                due_open.append(shown)
            # else: due in the future -> not this week's summary

    open_items = sorted(
        due_open + overdue_open + undated_open,
        key=lambda i: (i.due_date is None, i.due_date or "", i.title))
    counts_line = (
        f"本周到期未完成 {len(due_open)} 项 · 逾期未完成 {len(overdue_open)} 项 · "
        f"未定日期仍开放 {len(undated_open)} 项 · 本周已完成 {len(done_in_window)} 项")
    parts = [f"# 任务周报（{period_start.isoformat()} ~ {period_end.isoformat()}）",
             counts_line, "",
             TaskTracker.weekly_summary(open_items)]
    if done_in_window:
        parts += ["", "本周已完成任务："] + [
            f"- [{i.owner_raw}] {i.title}（截止 {i.due_date or '未定'}，{i.status}）"
            for i in done_in_window]
    summary = "\n".join(parts)

    delivered, note = False, ""
    if notify_tool is None:
        note = "no notify tool configured; summary built but not sent"
    else:
        try:
            result = notify_tool.send(
                to="team",
                title=f"任务周报（{period_start.isoformat()} ~ {period_end.isoformat()}）",
                content=summary)
        except Exception as exc:  # a bad channel must not lose the summary
            logger.error("weekly summary send failed via %s: %s",
                         getattr(notify_tool, "system", "?"), exc)
            note = f"notify tool raised {type(exc).__name__}: {exc}"
        else:
            if result and result.get("mock"):
                note = "notify provider is mock; nothing was actually delivered"
            elif result and result.get("delivered") is False:
                note = "notify tool reported delivered=false"
            else:
                delivered, note = True, "notify tool accepted the summary"
    return {
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "summary": summary,
        "delivered": delivered,
        "delivery_note": note,
    }
