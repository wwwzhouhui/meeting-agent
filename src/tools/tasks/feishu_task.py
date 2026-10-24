"""Feishu Task v2 implementation.

Endpoint: POST /open-apis/task/v2/tasks.  A tenant token can create a
task and assign it to a user by ``open_id``.  Feishu task due timestamps
are Unix **milliseconds** (strings) — deliberately different from the
Calendar API's second timestamps.

Updates use PATCH /open-apis/task/v2/tasks/{guid} with an explicit
``update_fields`` list, as required by Task v2.  Completion is the same
PATCH with ``completed_at`` set (or cleared with "0").
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from src.tools.feishu_client import FeishuClient, map_user_id, timestamp_millis

from .base import BaseTaskTool

logger = logging.getLogger(__name__)


class FeishuTaskTool(BaseTaskTool):
    system = "feishu_task"

    def __init__(
        self,
        token: str | FeishuClient = "",
        *,
        client: FeishuClient | None = None,
        app_id: str = "",
        app_secret: str = "",
        user_map: dict[str, str] | None = None,
        timezone: str = "Asia/Shanghai",
        transport: Any = None,
        http_client: Any = None,
        **options,
    ):
        if isinstance(token, FeishuClient):
            client = token
            token = ""
        self.token = str(token or "")
        if client is None and (app_id or app_secret or self.token):
            client = FeishuClient(
                app_id=app_id,
                app_secret=app_secret,
                static_access_token=self.token,
                transport=transport,
                http_client=http_client,
            )
        self.client = client
        self.user_map = dict(user_map or {})
        self.timezone = timezone or "Asia/Shanghai"
        self.options = options
        self._seen: dict[str, dict] = {}

    @property
    def configured(self) -> bool:
        return bool(self.client and self.client.configured)

    def _mock(self, kind: str, **kw) -> dict:
        logger.warning("FeishuTaskTool not configured; mock %s %s", kind, kw)
        return {"id": kw.get("id") or f"mock-feishu_task-{kind}", "mock": True,
                "system": self.system, **kw}

    def _due_payload(self, due_date) -> dict:
        # Date-only due dates are deadlines at the end of that local day;
        # explicit datetimes retain their exact time.  Either way Feishu
        # Task wants milliseconds, not the Calendar API's seconds.
        return {
            "timestamp": timestamp_millis(due_date, self.timezone, end_of_day=True),
            "is_all_day": False,
        }

    def _member_for_owner(self, owner_id) -> tuple[dict | None, list[str]]:
        if not owner_id:
            return None, []
        mapped = map_user_id(owner_id, self.user_map)
        if not mapped:
            return None, [f"skipped invalid/non-open_id owner_id: {owner_id}"]
        return {"id": mapped, "type": "user", "role": "assignee"}, []

    # ------------------------------------------------------------------ API
    def create_task(
        self,
        title,
        owner_id=None,
        due_date=None,
        idempotency_key="",
        *,
        description: str = "",
    ) -> dict:
        if idempotency_key and idempotency_key in self._seen:
            return self._seen[idempotency_key]
        if not self.configured:
            logger.warning("FeishuTaskTool not configured; mock create_task %s", title)
            result = {
                "id": f"mock-feishu_task-{idempotency_key or 'task'}",
                "mock": True,
                "system": self.system,
                "title": title,
            }
        else:
            payload: dict[str, Any] = {"summary": title}
            if description:
                payload["description"] = description
            if due_date:
                payload["due"] = self._due_payload(due_date)
            member, warnings = self._member_for_owner(owner_id)
            if member:
                payload["members"] = [member]
            params: dict[str, Any] = {"user_id_type": "open_id"}
            if idempotency_key:
                # Task v2 accepts a client token for safe retries; derive
                # a stable UUID from our existing idempotency key.
                params["client_token"] = str(uuid.uuid5(uuid.NAMESPACE_URL, str(idempotency_key)))
            data = self.client.request(
                "POST", "/open-apis/task/v2/tasks", token_type="tenant", json=payload, params=params
            )
            task = data.get("task") if isinstance(data.get("task"), dict) else data
            guid = task.get("guid") or task.get("id")
            if not guid:
                from src.tools.feishu_client import FeishuAPIError

                raise FeishuAPIError(
                    "invalid_response", "Feishu task create response has no task guid", "/open-apis/task/v2/tasks"
                )
            result = {
                "id": guid,
                "guid": guid,
                "mock": False,
                "system": self.system,
                "title": title,
                "task": task,
                "warnings": warnings,
            }
        if idempotency_key:
            self._seen[idempotency_key] = result
        return result

    def update_task(self, task_id, **fields) -> dict:
        if not self.configured:
            logger.warning("FeishuTaskTool mock update_task")
            return {"id": task_id, "mock": True, "system": self.system, **fields}

        task: dict[str, Any] = {}
        update_fields: list[str] = []
        warnings: list[str] = []

        def set_field(name: str, value: Any):
            task[name] = value
            if name not in update_fields:
                update_fields.append(name)

        if "title" in fields:
            set_field("summary", fields.pop("title"))
        if "summary" in fields:
            set_field("summary", fields.pop("summary"))
        if "description" in fields:
            set_field("description", fields.pop("description"))
        if "due_date" in fields:
            due = fields.pop("due_date")
            set_field("due", self._due_payload(due) if due else None)
        if "due" in fields:
            due = fields.pop("due")
            set_field("due", due if isinstance(due, dict) else self._due_payload(due))
        owner = fields.pop("owner_id", None)
        assignee = fields.pop("assignee", None)
        owner = assignee if assignee is not None else owner
        if owner is not None:
            member, member_warnings = self._member_for_owner(owner)
            warnings.extend(member_warnings)
            if member:
                set_field("members", [member])
            elif owner:
                raise ValueError(f"owner_id cannot be mapped to a Feishu open_id: {owner}")
        if "members" in fields:
            set_field("members", fields.pop("members"))

        completed_at = fields.pop("completed_at", None)
        completed = fields.pop("completed", None)
        status = fields.pop("status", None)
        if completed_at is not None:
            if isinstance(completed_at, (datetime,)) or (
                isinstance(completed_at, str) and ("-" in completed_at or "T" in completed_at)
            ):
                completed_value = timestamp_millis(completed_at, self.timezone)
            else:
                completed_value = str(completed_at)
            set_field("completed_at", completed_value)
        elif completed is True or status in ("done", "completed", "complete"):
            set_field("completed_at", str(int(datetime.now(timezone.utc).timestamp() * 1000)))
        elif completed is False or status in ("todo", "in_progress", "pending"):
            set_field("completed_at", "0")

        # Remaining fields are assumed to already use Task v2 names.
        for key, value in fields.items():
            set_field(key, value)
        if not update_fields:
            raise ValueError("update_task called with no updatable fields")

        data = self.client.request(
            "PATCH",
            f"/open-apis/task/v2/tasks/{task_id}",
            token_type="tenant",
            json={"task": task, "update_fields": update_fields},
            params={"user_id_type": "open_id"},
        )
        updated = data.get("task") if isinstance(data.get("task"), dict) else data
        return {
            "id": task_id,
            "guid": task_id,
            "mock": False,
            "system": self.system,
            "task": updated,
            "warnings": warnings,
        }

    def complete_task(self, task_id: str) -> dict:
        """Mark a task complete via PATCH completed_at."""
        return self.update_task(task_id, completed=True)

    def comment_task(self, task_id, text) -> dict:
        if not self.configured:
            logger.warning("FeishuTaskTool mock comment_task")
            return {"id": task_id, "mock": True, "system": self.system, "text": text}
        data = self.client.request(
            "POST",
            f"/open-apis/task/v2/tasks/{task_id}/comments",
            token_type="tenant",
            json={"content": text},
            params={"user_id_type": "open_id"},
        )
        comment = data.get("comment") if isinstance(data.get("comment"), dict) else data
        comment_id = comment.get("id") or comment.get("comment_id") or task_id
        return {
            "id": comment_id,
            "task_id": task_id,
            "mock": False,
            "system": self.system,
            "comment": comment,
        }
