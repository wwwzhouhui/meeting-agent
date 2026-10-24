"""Scheduler: the only calendar writer. Must go through a confirmation
gate before create_event is called (enforced by orchestrator, not here).
Unconfigured tool -> mock event id + warning, never fake success silently.
"""
from __future__ import annotations

import logging

from src.shared.schema import Meeting
from src.tools.calendar.base import BaseCalendarTool

logger = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, tool: BaseCalendarTool | None = None):
        self.tool = tool

    def create_follow_up(self, meeting: Meeting, chosen_time: str, title: str | None = None) -> Meeting:
        title = title or f"{meeting.title}（跟进会）"
        attendees = [p.email or p.name for p in meeting.participants]
        if self.tool is not None:
            try:
                result = self.tool.create_event(
                    title=title, start=chosen_time, attendees=attendees,
                    description=f"Follow-up of meeting {meeting.id}",
                )
            except Exception as exc:
                # Configured-but-unimplemented provider (or a real API
                # failure) must not crash the caller: keep the meeting
                # event-less and surface the problem as a warning.
                logger.error("calendar create_event failed via %s: %s",
                             getattr(self.tool, "system", "?"), exc)
                meeting.warnings.append(
                    f"calendar create_event failed: {type(exc).__name__}: {exc}")
                return meeting
            meeting.calendar_event_id = result.get("id")
        else:
            logger.warning("No calendar tool configured; returning mock event id")
            meeting.calendar_event_id = f"mock-cal-{meeting.id}"
        return meeting
