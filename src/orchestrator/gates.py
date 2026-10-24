"""Human-in-the-loop confirmation gates.

Two gates, deliberately separate (plan section 3):
  gate1 = minutes confirmation, gate2 = tasks confirmation.
In API mode a gate stays pending until the confirm endpoint is called.
A gate can also be rejected (reject / reject_minutes / reject_tasks):
the rejection revokes any approval and is kept until a confirm clears
it; the API layers status transitions (minutes_rejected / tasks_rejected)
and re-run endpoints on top of this.
In demo / auto mode, auto_approve short-circuits the wait.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class ConfirmationGate:
    def __init__(self, auto_approve: bool = False):
        self.auto_approve = auto_approve
        self._approved: dict[tuple[str, str], bool] = {}
        self._rejected: dict[tuple[str, str], str] = {}
        self.pending: list[dict] = []

    def request(self, kind: str, meeting_id: str, payload: str = "") -> bool:
        """Return True if approved (auto), else record pending and return False."""
        if self.auto_approve or self._approved.get((kind, meeting_id)):
            return True
        self.pending.append({"kind": kind, "meeting_id": meeting_id, "payload": payload})
        logger.info("Gate '%s' pending for meeting %s", kind, meeting_id)
        return False

    def confirm(self, kind: str, meeting_id: str) -> None:
        self._approved[(kind, meeting_id)] = True
        self._rejected.pop((kind, meeting_id), None)
        self.pending = [p for p in self.pending if not (p["kind"] == kind and p["meeting_id"] == meeting_id)]

    def is_approved(self, kind: str, meeting_id: str) -> bool:
        return self.auto_approve or bool(self._approved.get((kind, meeting_id)))

    # -- rejection path (symmetric to confirm) -----------------------------
    def reject(self, kind: str, meeting_id: str, reason: str = "") -> None:
        """Record a human rejection: any earlier approval is revoked and
        the pending entry is cleared, so a later flow run stops at this
        gate again until it is explicitly confirmed. The rejection stays
        queryable via is_rejected() until a confirm clears it."""
        self._approved.pop((kind, meeting_id), None)
        self._rejected[(kind, meeting_id)] = reason or ""
        self.pending = [p for p in self.pending if not (p["kind"] == kind and p["meeting_id"] == meeting_id)]
        logger.info("Gate '%s' rejected for meeting %s: %s", kind, meeting_id, reason)

    def reject_minutes(self, meeting_id: str, reason: str = "") -> None:
        self.reject("minutes", meeting_id, reason)

    def reject_tasks(self, meeting_id: str, reason: str = "") -> None:
        self.reject("tasks", meeting_id, reason)

    def is_rejected(self, kind: str, meeting_id: str) -> bool:
        return (kind, meeting_id) in self._rejected

    def rejection_reason(self, kind: str, meeting_id: str) -> str | None:
        return self._rejected.get((kind, meeting_id))
