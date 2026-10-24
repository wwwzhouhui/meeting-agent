
"""TODO(real): implement against Microsoft Graph: POST https://graph.microsoft.com/v1.0/me/calendars/{calendarId}/events ; POST https://graph.microsoft.com/v1.0/me/getSchedule (freebusy)
Unconfigured (no token) -> mock result + warning, never fake success."""
from __future__ import annotations
import logging
from .base import BaseCalendarTool
logger = logging.getLogger(__name__)
class OutlookCalendarTool(BaseCalendarTool):
    system = "outlook"
    def __init__(self, token: str = "", calendar_id: str = "primary"):
        self.token = token; self.calendar_id = calendar_id
    def _mock(self, kind: str, **kw) -> dict:
        logger.warning("OutlookCalendarTool not configured; mock %s %s", kind, kw)
        return {"id": f"mock-outlook-{kind}-001", "mock": True, "system": self.system, **kw}
    def list_events(self, start: str, end: str) -> list[dict]:
        if not self.token: logger.warning("OutlookCalendarTool: no token, mock list_events"); return []
        raise NotImplementedError("TODO: real API call - Microsoft Graph: POST https://graph.microsoft.com/v1.0/me/calendars/{calendarId}/events ; POST https://graph.microsoft.com/v1.0/me/getSchedule")
    def query_freebusy(self, attendees, start, end) -> dict:
        if not self.token: return self._mock("freebusy", attendees=attendees, start=start, end=end)
        raise NotImplementedError("TODO: real API call - Microsoft Graph: POST https://graph.microsoft.com/v1.0/me/calendars/{calendarId}/events ; POST https://graph.microsoft.com/v1.0/me/getSchedule")
    def create_event(self, title, start, attendees, description="", end=None) -> dict:
        if not self.token: return self._mock("event", title=title, start=start, attendees=attendees)
        raise NotImplementedError("TODO: real API call - Microsoft Graph: POST https://graph.microsoft.com/v1.0/me/calendars/{calendarId}/events ; POST https://graph.microsoft.com/v1.0/me/getSchedule")
    def update_event(self, event_id, **fields) -> dict:
        if not self.token: return self._mock("event-update", id=event_id, **fields)
        raise NotImplementedError("TODO: real API call - Microsoft Graph: POST https://graph.microsoft.com/v1.0/me/calendars/{calendarId}/events ; POST https://graph.microsoft.com/v1.0/me/getSchedule")
