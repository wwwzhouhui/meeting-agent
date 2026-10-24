"""Reminder scanning: scan_and_notify with a fake clock + recording /
mock / failing notify tools, idempotency, persistence, API trigger."""
from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.orchestrator.reminders import scan_and_notify
from src.shared.db import InMemoryDB, store
from src.shared.schema import ActionItem, Meeting
from src.tools.notify.base import BaseNotifyTool
from src.tools.notify.mock import MockNotifyTool

client = TestClient(app)
NOW = datetime(2026, 10, 23, 9, 0, 0)  # a Friday


class RecordingNotifyTool(BaseNotifyTool):
    system = "recording"

    def __init__(self):
        self.sends = []

    def send(self, to, title, content, **kw):
        self.sends.append({"to": to, "title": title, "content": content})
        return {"mock": False, "delivered": True, "system": self.system, "to": to}


class FailingNotifyTool(BaseNotifyTool):
    system = "failing"

    def send(self, to, title, content, **kw):
        raise RuntimeError("channel down")


def _item(iid, due, status="dispatched"):
    return ActionItem(id=iid, title=f"任务{iid}", owner_id="u-lisi", owner_raw="李四",
                      due_date=due, source_segment_ids=[1], status=status)


def _db_with(items, mid="rem-1"):
    db = InMemoryDB()
    db.save(Meeting(id=mid, title="提醒测试会", action_items=items))
    return db


@pytest.fixture(autouse=True)
def _clean_store():
    store._data.clear()
    yield
    store._data.clear()


def test_scan_due_soon_and_overdue_once_only():
    db = _db_with([
        _item("soon", "2026-10-24"),                    # tomorrow -> due soon
        _item("today", "2026-10-23", status="confirmed"),  # today -> due soon
        _item("late", "2026-10-22"),                    # yesterday -> overdue
        _item("far", "2026-10-30"),                     # outside window
        _item("done", "2026-10-24", status="done"),     # never notified
        _item("nodue", None),
    ])
    tool = RecordingNotifyTool()
    counts = scan_and_notify(db, tool, NOW, lead_hours=24)
    assert counts == {"due_soon_sent": 2, "overdue_sent": 1, "mocked": 0, "failed": 0}
    assert len(tool.sends) == 3
    assert all(s["to"] == "u-lisi" for s in tool.sends)
    assert any("已逾期" in s["title"] for s in tool.sends)
    assert any("即将到期" in s["title"] for s in tool.sends)

    saved = {i.id: i for i in db.get("rem-1").action_items}
    assert saved["soon"].reminder_sent_at == NOW
    assert saved["late"].overdue_notified_at == NOW
    assert saved["late"].status == "overdue"  # flipped like TaskTracker
    assert saved["far"].reminder_sent_at is None
    assert saved["done"].reminder_sent_at is None

    # second scan over the same data sends nothing (idempotent)
    counts2 = scan_and_notify(db, tool, NOW, lead_hours=24)
    assert counts2 == {"due_soon_sent": 0, "overdue_sent": 0, "mocked": 0, "failed": 0}
    assert len(tool.sends) == 3


def test_scan_lead_window_respects_lead_hours():
    db = _db_with([_item("tomorrow", "2026-10-24")])
    tool = RecordingNotifyTool()
    counts = scan_and_notify(db, tool, NOW, lead_hours=0)
    assert counts["due_soon_sent"] == 0 and not tool.sends
    counts = scan_and_notify(db, tool, NOW, lead_hours=24)
    assert counts["due_soon_sent"] == 1


def test_scan_mock_tool_counted_separately():
    db = _db_with([_item("soon", "2026-10-24"), _item("late", "2026-10-20")])
    counts = scan_and_notify(db, MockNotifyTool(), NOW, lead_hours=24)
    assert counts["due_soon_sent"] == 1 and counts["overdue_sent"] == 1
    assert counts["mocked"] == 2  # both sends were mock sends
    # mock sends still stamp the items, so a rescan is silent
    counts2 = scan_and_notify(db, MockNotifyTool(), NOW, lead_hours=24)
    assert counts2["mocked"] == 0 and counts2["due_soon_sent"] == 0


def test_scan_default_tool_is_mock_and_failing_tool_does_not_crash():
    db = _db_with([_item("soon", "2026-10-24")])
    counts = scan_and_notify(db, None, NOW, lead_hours=24)  # None -> mock fallback
    assert counts["due_soon_sent"] == 1 and counts["mocked"] == 1

    db2 = _db_with([_item("soon", "2026-10-24")], mid="rem-2")
    counts = scan_and_notify(db2, FailingNotifyTool(), NOW, lead_hours=24)
    assert counts["failed"] == 1 and counts["due_soon_sent"] == 0
    # not stamped -> a later scan with a working tool will retry it
    assert db2.get("rem-2").action_items[0].reminder_sent_at is None


def test_api_reminders_run():
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    store.save(Meeting(id="rem-api-1", title="API 提醒会",
                       action_items=[_item("api-item", tomorrow)]))
    r = client.post("/reminders/run")
    assert r.status_code == 200
    body = r.json()
    assert body["due_soon_sent"] == 1 and body["overdue_sent"] == 0
    assert body["mocked"] == 1  # default NOTIFY_PROVIDER=mock in tests
    r = client.post("/reminders/run")
    assert r.json()["due_soon_sent"] == 0  # already stamped -> silent
