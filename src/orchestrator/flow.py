"""Orchestrator: deterministic flow, pure Python.

Flow: start -> minutes -> gate1 -> tasks -> gate2 -> calendar propose
      -> dispatch (only after gate2)

Event bus: stage transitions are published on an in-process EventBus
(src/orchestrator/eventbus.py) and the two cross-agent handoffs are
driven by subscriptions registered in __init__:

  minutes.confirmed  -> TaskAgent extraction   (tasks.extracted)
  tasks.confirmed    -> CalendarAgent propose  (schedule.proposed)
                     -> TaskAgent dispatch     (tasks.dispatched /
                                               dispatch.failed)

The two handoff subscriptions are CRITICAL (see eventbus.py): if the
downstream agent raises, the failure is written to meeting.warnings
and the exception propagates to whoever confirmed the gate — the
confirmation flags (meeting.minutes_confirmed / tasks_confirmed) are
only set AFTER the handoff emit returns, so a failed handoff leaves
the meeting un-confirmed and the confirm can simply be retried.
Plain audit subscribers stay non-critical and isolated.

Every event is also appended to meeting.events (the audit log) by
EventBus.emit(). Stages that stay inside one agent (transcription
inside MinutesAgent.run, assignment inside TaskAgent.run) remain
direct calls — the bus carries the pipeline's lifecycle, not
intra-agent internals. See AUDIT.md round 8 for the exact split.

Orchestration config (src/shared/orchestration.py): pipeline step
switches are honoured by the step methods / run(); a step that is off
is skipped WITH a warning trace, never silently. A gate whose
enabled=false auto-passes WITH a warning + a minutes/tasks.confirmed
event whose payload says via=config_gate_disabled — it is never
mistaken for a human confirmation.

Optional CrewAI path: if crewai is installed you can mirror these steps
with @start/@listen on a crewai.flow.flow.Flow subclass; the agents in
src/agents/* are framework-free, so only this file would change.
LangGraph migration: each step_* below becomes a graph node.
"""
from __future__ import annotations

import logging
from collections import OrderedDict
from datetime import datetime, timezone

from src.agents.calendar_agent.agent import CalendarAgent
from src.agents.minutes.agent import MinutesAgent
from src.agents.task_agent.agent import TaskAgent
from src.shared.orchestration import Orchestration, load_orchestration
from src.shared.schema import BriefResult, Meeting, Rejection

from .eventbus import Event, EventBus
from .gates import ConfirmationGate

logger = logging.getLogger(__name__)


def _title_bigrams(title: str) -> set[str]:
    t = "".join((title or "").split())
    return {t[i:i + 2] for i in range(len(t) - 1)}


def related_history(meeting: Meeting, all_meetings: list[Meeting]) -> list[Meeting]:
    """History meetings relevant to `meeting`: sharing at least one
    participant (by user_id) or a title topic (shared bigram). Only used
    to feed the pre-meeting brief — nothing is invented, meetings that
    are not in the store simply do not appear."""
    my_people = {p.user_id for p in meeting.participants}
    my_topic = _title_bigrams(meeting.title)
    out = []
    for h in all_meetings:
        if h.id == meeting.id:
            continue
        if my_people & {p.user_id for p in h.participants} or my_topic & _title_bigrams(h.title):
            out.append(h)
    return out


def stamp_gate_entry(meeting: Meeting, kind: str) -> None:
    """Record that `meeting` just entered the `kind` confirmation gate
    (kind: "minutes" | "tasks"): the gate-timeout clock restarts and the
    one-shot escalation marker re-arms. Called by the flow when a run
    stops at a gate and by the API paths that land a meeting in an
    awaiting_* state (confirm-minutes, reextract-tasks)."""
    now = datetime.now(timezone.utc)
    if kind == "minutes":
        meeting.minutes_gate_entered_at = now
        meeting.minutes_gate_timeout_notified_at = None
    elif kind == "tasks":
        meeting.tasks_gate_entered_at = now
        meeting.tasks_gate_timeout_notified_at = None
    else:
        raise ValueError(f"unknown gate kind {kind!r}")


class MeetingFlow:
    def __init__(self, minutes_agent: MinutesAgent | None = None,
                 task_agent: TaskAgent | None = None,
                 calendar_agent: CalendarAgent | None = None,
                 gate: ConfirmationGate | None = None,
                 bus: EventBus | None = None,
                 orchestration: Orchestration | None = None):
        self.minutes_agent = minutes_agent or MinutesAgent()
        self.task_agent = task_agent or TaskAgent()
        self.calendar_agent = calendar_agent or CalendarAgent()
        self.gate = gate or ConfirmationGate(auto_approve=False)
        self.bus = bus or EventBus()
        self.orchestration = orchestration or load_orchestration()
        # meeting_id -> the live Meeting object, registered on every
        # emit so bus handlers (which only receive the Event) can find
        # the meeting they must advance. BOUNDED on purpose: the API
        # keeps one flow for the process lifetime, so an unbounded
        # registry would pin every meeting ever touched in memory.
        # Oldest entries are evicted past _KNOWN_LIMIT, and a meeting
        # is dropped explicitly once it reaches its terminal dispatch
        # (see _on_tasks_confirmed) — any later emit re-registers it,
        # so eviction can never lose a meeting, only a shortcut to it.
        self._known: OrderedDict[str, Meeting] = OrderedDict()
        # The two cross-agent handoffs are load-bearing: register them
        # as CRITICAL subscribers so a downstream failure propagates
        # to the confirmer instead of being swallowed by the bus.
        self.bus.subscribe("minutes.confirmed", self._on_minutes_confirmed,
                           critical=True)
        self.bus.subscribe("tasks.confirmed", self._on_tasks_confirmed,
                           critical=True)

    # Upper bound for the _known registry (see __init__).
    _KNOWN_LIMIT = 512

    # -- event plumbing -----------------------------------------------------
    def _emit(self, meeting: Meeting, event_type: str,
              payload: dict | None = None) -> Event:
        self._known[meeting.id] = meeting
        self._known.move_to_end(meeting.id)
        while len(self._known) > self._KNOWN_LIMIT:
            self._known.popitem(last=False)  # evict least recently used
        return self.bus.emit(meeting, event_type, payload)

    def _on_minutes_confirmed(self, event: Event) -> None:
        """Bus handoff #1 (CRITICAL): minutes confirmed -> TaskAgent
        extracts. A failure is recorded on the meeting's warnings and
        re-raised, so the confirmation is never stamped over missing
        tasks (the caller leaves minutes_confirmed unset -> retryable)."""
        meeting = self._known.get(event.meeting_id)
        if meeting is None:
            logger.warning("minutes.confirmed for unknown meeting %s; ignored",
                           event.meeting_id)
            return
        if not self.orchestration.step_enabled("tasks"):
            logger.info("tasks step disabled; extraction skipped for %s", meeting.id)
            return
        try:
            self.step_tasks(meeting)
        except Exception as exc:
            meeting.warnings.append(
                "bus handoff failed: minutes.confirmed -> task extraction "
                f"raised {type(exc).__name__}: {exc}; the minutes "
                "confirmation was NOT recorded — fix the cause and "
                "confirm again to retry")
            raise

    def _on_tasks_confirmed(self, event: Event) -> None:
        """Bus handoff #2 (CRITICAL): tasks confirmed -> CalendarAgent
        proposes, then TaskAgent dispatches (unless the dispatch step
        is off, in which case the meeting stops at tasks_confirmed,
        with a trace). A failure is recorded on the meeting's warnings
        and re-raised, mirroring handoff #1."""
        meeting = self._known.get(event.meeting_id)
        if meeting is None:
            logger.warning("tasks.confirmed for unknown meeting %s; ignored",
                           event.meeting_id)
            return
        try:
            self.step_calendar(meeting)
            if not self.orchestration.step_enabled("dispatch"):
                meeting.status = "tasks_confirmed"
                meeting.warnings.append(
                    "dispatch step disabled in orchestration config; meeting "
                    "stopped at tasks_confirmed — nothing was dispatched")
                return
            self.step_dispatch(meeting)
        except Exception as exc:
            meeting.warnings.append(
                "bus handoff failed: tasks.confirmed -> calendar proposal "
                f"/ task dispatch raised {type(exc).__name__}: {exc}; the "
                "tasks confirmation was NOT recorded — fix the cause and "
                "confirm again to retry")
            raise
        # Terminal: tasks.dispatched has been emitted (inside
        # step_dispatch) — the flow no longer needs the live object.
        # Any later emit for this meeting re-registers it.
        self._known.pop(meeting.id, None)

    # -- pre-meeting step (separate phase from the post-meeting run()) ----
    def prepare_brief(self, meeting: Meeting,
                      history: list[Meeting] | None = None) -> Meeting:
        """Generate (or regenerate) the pre-meeting brief and store it on
        meeting.brief. Callable in ANY meeting status — it is pre-meeting
        material. History defaults to every meeting in the shared store,
        filtered to related ones (shared participant or shared topic);
        regenerating overwrites the old brief and refreshes the timestamp.
        """
        if not self.orchestration.step_enabled("brief"):
            meeting.warnings.append(
                "brief step disabled in orchestration config; brief not generated")
            return meeting
        if history is None:
            from src.shared.db import store

            history = store.list_all()
        related = related_history(meeting, history)
        content = self.calendar_agent.prepare_brief(meeting, history=related)
        meeting.brief = BriefResult(
            content=content,
            generated_at=datetime.now(timezone.utc),
            source_meeting_ids=[h.id for h in related],
        )
        self._emit(meeting, "brief.generated",
                   {"related_meetings": len(related)})
        return meeting

    # -- individual steps (LangGraph-node shaped) -------------------------
    def step_minutes(self, meeting: Meeting) -> Meeting:
        had_transcript = bool(meeting.transcript)
        if not self.orchestration.step_enabled("transcribe") and not had_transcript:
            meeting.status = "needs_transcript"
            meeting.warnings.append(
                "transcribe step disabled in orchestration config; ASR "
                "skipped and no transcript was provided")
            return meeting
        meeting.status = "processing"
        meeting = self.minutes_agent.run(meeting)
        if not had_transcript and meeting.transcript:
            self._emit(meeting, "transcript.ready", {
                "segments": len(meeting.transcript),
                "engine": getattr(self.minutes_agent.transcriber, "engine", "mock"),
                "via": "asr",
            })
        if meeting.minutes is not None:
            self._emit(meeting, "minutes.generated", {
                "decisions": len(meeting.decisions),
                "risks": len(meeting.risks),
                "open_questions": len(meeting.open_questions),
                "source": meeting.generation_source,
            })
        return meeting

    def step_tasks(self, meeting: Meeting) -> Meeting:
        meeting = self.task_agent.run(meeting)
        self._emit(meeting, "tasks.extracted", {
            "count": len(meeting.action_items),
            "source": meeting.tasks_generation_source,
        })
        return meeting

    def step_calendar(self, meeting: Meeting) -> Meeting:
        if not self.orchestration.step_enabled("schedule"):
            return meeting
        meeting = self.calendar_agent.run(meeting)
        self._emit(meeting, "schedule.proposed", {
            "candidates": len(meeting.follow_up_candidate_times),
            "first": meeting.follow_up_candidate_times[0]
                     if meeting.follow_up_candidate_times else None,
        })
        return meeting

    def step_dispatch(self, meeting: Meeting) -> Meeting:
        meeting = self.task_agent.dispatch_confirmed(meeting)
        failed = [i for i in meeting.action_items if i.status == "dispatch_failed"]
        if failed and any(i.external_task_id for i in meeting.action_items):
            meeting.status = "dispatched_with_failures"
            meeting.warnings.append(
                f"{len(failed)} task(s) failed to dispatch: "
                + "; ".join(f"{i.id}: {i.dispatch_error}" for i in failed))
        elif failed:
            meeting.status = "dispatch_failed"
            meeting.warnings.append(
                "all task dispatches failed: "
                + "; ".join(f"{i.id}: {i.dispatch_error}" for i in failed))
        else:
            meeting.status = "dispatched"
        for item in failed:
            self._emit(meeting, "dispatch.failed",
                       {"item_id": item.id, "error": item.dispatch_error})
        self._emit(meeting, "tasks.dispatched", {
            "status": meeting.status,
            "dispatched": sum(1 for i in meeting.action_items if i.external_task_id),
            "failed": len(failed),
        })
        return meeting

    # -- gate transitions (shared by run() and the API confirm paths) -----
    def _gate_decision(self, meeting: Meeting, kind: str,
                       payload: str) -> tuple[bool, str]:
        """(approved, via) for gate `kind`. A config-disabled gate
        auto-passes but ALWAYS leaves a trace — a warning here and
        via="config_gate_disabled" on the confirmed event — so it can
        never be mistaken for a human confirmation."""
        if not self.orchestration.gate(kind).enabled:
            meeting.warnings.append(
                f"{kind} confirmation gate disabled in orchestration "
                "config — auto-passed without human confirmation "
                "(this trace is the record of that choice)")
            return True, "config_gate_disabled"
        return self.gate.request(kind, meeting.id, payload=payload), "approved"

    def confirm_minutes(self, meeting: Meeting) -> Meeting:
        """Gate1 confirmed (API path): publish minutes.confirmed — the
        bus handler extracts the tasks — and only AFTER that handoff
        succeeds record the confirmation; then land in
        awaiting_tasks_confirmation with a fresh gate stamp. If the
        critical handoff raises, the exception propagates and
        minutes_confirmed stays False, so the endpoint's idempotency
        early-return cannot block a retry. With the tasks step
        disabled in the orchestration config there is no gate2 to
        await: the meeting lands in the dedicated status
        "minutes_confirmed" (flag and status agree) instead of
        pretending gate2 is pending."""
        self.gate.confirm("minutes", meeting.id)
        self._emit(meeting, "minutes.confirmed", {"via": "api"})
        meeting.minutes_confirmed = True
        if not self.orchestration.step_enabled("tasks"):
            meeting.status = "minutes_confirmed"
            meeting.warnings.append(
                "tasks step disabled in orchestration config; tasks "
                "were not extracted after minutes confirmation — the "
                "meeting stops at status minutes_confirmed")
            return meeting
        meeting.status = "awaiting_tasks_confirmation"
        stamp_gate_entry(meeting, "tasks")
        self._emit(meeting, "gate.tasks.entered", {"gate": "tasks"})
        return meeting

    def confirm_tasks(self, meeting: Meeting) -> Meeting:
        """Gate2 confirmed (API path): publish tasks.confirmed — the
        bus handler proposes follow-up times and dispatches — and only
        record tasks_confirmed after the handoff succeeds (see
        confirm_minutes for why the flag comes after the emit)."""
        self.gate.confirm("tasks", meeting.id)
        self._emit(meeting, "tasks.confirmed", {"via": "api"})
        meeting.tasks_confirmed = True
        return meeting

    def reject_minutes(self, meeting: Meeting, reason: str = "") -> Meeting:
        self.gate.reject_minutes(meeting.id, reason)
        meeting.minutes_confirmed = False
        meeting.rejections.append(Rejection(
            gate="minutes", reason=reason, at=datetime.now(timezone.utc)))
        meeting.status = "minutes_rejected"
        self._emit(meeting, "minutes.rejected", {"reason": reason})
        return meeting

    def reject_tasks(self, meeting: Meeting, reason: str = "") -> Meeting:
        self.gate.reject_tasks(meeting.id, reason)
        meeting.tasks_confirmed = False
        meeting.rejections.append(Rejection(
            gate="tasks", reason=reason, at=datetime.now(timezone.utc)))
        meeting.status = "tasks_rejected"
        self._emit(meeting, "tasks.rejected", {"reason": reason})
        return meeting

    # -- full run -----------------------------------------------------------
    def run(self, meeting: Meeting,
            auto_approve_minutes: bool = False,
            auto_approve_tasks: bool = False) -> Meeting:
        if auto_approve_minutes:
            self.gate.confirm("minutes", meeting.id)
        if auto_approve_tasks:
            self.gate.confirm("tasks", meeting.id)

        if not any(e.type == "meeting.created" for e in meeting.events):
            self._emit(meeting, "meeting.created",
                       {"title": meeting.title, "via": "flow"})

        if not self.orchestration.step_enabled("minutes"):
            meeting.warnings.append(
                "minutes step disabled in orchestration config; minutes "
                "were not generated")
            return meeting

        meeting = self.step_minutes(meeting)

        if meeting.status == "needs_transcript":
            # No transcript -> no minutes -> nothing to confirm or extract.
            return meeting

        approved, via = self._gate_decision(
            meeting, "minutes", meeting.executive_summary or "")
        if not approved:
            meeting.status = "awaiting_minutes_confirmation"
            stamp_gate_entry(meeting, "minutes")
            self._emit(meeting, "gate.minutes.entered", {"gate": "minutes"})
            return meeting
        # Bus handoff: the subscribed handler extracts the tasks.
        # The confirmed flag is set only after the emit returns, so a
        # failed handoff leaves the meeting un-confirmed (retryable)
        # instead of stamped over missing tasks.
        self._emit(meeting, "minutes.confirmed", {"via": via})
        meeting.minutes_confirmed = True

        if not self.orchestration.step_enabled("tasks"):
            # Same terminal state as confirm_minutes: flag and status
            # agree that gate1 passed and there is no gate2 to await.
            meeting.status = "minutes_confirmed"
            meeting.warnings.append(
                "tasks step disabled in orchestration config; tasks "
                "were not extracted")
            return meeting

        approved, via = self._gate_decision(
            meeting, "tasks", f"{len(meeting.action_items)} tasks")
        if not approved:
            meeting.status = "awaiting_tasks_confirmation"
            stamp_gate_entry(meeting, "tasks")
            self._emit(meeting, "gate.tasks.entered", {"gate": "tasks"})
            return meeting
        # Bus handoff: the subscribed handler proposes + dispatches.
        self._emit(meeting, "tasks.confirmed", {"via": via})
        meeting.tasks_confirmed = True
        return meeting

    # alias used by the API's /process endpoint
    process = run
