"""Weekly summary: build_weekly_summary aggregation across meetings,
window bucketing, honest delivery reporting, the POST /weekly-summary/run
endpoint, and Monday-09:00 scheduler registration."""
from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from src.api.main import app, build_scheduler
from src.orchestrator.weekly_summary import build_weekly_summary
from src.shared.config import Settings
from src.shared.db import InMemoryDB, store
from src.shared.schema import ActionItem, Meeting
from src.tools.notify.base import BaseNotifyTool
from src.tools.notify.mock import MockNotifyTool

client = TestClient(app)
NOW = datetime(2026, 10, 23, 12, 0, 0)  # a Friday; window = 10-17 .. 10-23


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


def _item(iid, title, due, status, owner="李四"):
    return ActionItem(id=iid, title=title, owner_id="u-1", owner_raw=owner,
                      due_date=due, source_segment_ids=[1], status=status)


def _two_meeting_db():
    db = InMemoryDB()
    db.save(Meeting(id="w-1", title="支付联调会", action_items=[
        _item("a1", "续期证书", "2026-10-20", "dispatched"),       # due in window
        _item("a2", "交联调报告", "2026-10-10", "dispatched", "王五"),  # overdue
        _item("a3", "整理会议文档", None, "confirmed", "张三"),      # undated open
        _item("a4", "下月规划", "2026-10-30", "dispatched"),        # future: excluded
    ]))
    db.save(Meeting(id="w-2", title="上线评审会", action_items=[
        _item("b1", "出灰度方案", "2026-10-21", "done", "王五"),    # done in window
        _item("b2", "旧事项收尾", "2026-10-05", "done"),            # done outside: excluded
        _item("b3", "确认值班表", "2026-10-23", "confirmed"),       # due today
    ]))
    return db


@pytest.fixture(autouse=True)
def _clean_store():
    store._data.clear()
    yield
    store._data.clear()


def test_aggregates_across_meetings_with_window_buckets():
    tool = RecordingNotifyTool()
    result = build_weekly_summary(_two_meeting_db(), NOW, window_days=7, notify_tool=tool)
    assert result["period_start"] == "2026-10-17"
    assert result["period_end"] == "2026-10-23"
    summary = result["summary"]
    # tracker's own body format, items prefixed with their meeting title
    assert "本周未完成任务：" in summary
    assert "《支付联调会》续期证书" in summary
    assert "《支付联调会》交联调报告" in summary
    assert "《支付联调会》整理会议文档" in summary
    assert "《上线评审会》确认值班表" in summary
    # out-of-window items do not appear at all
    assert "下月规划" not in summary
    assert "旧事项收尾" not in summary
    # bucket counts: due-open a1+b3=2, overdue a2=1, undated a3=1, done b1=1
    assert "本周到期未完成 2 项" in summary
    assert "逾期未完成 1 项" in summary
    assert "未定日期仍开放 1 项" in summary
    assert "本周已完成 1 项" in summary
    # completed items get their own section in the same line format
    assert "本周已完成任务：" in summary
    assert "《上线评审会》出灰度方案" in summary
    # delivery: the sent content is exactly the returned summary
    assert result["delivered"] is True
    assert tool.sends and tool.sends[0]["content"] == summary


def test_window_days_changes_the_window():
    result = build_weekly_summary(_two_meeting_db(), NOW, window_days=1,
                                  notify_tool=RecordingNotifyTool())
    assert result["period_start"] == "2026-10-23" == result["period_end"]
    # a1 (due 10-20, open) slides from "due this window" into "overdue"
    assert "本周到期未完成 1 项" in result["summary"]  # only b3
    assert "逾期未完成 2 项" in result["summary"]      # a1 + a2
    assert "本周已完成 0 项" in result["summary"]      # b1 due 10-21 now outside


def test_delivery_reporting_is_honest():
    db = _two_meeting_db()
    result = build_weekly_summary(db, NOW, notify_tool=MockNotifyTool())
    assert result["delivered"] is False and "mock" in result["delivery_note"]
    assert "任务周报" in result["summary"]  # summary still produced in full

    result = build_weekly_summary(db, NOW, notify_tool=None)
    assert result["delivered"] is False
    assert "not sent" in result["delivery_note"]
    assert "任务周报" in result["summary"]

    result = build_weekly_summary(db, NOW, notify_tool=FailingNotifyTool())
    assert result["delivered"] is False
    assert "RuntimeError" in result["delivery_note"]
    assert "任务周报" in result["summary"]


def test_empty_store_summary():
    result = build_weekly_summary(InMemoryDB(), NOW, notify_tool=RecordingNotifyTool())
    assert "本周到期未完成 0 项" in result["summary"]
    assert "本周无未完成任务。" in result["summary"]  # tracker wording, verbatim
    assert result["delivered"] is True


def test_stored_items_are_not_mutated():
    db = _two_meeting_db()
    build_weekly_summary(db, NOW, notify_tool=RecordingNotifyTool())
    titles = [i.title for m in db.list_all() for i in m.action_items]
    assert "续期证书" in titles and not any("《" in t for t in titles)


# ------------------------------------------------------------------- API
def test_api_weekly_summary_run():
    today = date.today()
    store.save(Meeting(id="w-api-1", title="API 周报会", action_items=[
        _item("w-api-item", "今天到期事项", today.isoformat(), "dispatched"),
    ]))
    r = client.post("/weekly-summary/run")
    assert r.status_code == 200
    body = r.json()
    assert body["period_end"] == today.isoformat()
    assert (date.fromisoformat(body["period_end"])
            - date.fromisoformat(body["period_start"])).days == 6  # default 7-day window
    assert "《API 周报会》今天到期事项" in body["summary"]  # full text returned
    assert body["delivered"] is False  # default NOTIFY_PROVIDER=mock
    assert "mock" in body["delivery_note"]
    r = client.post("/weekly-summary/run?window_days=3")
    body = r.json()
    assert (date.fromisoformat(body["period_end"])
            - date.fromisoformat(body["period_start"])).days == 2


# --------------------------------------------------------------- scheduler
def test_weekly_summary_scheduler_registration():
    assert build_scheduler(Settings(weekly_summary_enabled=False,
                                    reminders_enabled=False,
                                    gate_timeouts_enabled=False)) is None
    sched = build_scheduler(Settings(weekly_summary_enabled=True))
    jobs = {j.id: j for j in sched.get_jobs()}
    assert list(jobs) == ["weekly-summary"]
    trigger = str(jobs["weekly-summary"].trigger)
    assert "mon" in trigger and "9" in trigger  # Mondays 09:00
