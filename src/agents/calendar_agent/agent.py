"""Calendar Agent (stateless): pre-meeting brief + post-meeting follow-up
candidate times. Never creates an event on its own -- see Scheduler+gate.
"""
from __future__ import annotations

from src.shared.schema import Meeting

from .availability import AvailabilityChecker
from .date_parser import parse_relative_date
from .intent import classify_intent
from .scheduler import Scheduler

FOLLOW_UP_HINTS = ("下次", "再对一次", "跟进会", "再开", "后续会议")


class CalendarAgent:
    def __init__(self, checker: AvailabilityChecker | None = None, scheduler: Scheduler | None = None):
        self.checker = checker or AvailabilityChecker()
        if scheduler is None:
            # CALENDAR_PROVIDER env decides the calendar target (mock -> None).
            from src.tools.factory import build_calendar_tool

            scheduler = Scheduler(tool=build_calendar_tool())
        self.scheduler = scheduler

    def run(self, meeting: Meeting) -> Meeting:
        meeting.follow_up_candidate_times = self.propose(meeting)
        return meeting

    def propose(self, meeting: Meeting, text: str | None = None) -> list[str]:
        base = meeting.base_date()
        preferred = None
        # Prefer an explicit follow-up utterance from the transcript.
        sources = [text] if text else []
        sources += [s.text for s in meeting.transcript]
        for src in sources:
            if not src:
                continue
            if classify_intent(src) in ("follow_up", "schedule") or any(h in src for h in FOLLOW_UP_HINTS):
                d = parse_relative_date(src, base)
                if d:
                    preferred = d
                    break
        return self.checker.propose_slots(preferred, base, count=3, timezone=meeting.timezone)

    def prepare_brief(self, meeting: Meeting, history: list[Meeting] | None = None) -> str:
        lines = [f"# 会前 Brief：{meeting.title}"]
        if meeting.start_at:
            lines.append(f"时间：{meeting.start_at.isoformat()}（{meeting.timezone}）")
        if meeting.participants:
            lines.append("参会人：" + "、".join(
                p.name + (f"（{p.role}）" if p.role else "") for p in meeting.participants))
        for h in history or []:
            if h.decisions:
                lines.append("## 上次决策")
                lines += [f"- {d.text}" for d in h.decisions]
            pending = [a for a in h.action_items if a.status not in ("done",)]
            if pending:
                lines.append("## 遗留任务")
                lines += [f"- [{a.owner_raw}] {a.title}（{a.due_date or '无截止'}）" for a in pending]
        lines.append("## 建议议程")
        lines.append("- 复盘遗留任务\n- 本次议题讨论\n- 确认下一步与负责人")
        return "\n".join(lines)

    def schedule_follow_up(self, meeting: Meeting, chosen_time: str) -> Meeting:
        return self.scheduler.create_follow_up(meeting, chosen_time)
