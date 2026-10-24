
"""Notification tool interface (kept separate from task systems)."""
from __future__ import annotations
from abc import ABC, abstractmethod
class BaseNotifyTool(ABC):
    system: str = "base"
    @abstractmethod
    def send(self, to: str, title: str, content: str, **kw) -> dict: ...
