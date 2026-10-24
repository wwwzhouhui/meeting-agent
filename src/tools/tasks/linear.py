
"""TODO(real): implement against Linear GraphQL: POST https://api.linear.app/graphql (issueCreate)
Unconfigured -> mock result + warning."""
from __future__ import annotations
import logging
from .base import BaseTaskTool
logger = logging.getLogger(__name__)
class LinearTaskTool(BaseTaskTool):
    system = "linear"
    def __init__(self, token: str = "", **kw):
        self.token = token; self.options = kw; self._seen: dict[str, dict] = {}
    def create_task(self, title, owner_id=None, due_date=None, idempotency_key="") -> dict:
        if idempotency_key and idempotency_key in self._seen: return self._seen[idempotency_key]
        if not self.token:
            logger.warning("LinearTaskTool not configured; mock create_task %s", title)
            result = {"id": f"mock-linear-{idempotency_key or 'task'}", "mock": True, "system": self.system, "title": title}
        else:
            raise NotImplementedError("TODO: real API call - Linear GraphQL: POST https://api.linear.app/graphql (issueCreate)")
        if idempotency_key: self._seen[idempotency_key] = result
        return result
    def update_task(self, task_id, **fields) -> dict:
        if not self.token: logger.warning("LinearTaskTool mock update_task"); return {"id": task_id, "mock": True, "system": self.system, **fields}
        raise NotImplementedError("TODO: real API call - Linear GraphQL: POST https://api.linear.app/graphql (issueCreate)")
    def comment_task(self, task_id, text) -> dict:
        if not self.token: logger.warning("LinearTaskTool mock comment_task"); return {"id": task_id, "mock": True, "system": self.system, "text": text}
        raise NotImplementedError("TODO: real API call - Linear GraphQL: POST https://api.linear.app/graphql (issueCreate)")
