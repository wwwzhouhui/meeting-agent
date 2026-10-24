
"""Task-system tool interface (Feishu Task / Jira / Linear / Trello)."""
from __future__ import annotations
from abc import ABC, abstractmethod
class BaseTaskTool(ABC):
    system: str = "base"
    @abstractmethod
    def create_task(self, title: str, owner_id: str | None = None, due_date: str | None = None, idempotency_key: str = "") -> dict: ...
    @abstractmethod
    def update_task(self, task_id: str, **fields) -> dict: ...
    @abstractmethod
    def comment_task(self, task_id: str, text: str) -> dict: ...
