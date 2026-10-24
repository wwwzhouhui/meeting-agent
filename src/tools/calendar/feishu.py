"""Feishu Calendar v4 implementation.

Endpoints used:
- POST   /open-apis/calendar/v4/calendars/{calendar_id}/events
- GET    /open-apis/calendar/v4/calendars/{calendar_id}/events
- GET    /open-apis/calendar/v4/calendars/{calendar_id}/events/{event_id}
- PATCH  /open-apis/calendar/v4/calendars/{calendar_id}/events/{event_id}
- DELETE /open-apis/calendar/v4/calendars/{calendar_id}/events/{event_id}
- POST   /open-apis/calendar/v4/calendars/{calendar_id}/events/{event_id}/attendees
- POST   /open-apis/calendar/v4/freebusy/list

Feishu calendar timestamps are Unix **seconds** (strings), unlike Feishu
Task v2's millisecond timestamps.

Auth matters: a tenant token can write only the app's own calendar.  To
write a user's calendar, set FEISHU_CALENDAR_AUTH=user plus a user access
token (or refresh token).  Events therefore require FEISHU_CALENDAR_ID
in both modes; this tool never invents Feishu's non-existent "primary"
calendar id.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from src.tools.feishu_client import (
    FeishuAPIError,
    FeishuClient,
    map_user_id,
    parse_feishu_datetime,
    timestamp_seconds,
)

from .base import BaseCalendarTool

logger = logging.getLogger(__name__)


class FeishuCalendarTool(BaseCalendarTool):
    system = "feishu"

    def __init__(
        self,
        token: str | FeishuClient = "",
        calendar_id: str = "",
        *,
        client: FeishuClient | None = None,
        app_id: str = "",
        app_secret: str = "",
        auth: str = "tenant",
        auth_mode: str | None = None,
        user_access_token: str = "",
        refresh_token: str = "",
        user_map: dict[str, str] | None = None,
        timezone: str = "Asia/Shanghai",
        transport: Any = None,
        http_client: Any = None,
    ):
        # Backwards-compatible first argument: older code passed a token
        # positionally.  Accepting a FeishuClient there too makes tests
        # and embedding code less brittle.
        if isinstance(token, FeishuClient):
            client = token
            token = ""
        self.token = str(token or "")
        if client is None and (app_id or app_secret or user_access_token or refresh_token or self.token):
            client = FeishuClient(
                app_id=app_id,
                app_secret=app_secret,
                user_access_token=user_access_token,
                refresh_token=refresh_token,
                static_access_token=self.token if (auth_mode or auth) != "user" else "",
                transport=transport,
                http_client=http_client,
            )
            if (auth_mode or auth) == "user" and self.token and not user_access_token:
                client.user_access_token = self.token
        self.client = client
        self.calendar_id = calendar_id or ""
        mode = (auth_mode or auth or "tenant").strip().lower()
        if mode not in ("tenant", "user"):
            logger.warning("invalid Feishu calendar auth mode %r; using tenant", mode)
            mode = "tenant"
        self.auth_mode = mode
        self.auth = mode
        self.user_map = dict(user_map or {})
        self.timezone = timezone or "Asia/Shanghai"

    @property
    def configured(self) -> bool:
        return bool(self.client and self.client.configured)

    # -------------------------------------------------------------- helpers
    def _mock(self, kind: str, **kw) -> dict:
        logger.warning("FeishuCalendarTool not configured; mock %s %s", kind, kw)
        return {"id": f"mock-feishu-{kind}-001", "mock": True, "system": self.system, **kw}

    def _require_calendar_id(self) -> str:
        if not self.calendar_id:
            raise FeishuAPIError(
                "not_configured",
                "FEISHU_CALENDAR_ID is not configured: create an app calendar in Feishu "
                "or fill in a user calendar id before writing events",
            )
        return self.calendar_id

    def _path(self, suffix: str = "") -> str:
        calendar_id = self._require_calendar_id()
        return f"/open-apis/calendar/v4/calendars/{calendar_id}/events{suffix}"

    @staticmethod
    def _event_from_response(data: dict) -> dict:
        if isinstance(data.get("event"), dict):
            return data["event"]
        return data

    def _normalized_event(self, data: dict, **extra) -> dict:
        event = self._event_from_response(data)
        event_id = event.get("event_id") or event.get("id")
        return {
            "id": event_id,
            "event_id": event_id,
            "mock": False,
            "system": self.system,
            "event": event,
            **extra,
        }

    @staticmethod
    def _raw_attendee_id(value: Any) -> str:
        if isinstance(value, dict):
            for key in ("attendee_id", "open_id", "user_id", "id", "email", "name"):
                if value.get(key):
                    return str(value[key])
            return ""
        return str(value or "")

    def _split_attendees(self, attendees: list[Any]) -> tuple[list[str], list[str]]:
        valid: list[str] = []
        skipped: list[str] = []
        for raw_value in attendees or []:
            raw = self._raw_attendee_id(raw_value)
            mapped = map_user_id(raw, self.user_map)
            if mapped:
                if mapped not in valid:
                    valid.append(mapped)
            elif raw:
                skipped.append(raw)
        return valid, skipped

    def _time_payload(self, value, timezone: str) -> dict:
        return {"timestamp": timestamp_seconds(value, timezone), "timezone": timezone}

    # ------------------------------------------------------------------ API
    def list_events(self, start: str, end: str) -> list[dict]:
        if not self.configured:
            logger.warning("FeishuCalendarTool: not configured, mock list_events")
            return []
        path = self._path()
        data = self.client.request(
            "GET",
            path,
            token_type=self.auth_mode,
            params={
                "start_time": timestamp_seconds(start, self.timezone),
                "end_time": timestamp_seconds(end, self.timezone),
                "page_size": 100,
                "user_id_type": "open_id",
            },
        )
        raw_items = []
        for key in ("items", "event_list", "events"):
            if isinstance(data.get(key), list):
                raw_items = data[key]
                break
        out = []
        for event in raw_items:
            if isinstance(event, dict):
                item = dict(event)
                item.setdefault("id", event.get("event_id"))
                out.append(item)
        return out

    def query_freebusy(self, attendees, start, end) -> dict:
        if not self.configured:
            return self._mock("freebusy", attendees=attendees, start=start, end=end)
        valid, skipped = self._split_attendees(attendees)
        warnings = []
        if skipped:
            warnings.append(f"skipped non-open_id/freebusy attendees: {skipped}")
        if not valid:
            return {
                "mock": False,
                "system": self.system,
                "freebusy_list": [],
                "warnings": warnings + ["no valid open_id attendees were provided"],
            }
        body = {
            "time_min": timestamp_seconds(start, self.timezone),
            "time_max": timestamp_seconds(end, self.timezone),
            "user_id": {"user_ids": valid, "id_type": "open_id"},
            "include_external_calendar": True,
            "only_busy": True,
        }
        data = self.client.request(
            "POST", "/open-apis/calendar/v4/freebusy/list", token_type=self.auth_mode, json=body
        )
        return {
            "mock": False,
            "system": self.system,
            "freebusy_list": data.get("freebusy_list", data),
            "data": data,
            "warnings": warnings,
        }

    def create_event(self, title, start, attendees, description="", end=None) -> dict:
        if not self.configured:
            return self._mock("event", title=title, start=start, attendees=attendees)
        calendar_id = self._require_calendar_id()
        timezone = self.timezone
        start_dt = parse_feishu_datetime(start, timezone)
        end_dt = parse_feishu_datetime(end, timezone) if end else start_dt + timedelta(hours=1)
        if end_dt <= start_dt:
            raise ValueError("calendar event end must be after start")
        body = {
            "summary": title,
            "description": description or "",
            "start_time": {"timestamp": str(int(start_dt.timestamp())), "timezone": timezone},
            "end_time": {"timestamp": str(int(end_dt.timestamp())), "timezone": timezone},
            "need_notification": True,
        }
        path = self._path()
        data = self.client.request(
            "POST", path, token_type=self.auth_mode, json=body, params={"user_id_type": "open_id"}
        )
        valid_attendees, skipped = self._split_attendees(attendees)
        warnings = []
        if skipped:
            warnings.append(f"skipped non-open_id attendees: {skipped}")
        event = self._event_from_response(data)
        event_id = event.get("event_id") or event.get("id")
        attendee_result = None
        if valid_attendees and event_id:
            attendee_path = f"{path}/{event_id}/attendees"
            attendee_body = {
                "attendees": [
                    {"type": "user", "attendee_id": open_id} for open_id in valid_attendees
                ],
                "need_notification": True,
            }
            try:
                attendee_result = self.client.request(
                    "POST",
                    attendee_path,
                    token_type=self.auth_mode,
                    json=attendee_body,
                    params={"user_id_type": "open_id"},
                )
            except Exception as exc:
                # The event itself was created successfully.  A failed
                # attendee add is surfaced, but must not turn the created
                # event into a phantom failure or crash the flow.
                logger.error("Feishu event %s created but attendees failed: %s", event_id, exc)
                warnings.append(f"attendees could not be added: {type(exc).__name__}: {exc}")
        return self._normalized_event(
            data,
            calendar_id=calendar_id,
            attendees_added=valid_attendees if attendee_result is not None else [],
            skipped_attendees=skipped,
            attendee_result=attendee_result,
            warnings=warnings,
        )

    def get_event(self, event_id: str) -> dict:
        if not self.configured:
            return self._mock("event-get", id=event_id)
        data = self.client.request(
            "GET", self._path(f"/{event_id}"), token_type=self.auth_mode,
            params={"user_id_type": "open_id"},
        )
        return self._normalized_event(data, calendar_id=self.calendar_id)

    def update_event(self, event_id: str, **fields) -> dict:
        if not self.configured:
            return self._mock("event-update", id=event_id, **fields)
        timezone = str(fields.pop("timezone", self.timezone) or self.timezone)
        body: dict[str, Any] = {}
        if "title" in fields:
            body["summary"] = fields.pop("title")
        if "summary" in fields:
            body["summary"] = fields.pop("summary")
        if "description" in fields:
            body["description"] = fields.pop("description")
        if "start" in fields:
            body["start_time"] = self._time_payload(fields.pop("start"), timezone)
        if "start_time" in fields:
            raw_start = fields.pop("start_time")
            body["start_time"] = raw_start if isinstance(raw_start, dict) else self._time_payload(raw_start, timezone)
        if "end" in fields:
            body["end_time"] = self._time_payload(fields.pop("end"), timezone)
        if "end_time" in fields:
            raw_end = fields.pop("end_time")
            body["end_time"] = raw_end if isinstance(raw_end, dict) else self._time_payload(raw_end, timezone)
        # Any remaining caller-supplied fields are already in Feishu's
        # naming (visibility, status, reminders, ...) and pass through.
        body.update(fields)
        data = self.client.request(
            "PATCH", self._path(f"/{event_id}"), token_type=self.auth_mode, json=body,
            params={"user_id_type": "open_id"},
        )
        return self._normalized_event(data, calendar_id=self.calendar_id)

    def delete_event(self, event_id: str) -> dict:
        if not self.configured:
            return self._mock("event-delete", id=event_id)
        data = self.client.request(
            "DELETE", self._path(f"/{event_id}"), token_type=self.auth_mode,
            params={"user_id_type": "open_id"},
        )
        return {
            "id": event_id,
            "event_id": event_id,
            "deleted": True,
            "mock": False,
            "system": self.system,
            "data": data,
        }

    # A descriptive alias matching the calendar terminology used in docs.
    def remove_event(self, event_id: str) -> dict:
        return self.delete_event(event_id)
