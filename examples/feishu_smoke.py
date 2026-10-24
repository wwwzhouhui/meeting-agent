#!/usr/bin/env python3
"""Feishu live smoke test — READ THIS BEFORE RUNNING.

This script intentionally creates REAL data in the configured Feishu
workspace: one test task, one group-bot message and (when a calendar ID
is configured) one test calendar event. Titles carry a [smoke] prefix
so they can be found and deleted afterwards. It does not print app
secrets, access tokens or the full webhook URL.

Usage:
    cp .env.example .env   # fill in the Feishu values first
    python3 examples/feishu_smoke.py

Recommended order in docs/feishu-setup.md is webhook -> task -> calendar
for manual troubleshooting; this script starts with the tenant-token
check because the task call depends on it.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:  # Load the project-root .env even when invoked from another cwd.
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except Exception:
    pass

from src.shared.config import get_settings  # noqa: E402
from src.tools.calendar.feishu import FeishuCalendarTool  # noqa: E402
from src.tools.feishu_client import FeishuAPIError, FeishuClient  # noqa: E402
from src.tools.notify.feishu import FeishuNotifyTool  # noqa: E402
from src.tools.tasks.feishu_task import FeishuTaskTool  # noqa: E402


def _first_open_id(settings) -> str:
    for value in settings.feishu_user_map_dict.values():
        if value.startswith("ou_"):
            return value
    return ""


def main() -> int:
    settings = get_settings()
    failures = 0
    client = FeishuClient(
        app_id=settings.feishu_app_id,
        app_secret=settings.feishu_app_secret,
        user_access_token=settings.feishu_user_access_token,
        refresh_token=settings.feishu_refresh_token,
    )
    tz = ZoneInfo(settings.default_timezone)

    print("[1/4] tenant_access_token")
    try:
        client.get_tenant_token()
        print("  OK: tenant token obtained (value not printed)")
    except Exception as exc:
        failures += 1
        print(f"  FAILED: {type(exc).__name__}: {exc}")

    print("[2/4] create test task")
    try:
        task_tool = FeishuTaskTool(
            client=client,
            user_map=settings.feishu_user_map_dict,
            timezone=settings.default_timezone,
        )
        if not task_tool.configured:
            raise FeishuAPIError("not_configured", "FEISHU_APP_ID/FEISHU_APP_SECRET are missing")
        due = (datetime.now(tz) + timedelta(days=1)).date().isoformat()
        owner = _first_open_id(settings)
        result = task_tool.create_task(
            title="[smoke] 飞书任务联调测试",
            owner_id=owner or None,
            due_date=due,
            idempotency_key="feishu-smoke-task",
            description="Created by examples/feishu_smoke.py; delete after verification.",
        )
        print(f"  OK: task guid={result.get('id')} warnings={result.get('warnings', [])}")
    except Exception as exc:
        failures += 1
        print(f"  FAILED: {type(exc).__name__}: {exc}")

    print("[3/4] webhook notification")
    try:
        notify_tool = FeishuNotifyTool(
            endpoint=settings.feishu_webhook_url,
            secret=settings.feishu_webhook_secret,
        )
        if not notify_tool.configured:
            raise FeishuAPIError("not_configured", "FEISHU_WEBHOOK_URL is missing")
        result = notify_tool.send(
            to="feishu-smoke",
            title="[smoke] 飞书通知联调测试",
            content="这是一条由 examples/feishu_smoke.py 发出的测试通知。",
        )
        print(f"  OK: code={result.get('code')} msg={result.get('msg')}")
    except Exception as exc:
        failures += 1
        print(f"  FAILED: {type(exc).__name__}: {exc}")

    print("[4/4] create test calendar event")
    if not settings.feishu_calendar_id:
        print("  SKIPPED: FEISHU_CALENDAR_ID is not configured")
    else:
        try:
            calendar_tool = FeishuCalendarTool(
                client=client,
                calendar_id=settings.feishu_calendar_id,
                auth_mode=settings.feishu_calendar_auth,
                user_map=settings.feishu_user_map_dict,
                timezone=settings.default_timezone,
            )
            start = (datetime.now(tz) + timedelta(days=1)).replace(
                hour=10, minute=0, second=0, microsecond=0
            )
            end = start + timedelta(minutes=30)
            owner = _first_open_id(settings)
            result = calendar_tool.create_event(
                title="[smoke] 飞书日程联调测试",
                start=start.isoformat(),
                attendees=[owner] if owner else [],
                description="Created by examples/feishu_smoke.py; delete after verification.",
                end=end.isoformat(),
            )
            print(
                f"  OK: event id={result.get('id')} warnings={result.get('warnings', [])} "
                f"skipped_attendees={result.get('skipped_attendees', [])}"
            )
        except Exception as exc:
            failures += 1
            print(f"  FAILED: {type(exc).__name__}: {exc}")

    if failures:
        print(f"\nSmoke finished with {failures} failed step(s).")
        return 1
    print("\nSmoke finished: all configured steps passed. Delete the [smoke] task/event afterwards.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
