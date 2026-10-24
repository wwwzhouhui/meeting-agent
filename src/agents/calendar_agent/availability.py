"""Availability checking. MVP: no real calendar connected -> propose
slots on weekday daytime hours, skipping a configurable busy set.
TODO(real): query_freebusy via tools.calendar base implementations.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

DEFAULT_HOURS = [time(10, 0), time(14, 0), time(16, 0)]


class AvailabilityChecker:
    def __init__(self, busy: set[str] | None = None):
        # busy holds ISO datetime strings that are already taken (mock data)
        self.busy = busy or set()

    def is_free(self, dt: datetime) -> bool:
        return dt.isoformat() not in self.busy

    def propose_slots(
        self,
        preferred: date | None,
        base_date: date,
        count: int = 3,
        timezone: str = "Asia/Shanghai",
    ) -> list[str]:
        """Return up to `count` ISO datetime strings, preferred date first."""
        slots: list[str] = []
        start_day = preferred or (base_date + timedelta(days=1))
        # scan at most 10 days forward
        for offset in range(10):
            cur = start_day + timedelta(days=offset)
            if cur.weekday() >= 5:  # skip weekends
                continue
            for h in DEFAULT_HOURS:
                # Use a naive local ISO string with timezone offset label kept
                # simple: store as 'YYYY-MM-DDTHH:MM:00' in meeting timezone.
                dt = datetime.combine(cur, h)
                key = dt.isoformat()
                if not self.is_free(dt):
                    continue
                slots.append(key)
                if len(slots) >= count:
                    return slots
        return slots
