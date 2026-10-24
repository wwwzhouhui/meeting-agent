"""In-process synchronous event bus (message bus for the agents).

Why an in-process bus and not a broker (Redis/Kafka)? The three agents
run in one process against one shared Meeting state object; a broker
would add a deployment dependency and failure modes without buying
anything at this scale. What the bus DOES buy, and why the pipeline is
wired through it:

- Decoupling: the orchestrator publishes lifecycle events
  (``minutes.confirmed``, ``tasks.confirmed``, ...) and the agent that
  owns the next stage reacts via a subscription, instead of the flow
  hard-calling across agent boundaries. The two cross-agent handoffs —
  minutes-confirmed -> task extraction and tasks-confirmed -> calendar
  proposal + dispatch — are genuinely bus-driven (see flow.py).
- Audit: ``emit()`` appends every event to ``meeting.events`` before
  publishing, so each meeting carries its own ordered event timeline,
  persisted with the meeting (Postgres JSONB included).
- Two-track failure semantics (``subscribe(..., critical=...)``):
  * Non-critical subscribers (audit-style observers) are ISOLATED: a
    raising one never blocks the remaining subscribers or the
    publisher; the failure is logged and recorded in ``bus.errors``.
  * Critical subscribers — the load-bearing handoffs whose work the
    publisher's state transition DEPENDS on — are recorded in
    ``bus.errors`` too, but their exception then PROPAGATES to the
    publish()/emit() caller. Swallowing a handoff failure would let
    the flow mark a gate confirmed and move on with the downstream
    work silently missing, so the caller must see it and fail loudly
    instead (the confirmation stays un-recorded and can be retried).
  Note the ordering consequence: emit() appends the audit record
  BEFORE publishing, so when a critical handler raises, the event is
  still in meeting.events — correctly, because the event really was
  published; it is the downstream handoff that failed.

Payload discipline: payloads are summaries — counts, ids, short status
strings. Transcript text and credentials never enter a payload; emit()
additionally redacts any key that looks like a credential
(secret/token/password/api_key) as defence in depth.

Event types emitted by the current pipeline (only nodes that exist —
see flow.py / api/main.py for the exact emit points):

  meeting.created            brief.generated          transcript.ready
  minutes.generated          gate.minutes.entered     gate.tasks.entered
  minutes.confirmed          minutes.rejected
  tasks.extracted            tasks.confirmed          tasks.rejected
  schedule.proposed          tasks.dispatched         dispatch.failed
  reminders.sent             gate.timeout             weekly_summary.generated
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable

from pydantic import BaseModel, Field

from src.shared.schema import Meeting, MeetingEvent

logger = logging.getLogger(__name__)

Handler = Callable[["Event"], None]

_CREDENTIAL_KEY_PARTS = ("secret", "token", "password", "api_key", "apikey", "credential")


class Event(BaseModel):
    """One bus message. ``meeting_id`` is empty for process-wide
    events (e.g. weekly_summary.generated) that belong to no single
    meeting; those are published but not written to any meeting log."""

    type: str
    meeting_id: str = ""
    at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    payload: dict[str, Any] = Field(default_factory=dict)


def _sanitize(payload: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in (payload or {}).items():
        lowered = str(key).lower()
        if any(part in lowered for part in _CREDENTIAL_KEY_PARTS):
            out[key] = "***"
        else:
            out[key] = value
    return out


class EventBus:
    """Synchronous pub/sub. Handlers run in subscription order;
    ``"*"`` subscribers receive every event (after type-specific ones).

    Each subscription is either non-critical (default: failures are
    isolated, see publish()) or critical (failures propagate to the
    publisher after being recorded — for load-bearing handoffs).
    """

    def __init__(self) -> None:
        # event_type -> [(handler, critical), ...] in subscribe order.
        self._handlers: dict[str, list[tuple[Handler, bool]]] = {}
        # Subscriber failures, kept for inspection/tests:
        # {"event_type", "handler", "error", "critical"}.
        self.errors: list[dict] = []

    def subscribe(self, event_type: str, handler: Handler | None = None,
                  *, critical: bool = False):
        """Register `handler` for `event_type` ("*" = all events).

        ``critical=True`` marks a load-bearing subscriber: if it
        raises, publish() records the failure and then re-raises, so
        the publisher cannot silently continue without the work this
        handler was supposed to do. Non-critical (default) handlers
        are isolated instead. Usable as a decorator:
        @bus.subscribe("minutes.confirmed", critical=True)."""
        if handler is None:
            def decorator(fn: Handler) -> Handler:
                self._handlers.setdefault(event_type, []).append((fn, critical))
                return fn

            return decorator
        self._handlers.setdefault(event_type, []).append((handler, critical))
        return handler

    def publish(self, event: Event) -> Event:
        """Deliver `event` to its subscribers, in subscription order.

        A raising NON-critical handler is isolated: the failure is
        logged + recorded in self.errors and the remaining handlers
        still run. A raising CRITICAL handler is logged + recorded
        the same way, then its exception propagates to the caller —
        delivery stops there, because the publisher's next state
        depends on that handler's work."""
        handlers = list(self._handlers.get(event.type, ()))
        handlers += list(self._handlers.get("*", ()))
        for handler, critical in handlers:
            try:
                handler(event)
            except Exception as exc:
                logger.exception(
                    "event handler %r failed on %s (meeting %s)",
                    getattr(handler, "__name__", repr(handler)),
                    event.type, event.meeting_id)
                self.errors.append({
                    "event_type": event.type,
                    "meeting_id": event.meeting_id,
                    "handler": getattr(handler, "__name__", repr(handler)),
                    "error": f"{type(exc).__name__}: {exc}",
                    "critical": critical,
                })
                if critical:
                    raise
        return event

    def emit(self, meeting: Meeting, event_type: str,
             payload: dict[str, Any] | None = None) -> Event:
        """Audit + publish: append the event to ``meeting.events``
        (the persistent audit log) and then deliver it to subscribers.
        This is THE way pipeline code publishes meeting events, so the
        log can never drift from what the bus carried.

        The audit record is appended BEFORE delivery on purpose: if a
        critical subscriber raises (see publish()), the exception
        propagates out of emit(), but the event stays in the log — it
        really was published; only the downstream handoff failed."""
        event = Event(type=event_type, meeting_id=meeting.id,
                      payload=_sanitize(payload or {}))
        meeting.events.append(MeetingEvent(type=event.type, at=event.at,
                                           payload=event.payload))
        return self.publish(event)
