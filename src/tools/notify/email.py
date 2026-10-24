
"""TODO(real): implement against SMTP via smtplib (TODO: host/port/from in .env)
Unconfigured -> mock result (delivered=False) + warning."""
from __future__ import annotations
import logging
from .base import BaseNotifyTool
logger = logging.getLogger(__name__)
class EmailNotifyTool(BaseNotifyTool):
    system = "email"
    def __init__(self, endpoint: str = "", **kw):
        self.endpoint = endpoint; self.options = kw
    def send(self, to, title, content, **kw) -> dict:
        if not self.endpoint:
            logger.warning("EmailNotifyTool not configured; mock send to=%s title=%s", to, title)
            return {"mock": True, "delivered": False, "system": self.system, "to": to, "title": title}
        raise NotImplementedError("TODO: real send - SMTP via smtplib (TODO: host/port/from in .env)")
