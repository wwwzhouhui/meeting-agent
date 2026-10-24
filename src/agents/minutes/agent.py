"""Minutes Agent (stateless): transcript -> Meeting with minutes filled.
Does NOT create calendar events or tasks.

Empty-transcript contract: if there is no transcript (e.g. only an
audio_url and ASR is not configured), the agent does NOT fabricate
minutes. It sets ``meeting.status = "needs_transcript"``, appends a
warning to ``meeting.warnings`` and returns with ``minutes is None``;
the orchestrator stops the flow there.
"""
from __future__ import annotations

import logging

from src.shared.config import get_settings
from src.shared.schema import Meeting

from .analyst import NoteAnalyst
from .transcriber import Transcriber

logger = logging.getLogger(__name__)


class MinutesAgent:
    def __init__(self, transcriber: Transcriber | None = None,
                 analyst: NoteAnalyst | None = None, *, store=None):
        if transcriber is None:
            transcriber = Transcriber(engine=get_settings().asr_engine)
        self.transcriber = transcriber
        self.analyst = analyst or NoteAnalyst()
        # Store used ONLY to persist a cloud-ASR task id the moment it
        # is submitted (see _persist_asr_task_id). None -> the shared
        # store from src.shared.db, resolved lazily at callback time.
        self._store = store

    def _persist_asr_task_id(self, meeting: Meeting, task_id: str) -> None:
        """Write the cloud ASR task id onto the meeting AND save it
        immediately — not at the end of run(). A cloud transcription
        can poll for a long time; if the process dies meanwhile, the
        stored meeting still carries the id and the result can be
        looked up by id instead of being lost with the process.

        The save only happens when the meeting already exists in the
        store (the API path creates it before /process); in-memory
        callers (demo, unit tests) just get the attribute set, and the
        caller's own save persists it as before.
        """
        meeting.asr_task_id = task_id
        store = self._store
        if store is None:
            try:
                from src.shared.db import store as shared_store

                store = shared_store
            except Exception:
                return
        try:
            if store.get(meeting.id) is not None:
                store.save(meeting)
        except Exception:
            logger.exception(
                "failed to persist ASR task id %s for meeting %s", task_id, meeting.id)

    def run(self, meeting: Meeting) -> Meeting:
        if not meeting.transcript and meeting.audio_url:
            if hasattr(self.transcriber, "on_task_submitted"):
                self.transcriber.on_task_submitted = (
                    lambda task_id, m=meeting: self._persist_asr_task_id(m, task_id))
            meeting.transcript = self.transcriber.transcribe(meeting.audio_url)
            task_id = getattr(self.transcriber, "last_task_id", None)
            if task_id:
                meeting.asr_task_id = task_id
            note = getattr(self.transcriber, "last_note", None)
            if meeting.transcript and note:
                # e.g. ASR transcript without speaker diarization — a
                # human should know before trusting speaker attribution.
                meeting.warnings.append(note)
        if not meeting.transcript:
            warning = (
                "transcript is empty (ASR engine "
                f"'{self.transcriber.engine}' produced no segments); "
                "minutes were NOT generated — provide a transcript or configure ASR"
            )
            reason = getattr(self.transcriber, "last_error", None)
            if reason:
                warning += f" Reason: {reason}"
            logger.warning("meeting %s: %s", meeting.id, warning)
            meeting.warnings.append(warning)
            meeting.minutes = None
            meeting.status = "needs_transcript"
            return meeting
        result = self.analyst.analyze(
            meeting.transcript, title=meeting.title,
            participants=[p.name for p in meeting.participants],
        )
        meeting.minutes = result
        meeting.generation_source = result.generation_source
        meeting.generation_model = result.generation_model
        # Surface analyst quality notes (e.g. suspect decisions kept
        # after repair) on the meeting's human-visible warning channel.
        for note in result.generation_notes:
            meeting.warnings.append(note)
        meeting.summary = result.summary
        meeting.executive_summary = result.executive_summary
        meeting.detailed_minutes = result.detailed_minutes
        meeting.topics = result.topics
        meeting.decisions = result.decisions
        meeting.risks = result.risks
        meeting.open_questions = result.open_questions
        meeting.status = "minutes_draft"
        return meeting
