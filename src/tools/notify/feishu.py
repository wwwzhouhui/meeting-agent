"""Feishu custom-bot webhook notifications.

The webhook URL identifies one group bot, so ``to`` cannot select an
individual recipient; it is retained in the result for auditability and
the card itself carries the title/body produced by the caller.

When FEISHU_WEBHOOK_SECRET is set, signing follows Feishu's custom-bot
specification: use ``"{timestamp}\n{secret}"`` as the HMAC-SHA256 key
(with an empty message), base64-encode the digest, and send it in the
JSON body's ``timestamp`` and ``sign`` fields.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time
from typing import Any, Callable

from src.tools.feishu_client import FeishuClient

from .base import BaseNotifyTool

logger = logging.getLogger(__name__)


class FeishuNotifyTool(BaseNotifyTool):
    system = "feishu"

    def __init__(
        self,
        endpoint: str = "",
        *,
        url: str = "",
        secret: str = "",
        client: FeishuClient | None = None,
        transport: Any = None,
        http_client: Any = None,
        time_fn: Callable[[], float] | None = None,
        **kw,
    ):
        self.endpoint = url or endpoint or ""
        self.url = self.endpoint
        self.secret = secret or ""
        self.options = kw
        self._time_fn = time_fn or time.time
        self.client = client or FeishuClient(transport=transport, http_client=http_client)

    @property
    def configured(self) -> bool:
        return bool(self.endpoint)

    def _sign(self, timestamp: str) -> str:
        string_to_sign = f"{timestamp}\n{self.secret}"
        digest = hmac.new(
            string_to_sign.encode("utf-8"), msg=b"", digestmod=hashlib.sha256
        ).digest()
        return base64.b64encode(digest).decode("utf-8")

    @staticmethod
    def _card(title: str, content: str) -> dict:
        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": "blue",
                "title": {"tag": "plain_text", "content": title},
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": content}},
            ],
        }

    def send(self, to, title, content, **kw) -> dict:
        if not self.endpoint:
            logger.warning("FeishuNotifyTool not configured; mock send to=%s title=%s", to, title)
            return {"mock": True, "delivered": False, "system": self.system, "to": to, "title": title}
        payload: dict[str, Any] = {
            "msg_type": "interactive",
            "card": self._card(title, content),
        }
        if self.secret:
            timestamp = str(int(self._time_fn()))
            payload["timestamp"] = timestamp
            payload["sign"] = self._sign(timestamp)
        response = self.client.post_webhook(self.endpoint, payload)
        code = response.get("code", response.get("StatusCode", 0))
        msg = response.get("msg") or response.get("StatusMessage") or ""
        return {
            "mock": False,
            "delivered": True,
            "system": self.system,
            "to": to,
            "title": title,
            "code": code,
            "msg": msg,
            "data": response.get("data"),
            "response": response,
        }
