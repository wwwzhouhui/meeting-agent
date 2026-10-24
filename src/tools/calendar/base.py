
"""Calendar tool interface. All providers implement this.
Official API references are noted in each provider module."""
from __future__ import annotations
from abc import ABC, abstractmethod

class BaseCalendarTool(ABC):
    system: str = "base"
    @abstractmethod
    def list_events(self, start: str, end: str) -> list[dict]: ...
    @abstractmethod
    def query_freebusy(self, attendees: list[str], start: str, end: str) -> dict: ...
    @abstractmethod
    def create_event(self, title: str, start: str, attendees: list[str], description: str = "", end: str | None = None) -> dict: ...
    @abstractmethod
    def update_event(self, event_id: str, **fields) -> dict: ...
