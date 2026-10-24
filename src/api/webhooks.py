"""Webhook receivers — NOT IMPLEMENTED YET, on purpose and loudly.

None of the three webhooks below process anything today: provider
signature verification (Feishu challenge / Google channel token /
Slack signing secret) and the follow-up processing are TODO. Instead
of the previous behaviour (HTTP 200 with {"ok": true, "mock": true},
which callers could mistake for success), every webhook now answers
**HTTP 501** with an explicit ``not_implemented`` body, and never 500s
on a malformed payload.
TODO(real): verify signatures, then enqueue processing.
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhooks", tags=["webhooks"])


async def _safe_body(request: Request) -> dict:
    raw = await request.body()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {"_payload": data}
    except Exception:
        return {}


def _not_implemented(kind: str, detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=501,
        content={
            "ok": False,
            "error": "not_implemented",
            "webhook": kind,
            "detail": detail,
        },
    )


@router.post("/calendar")
async def calendar_webhook(request: Request):
    body = await _safe_body(request)
    logger.info("calendar webhook received but NOT implemented: %s", body)
    return _not_implemented(
        "calendar",
        "calendar change processing is not implemented; no event was read or changed",
    )


@router.post("/recording")
async def recording_webhook(request: Request):
    body = await _safe_body(request)
    logger.info("recording webhook received but NOT implemented: %s", body)
    # TODO: download recording -> Transcriber -> MeetingFlow.run
    return _not_implemented(
        "recording",
        "recording processing is not implemented; no transcript was created",
    )


@router.post("/task-status")
async def task_status_webhook(request: Request):
    body = await _safe_body(request)
    logger.info("task-status webhook received but NOT implemented: %s", body)
    # TODO: map external_task_id -> ActionItem and update status
    return _not_implemented(
        "task-status",
        "external task-status sync is not implemented; no task was updated",
    )
