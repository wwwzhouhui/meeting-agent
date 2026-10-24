"""Calendar Agent family: intent / date parser integration / availability /
scheduler / agent.propose / prepare_brief.

Note on scope: prepare_brief() itself is unit-tested here; its wiring
into MeetingFlow + the API (POST/GET /meetings/{id}/brief) is covered by
tests/test_brief.py — see AUDIT.md round 2.
"""
from datetime import date, datetime, time

from src.agents.calendar_agent.agent import CalendarAgent
from src.agents.calendar_agent.availability import AvailabilityChecker
from src.agents.calendar_agent.intent import classify_intent
from src.agents.calendar_agent.scheduler import Scheduler
from src.tools.calendar.feishu import FeishuCalendarTool
from src.shared.schema import ActionItem, Decision, Meeting

BASE = date(2026, 10, 23)  # Friday


# ------------------------------------------------------------- intent
def test_intent_cancel():
    assert classify_intent("这场会议取消吧") == "cancel"


def test_intent_reschedule_variants():
    assert classify_intent("会议改期到下周") == "reschedule"
    assert classify_intent("把会议推迟到周五") == "reschedule"
    assert classify_intent("咱们改到周三吧") == "reschedule"


def test_intent_postpone_without_meeting_is_not_reschedule():
    # Locks the merged-branch semantics: 推迟到 alone (no 会议) must not
    # be classified as reschedule (e.g. "上线推迟到11月8日" is a decision).
    assert classify_intent("上线推迟到11月8日") == "unknown"


def test_intent_availability_followup_schedule_unknown():
    assert classify_intent("你明天有空吗") == "query_availability"
    assert classify_intent("下次开会我们再对一次") == "follow_up"
    assert classify_intent("安排会议讨论一下方案") == "schedule"
    assert classify_intent("今天天气不错") == "unknown"


# --------------------------------------------------------- availability
def test_propose_slots_preferred_date_and_hours():
    slots = AvailabilityChecker().propose_slots(date(2026, 10, 26), BASE, count=3)
    assert slots == ["2026-10-26T10:00:00", "2026-10-26T14:00:00", "2026-10-26T16:00:00"]


def test_propose_slots_skips_busy_and_weekends():
    busy = {datetime.combine(date(2026, 10, 26), time(10, 0)).isoformat()}
    slots = AvailabilityChecker(busy=busy).propose_slots(date(2026, 10, 26), BASE, count=3)
    assert slots[0] == "2026-10-26T14:00:00"
    # preferred on a Saturday -> first slot is the next Monday
    slots = AvailabilityChecker().propose_slots(date(2026, 10, 24), BASE, count=1)
    assert slots[0].startswith("2026-10-26")


def test_propose_slots_default_is_next_workday():
    slots = AvailabilityChecker().propose_slots(None, BASE, count=1)
    assert slots[0].startswith("2026-10-26")  # Sat/Sun skipped after Friday base


# ------------------------------------------------------------ scheduler
def test_scheduler_without_tool_gives_mock_id(sample_meeting):
    m = Scheduler().create_follow_up(sample_meeting, "2026-10-27T10:00:00")
    assert m.calendar_event_id == f"mock-cal-{sample_meeting.id}"


def test_scheduler_configured_tool_failure_warns_not_crashes(sample_meeting):
    tool = FeishuCalendarTool(token="configured")  # raises NotImplementedError
    m = Scheduler(tool=tool).create_follow_up(sample_meeting, "2026-10-27T10:00:00")
    assert m.calendar_event_id is None
    assert any("create_event failed" in w for w in m.warnings)


# ---------------------------------------------------------------- agent
def test_agent_propose_uses_transcript_followup(sample_meeting):
    candidates = CalendarAgent().propose(sample_meeting)
    assert candidates and all(c.startswith("2026-10-27") for c in candidates)


def test_agent_run_sets_candidates(sample_meeting):
    m = CalendarAgent().run(sample_meeting)
    assert len(m.follow_up_candidate_times) == 3


def test_prepare_brief_includes_decisions_and_pending_only():
    history = Meeting(
        id="h1", title="上次会",
        decisions=[Decision(text="上线推迟至11月8日", segment_ids=[1])],
        action_items=[
            ActionItem(id="a1", title="完成证书续期", owner_raw="李四",
                       due_date="2026-10-28", source_segment_ids=[1], status="dispatched"),
            ActionItem(id="a2", title="已完成事项", owner_raw="王五",
                       source_segment_ids=[1], status="done"),
        ],
    )
    brief = CalendarAgent().prepare_brief(Meeting(id="n1", title="跟进会"), history=[history])
    assert "上线推迟至11月8日" in brief
    assert "完成证书续期" in brief
    assert "已完成事项" not in brief  # done items are not carried over
    assert "建议议程" in brief
