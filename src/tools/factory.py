"""Build tool instances from Settings (the *_PROVIDER env vars).

Before this factory existed, CALENDAR_PROVIDER / TASK_PROVIDER were
read into Settings but never wired to anything, so setting them had
silently no effect. Now:

- provider "mock" / "" / unknown -> None (callers fall back to their
  built-in mock behaviour, exactly as in the original skeleton);
- a known provider -> the matching tool, constructed with the
  credentials found in Settings. Feishu calendar/task/notify are real
  OpenAPI implementations. Other unimplemented stubs answer with
  ``{"mock": True, ...}`` results + warnings when unconfigured and may
  raise NotImplementedError when configured, which the callers
  (TaskTracker.dispatch / Scheduler.create_follow_up) convert into a
  per-item failure / meeting warning instead of a crash.
"""
from __future__ import annotations

import logging

from src.shared.config import Settings, get_settings

logger = logging.getLogger(__name__)


def _feishu_client(settings: Settings):
    from src.tools.feishu_client import FeishuClient

    return FeishuClient(
        app_id=settings.feishu_app_id,
        app_secret=settings.feishu_app_secret,
        user_access_token=settings.feishu_user_access_token,
        refresh_token=settings.feishu_refresh_token,
    )


def build_task_tool(settings: Settings | None = None):
    from src.tools.tasks.feishu_task import FeishuTaskTool
    from src.tools.tasks.jira import JiraTaskTool
    from src.tools.tasks.linear import LinearTaskTool
    from src.tools.tasks.trello import TrelloTaskTool

    s = settings or get_settings()
    provider = (s.task_provider or "mock").strip().lower()
    if provider in ("", "mock"):
        return None
    if provider == "linear":
        return LinearTaskTool(token=s.linear_api_key)
    if provider == "trello":
        return TrelloTaskTool(token=s.trello_api_key)
    if provider == "jira":
        return JiraTaskTool(token=s.jira_api_token)
    if provider in ("feishu", "feishu_task"):
        # Real Task v2 implementation.  With app credentials absent the
        # tool remains in its honest mock mode; with them present the
        # shared client exchanges them for tenant tokens at call time.
        return FeishuTaskTool(
            client=_feishu_client(s),
            user_map=s.feishu_user_map_dict,
            timezone=s.default_timezone,
        )
    logger.warning("unknown TASK_PROVIDER %r; falling back to mock dispatch", provider)
    return None


def build_calendar_tool(settings: Settings | None = None):
    from src.tools.calendar.feishu import FeishuCalendarTool
    from src.tools.calendar.google import GoogleCalendarTool
    from src.tools.calendar.outlook import OutlookCalendarTool

    s = settings or get_settings()
    provider = (s.calendar_provider or "mock").strip().lower()
    if provider in ("", "mock"):
        return None
    if provider == "google":
        return GoogleCalendarTool(token=s.google_calendar_token)
    if provider == "outlook":
        return OutlookCalendarTool(token=s.outlook_token)
    if provider == "feishu":
        return FeishuCalendarTool(
            client=_feishu_client(s),
            calendar_id=s.feishu_calendar_id,
            auth_mode=s.feishu_calendar_auth,
            user_map=s.feishu_user_map_dict,
            timezone=s.default_timezone,
        )
    logger.warning("unknown CALENDAR_PROVIDER %r; falling back to mock calendar", provider)
    return None


def build_notify_tool(settings: Settings | None = None):
    """Notify tool for the reminder scanner (src/orchestrator/reminders.py).

    provider mock/"" -> MockNotifyTool (send() returns mock:true,
    delivered:false — counted separately by the scanner); a known real
    provider -> its tool (Feishu webhook is a real implementation);
    unknown -> warning + MockNotifyTool so the reminder path never ends
    up tool-less.
    """
    from src.tools.notify.email import EmailNotifyTool
    from src.tools.notify.feishu import FeishuNotifyTool
    from src.tools.notify.mock import MockNotifyTool
    from src.tools.notify.slack import SlackNotifyTool

    s = settings or get_settings()
    provider = (s.notify_provider or "mock").strip().lower()
    if provider in ("", "mock"):
        return MockNotifyTool()
    if provider == "feishu":
        return FeishuNotifyTool(endpoint=s.feishu_webhook_url, secret=s.feishu_webhook_secret)
    if provider == "slack":
        return SlackNotifyTool(endpoint=s.slack_webhook)
    if provider == "email":
        return EmailNotifyTool(endpoint="")  # SMTP settings not modelled yet
    logger.warning("unknown NOTIFY_PROVIDER %r; falling back to mock notify", provider)
    return MockNotifyTool()
