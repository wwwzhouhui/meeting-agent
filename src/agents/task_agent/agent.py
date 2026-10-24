"""Task Agent (stateless): Meeting -> action_items extracted + assigned.
Dispatch happens only after the tasks gate (see orchestrator + tracker).
"""
from __future__ import annotations

from src.shared.schema import Meeting

from .assigner import Assigner
from .extractor import ActionItemExtractor
from .tracker import TaskTracker


class TaskAgent:
    def __init__(self, extractor: ActionItemExtractor | None = None,
                 assigner: Assigner | None = None, tracker: TaskTracker | None = None):
        self.extractor = extractor or ActionItemExtractor()
        self.assigner = assigner or Assigner()
        if tracker is None:
            # TASK_PROVIDER env decides the dispatch target (mock -> None).
            from src.tools.factory import build_task_tool

            tracker = TaskTracker(task_tool=build_task_tool())
        self.tracker = tracker

    def run(self, meeting: Meeting) -> Meeting:
        items = self.extractor.extract(meeting)
        # Surface extractor notes (e.g. scheduling items filtered out
        # by the safety net) on the meeting's human-visible warnings.
        for note in getattr(self.extractor, "extraction_notes", None) or []:
            meeting.warnings.append(note)
        items = self.assigner.assign(items, meeting.participants)
        # enforce evidence red line against the real transcript
        valid = meeting.valid_segment_ids()
        meeting.action_items = [
            i for i in items if i.source_segment_ids and all(s in valid for s in i.source_segment_ids)
        ]
        # extraction provenance (set by the extractor; "rules" for stubs)
        meeting.tasks_generation_source = getattr(
            self.extractor, "generation_source", None) or "rules"
        meeting.tasks_generation_model = getattr(
            self.extractor, "generation_model", None)
        meeting.status = "tasks_draft"
        return meeting

    def dispatch_confirmed(self, meeting: Meeting) -> Meeting:
        for item in meeting.action_items:
            if item.status == "pending_confirmation":
                item.status = "confirmed"
        meeting.action_items = self.tracker.dispatch(meeting.action_items)
        return meeting
