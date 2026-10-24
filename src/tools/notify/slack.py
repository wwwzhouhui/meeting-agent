
"""TODO(real): implement against Slack Incoming Webhook / chat.postMessage: POST https://slack.com/api/chat.postMessage (Block Kit)
Unconfigured -> mock result (delivered=False) + warning."""
from __future__ import annotations
import logging
from .base import BaseNotifyTool
logger = logging.getLogger(__name__)
class SlackNotifyTool(BaseNotifyTool):
    system = "slack"
    def __init__(self, endpoint: str = "", **kw):
        self.endpoint = endpoint; self.options = kw
    def send(self, to, title, content, **kw) -> dict:
        if not self.endpoint:
            logger.warning("SlackNotifyTool not configured; mock send to=%s title=%s", to, title)
            return {"mock": True, "delivered": False, "system": self.system, "to": to, "title": title}
        raise NotImplementedError("TODO: real send - Slack Incoming Webhook / chat.postMessage: POST https://slack.com/api/chat.postMessage (Block Kit)")
