
"""TODO(real): implement against Jira Cloud REST v3: POST https://{your-domain}.atlassian.net/rest/api/3/issue
Unconfigured -> mock result + warning."""
from __future__ import annotations
import logging
from .base import BaseTaskTool
logger = logging.getLogger(__name__)
class JiraTaskTool(BaseTaskTool):
    system = "jira"
    def __init__(self, token: str = "", **kw):
        self.token = token; self.options = kw; self._seen: dict[str, dict] = {}
    def create_task(self, title, owner_id=None, due_date=None, idempotency_key="") -> dict:
        if idempotency_key and idempotency_key in self._seen: return self._seen[idempotency_key]
        if not self.token:
            logger.warning("JiraTaskTool not configured; mock create_task %s", title)
            result = {"id": f"mock-jira-{idempotency_key or 'task'}", "mock": True, "system": self.system, "title": title}
        else:
            raise NotImplementedError("TODO: real API call - Jira Cloud REST v3: POST https://{your-domain}.atlassian.net/rest/api/3/issue")
        if idempotency_key: self._seen[idempotency_key] = result
        return result
    def update_task(self, task_id, **fields) -> dict:
        if not self.token: logger.warning("JiraTaskTool mock update_task"); return {"id": task_id, "mock": True, "system": self.system, **fields}
        raise NotImplementedError("TODO: real API call - Jira Cloud REST v3: POST https://{your-domain}.atlassian.net/rest/api/3/issue")
    def comment_task(self, task_id, text) -> dict:
        if not self.token: logger.warning("JiraTaskTool mock comment_task"); return {"id": task_id, "mock": True, "system": self.system, "text": text}
        raise NotImplementedError("TODO: real API call - Jira Cloud REST v3: POST https://{your-domain}.atlassian.net/rest/api/3/issue")
