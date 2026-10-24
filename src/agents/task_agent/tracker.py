"""Tracking + dispatch. Dispatch is idempotent (idempotency_key /
external_task_id): re-running never creates a duplicate external task.
Unconfigured task tool -> mock external id + warning log.
Due-soon / overdue reminder sending lives in
src/orchestrator/reminders.py (scan_and_notify), driven by an optional
APScheduler job (REMINDERS_ENABLED) or POST /reminders/run; the helpers
below (check_overdue / reminders_due / weekly_summary) remain the
date-logic building blocks.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from src.shared.schema import ActionItem
from src.tools.tasks.base import BaseTaskTool

logger = logging.getLogger(__name__)


class TaskTracker:
    def __init__(self, task_tool: BaseTaskTool | None = None):
        self.task_tool = task_tool

    def dispatch(self, items: list[ActionItem]) -> list[ActionItem]:
        for item in items:
            if item.status not in ("confirmed", "pending_confirmation"):
                continue
            if item.external_task_id:  # idempotent: already dispatched
                continue
            if self.task_tool is not None:
                # Per-item isolation: one failing external system (e.g. a
                # configured-but-unimplemented provider raising
                # NotImplementedError) must NOT crash the whole flow.
                # The item is marked dispatch_failed with the error kept,
                # and the remaining items still dispatch.
                try:
                    result = self.task_tool.create_task(
                        title=item.title, owner_id=item.owner_id,
                        due_date=item.due_date, idempotency_key=item.idempotency_key or item.id,
                    )
                except Exception as exc:
                    logger.error("dispatch failed for %s via %s: %s",
                                 item.id, getattr(self.task_tool, "system", "?"), exc)
                    item.status = "dispatch_failed"
                    item.dispatch_error = f"{type(exc).__name__}: {exc}"
                    continue
                if not result or not result.get("id"):
                    item.status = "dispatch_failed"
                    item.dispatch_error = "task tool returned no task id"
                    continue
                item.external_task_id = result.get("id")
                item.external_system = result.get("system")
            else:
                logger.warning("No task tool configured; mock-dispatching %s", item.id)
                item.external_task_id = f"mock-task-{item.id}"
                item.external_system = "mock"
            item.dispatch_error = None
            item.status = "dispatched"
        return items

    @staticmethod
    def check_overdue(items: list[ActionItem], today: date | None = None) -> list[ActionItem]:
        today = today or date.today()
        overdue = []
        for item in items:
            if item.due_date and item.status not in ("done", "overdue"):
                try:
                    due = date.fromisoformat(item.due_date)
                except ValueError:
                    continue
                if due < today:
                    item.status = "overdue"
                    overdue.append(item)
        return overdue

    @staticmethod
    def reminders_due(items: list[ActionItem], today: date | None = None) -> list[ActionItem]:
        today = today or date.today()
        out = []
        for item in items:
            if item.due_date and item.status in ("dispatched", "confirmed"):
                try:
                    due = date.fromisoformat(item.due_date)
                except ValueError:
                    continue
                if due == today + timedelta(days=1):
                    out.append(item)
        return out

    @staticmethod
    def weekly_summary(items: list[ActionItem]) -> str:
        pending = [i for i in items if i.status not in ("done",)]
        if not pending:
            return "本周无未完成任务。"
        lines = ["本周未完成任务："]
        lines += [f"- [{i.owner_raw}] {i.title}（截止 {i.due_date or '未定'}，{i.status}）" for i in pending]
        return "\n".join(lines)
