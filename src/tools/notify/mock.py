"""Built-in mock notify tool: the default when NOTIFY_PROVIDER is unset
or "mock". Never pretends delivery happened — send() returns
{"mock": True, "delivered": False, ...} and logs a warning. This is the
tool the reminder scanner (src/orchestrator/reminders.py) uses out of
the box, so reminder counts can be exercised end-to-end with no
external account configured.
"""
from __future__ import annotations

import logging

from .base import BaseNotifyTool

logger = logging.getLogger(__name__)


class MockNotifyTool(BaseNotifyTool):
    system = "mock"

    def send(self, to: str, title: str, content: str, **kw) -> dict:
        logger.warning("MockNotifyTool: mock send to=%s title=%s (nothing was delivered)", to, title)
        return {"mock": True, "delivered": False, "system": self.system,
                "to": to, "title": title}
