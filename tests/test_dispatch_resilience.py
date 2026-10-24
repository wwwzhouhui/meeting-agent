"""Dispatch resilience + factory wiring + empty-transcript contract.

A configured-but-unimplemented external provider must degrade to
per-item failures / warnings — never crash the whole flow.
"""
import copy

from src.agents.minutes.agent import MinutesAgent
from src.agents.task_agent.agent import TaskAgent
from src.agents.task_agent.tracker import TaskTracker
from src.orchestrator.flow import MeetingFlow
from src.shared.config import Settings
from src.shared.schema import ActionItem, Meeting
from src.tools.calendar.google import GoogleCalendarTool
from src.tools.calendar.outlook import OutlookCalendarTool
from src.tools.factory import build_calendar_tool, build_task_tool
from src.tools.tasks.linear import LinearTaskTool
from src.tools.tasks.base import BaseTaskTool


class FlakyTool(BaseTaskTool):
    system = "flaky"

    def create_task(self, title, owner_id=None, due_date=None, idempotency_key=""):
        if "证书" in title:
            raise NotImplementedError("TODO: real API call")
        return {"id": f"flaky-{idempotency_key}", "system": self.system}

    def update_task(self, task_id, **fields):
        return {"id": task_id}

    def comment_task(self, task_id, text):
        return {"id": task_id}


def _item(item_id, title, seg):
    return ActionItem(id=item_id, title=title, owner_raw="李四",
                      source_segment_ids=[seg], idempotency_key=f"m:{seg}",
                      status="confirmed")


def test_dispatch_one_failure_does_not_block_others():
    items = [_item("t1", "完成证书续期", 1), _item("t2", "提交灰度方案", 2)]
    out = TaskTracker(task_tool=FlakyTool()).dispatch(items)
    by_id = {i.id: i for i in out}
    assert by_id["t1"].status == "dispatch_failed"
    assert "NotImplementedError" in (by_id["t1"].dispatch_error or "")
    assert by_id["t1"].external_task_id is None
    assert by_id["t2"].status == "dispatched"
    assert by_id["t2"].external_task_id == "flaky-m:2"


def test_dispatch_configured_linear_tool_marks_all_failed_not_crashes():
    tool = LinearTaskTool(token="configured-key")
    out = TaskTracker(task_tool=tool).dispatch([_item("t1", "任意任务", 1)])
    assert out[0].status == "dispatch_failed"
    assert "NotImplementedError" in (out[0].dispatch_error or "")


def test_flow_with_failing_tool_ends_dispatch_failed_not_exception(sample_meeting):
    agent = TaskAgent(tracker=TaskTracker(task_tool=LinearTaskTool(token="x")))
    m = MeetingFlow(task_agent=agent).run(
        copy.deepcopy(sample_meeting), auto_approve_minutes=True, auto_approve_tasks=True)
    assert m.status == "dispatch_failed"
    assert m.action_items and all(i.status == "dispatch_failed" for i in m.action_items)
    assert any("failed to dispatch" in w or "dispatch" in w for w in m.warnings)


# ---------------------------------------------------------------- factory
def test_factory_mock_and_unknown_return_none():
    assert build_task_tool(Settings(task_provider="mock")) is None
    assert build_task_tool(Settings(task_provider="no-such")) is None
    assert build_calendar_tool(Settings(calendar_provider="mock")) is None


def test_factory_builds_configured_tools():
    tool = build_task_tool(Settings(task_provider="linear", linear_api_key="k"))
    assert isinstance(tool, LinearTaskTool) and tool.token == "k"
    cal = build_calendar_tool(Settings(calendar_provider="google", google_calendar_token="t"))
    assert isinstance(cal, GoogleCalendarTool) and cal.token == "t"
    out = build_calendar_tool(Settings(calendar_provider="outlook", outlook_token="t2"))
    assert isinstance(out, OutlookCalendarTool) and out.system == "outlook"


def test_factory_env_wiring_reaches_flow(monkeypatch, sample_meeting):
    monkeypatch.setenv("TASK_PROVIDER", "linear")
    monkeypatch.setenv("LINEAR_API_KEY", "configured")
    monkeypatch.setenv("LLM_MODE", "mock")
    m = MeetingFlow().run(copy.deepcopy(sample_meeting),
                          auto_approve_minutes=True, auto_approve_tasks=True)
    # configured-but-unimplemented Linear: flow completes, items failed, no crash
    assert m.status == "dispatch_failed"
    assert all(i.dispatch_error for i in m.action_items)


# ------------------------------------------------------------- tracker
def test_tracker_overdue_and_reminders_and_weekly():
    from datetime import date as _date

    today = _date(2026, 10, 23)
    overdue = _item("x1", "过期任务", 1)
    overdue.due_date = "2026-10-20"
    overdue.status = "dispatched"
    soon = _item("x2", "明天到期", 2)
    soon.due_date = "2026-10-24"
    soon.status = "dispatched"
    done = _item("x3", "已完成", 3)
    done.due_date = "2026-10-01"
    done.status = "done"
    items = [overdue, soon, done]
    out = TaskTracker.check_overdue(items, today=today)
    assert [i.id for i in out] == ["x1"] and overdue.status == "overdue"
    assert done.status == "done"  # done items never become overdue
    assert [i.id for i in TaskTracker.reminders_due(items, today=today)] == ["x2"]
    summary = TaskTracker.weekly_summary(items)
    assert "过期任务" in summary and "已完成" not in summary
    assert TaskTracker.weekly_summary([done]) == "本周无未完成任务。"


# ------------------------------------------------------- empty transcript
def test_minutes_agent_empty_transcript_needs_transcript():
    meeting = Meeting(id="audio-only", title="只有录音的会", audio_url="oss://x/meeting.mp3")
    m = MinutesAgent().run(meeting)
    assert m.status == "needs_transcript"
    assert m.minutes is None
    assert m.decisions == []
    assert any("transcript is empty" in w for w in m.warnings)


def test_flow_stops_at_needs_transcript():
    meeting = Meeting(id="audio-only-2", title="只有录音的会", audio_url="oss://x/a.mp3")
    m = MeetingFlow().run(meeting, auto_approve_minutes=True, auto_approve_tasks=True)
    assert m.status == "needs_transcript"
    assert m.action_items == []
