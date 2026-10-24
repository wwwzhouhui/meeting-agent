"""API end-to-end via TestClient: happy path, state-machine guards (409),
404/422, idempotent confirms, needs_transcript, calendar propose, webhooks.
"""
import copy
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.shared.db import store

client = TestClient(app)
SAMPLE = json.loads(
    (Path(__file__).resolve().parents[1] / "examples" / "sample_meeting.json")
    .read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _clean_store():
    store._data.clear()
    yield
    store._data.clear()


def _create(mid: str, **overrides):
    payload = copy.deepcopy(SAMPLE)
    payload["id"] = mid
    payload.update(overrides)
    return client.post("/meetings", json=payload)


def _full_flow(mid: str):
    assert _create(mid).status_code == 200
    r = client.post(f"/meetings/{mid}/process", json={})
    assert r.status_code == 200 and r.json()["status"] == "awaiting_minutes_confirmation"
    r = client.post(f"/meetings/{mid}/confirm-minutes")
    assert r.status_code == 200 and r.json()["status"] == "awaiting_tasks_confirmation"
    r = client.post(f"/meetings/{mid}/confirm-tasks")
    assert r.status_code == 200
    return r.json()


def test_health():
    assert client.get("/health").json() == {"ok": True}


def test_full_flow_and_idempotent_confirms():
    body = _full_flow("api-full-1")
    assert body["status"] == "dispatched"
    assert body["action_items"] and all(
        a["status"] == "dispatched" and a["external_task_id"] for a in body["action_items"])
    first_ids = [a["external_task_id"] for a in body["action_items"]]
    # repeat confirms are idempotent no-ops (no re-extraction / re-dispatch)
    r = client.post("/meetings/api-full-1/confirm-minutes")
    assert r.status_code == 200
    assert [a["external_task_id"] for a in r.json()["action_items"]] == first_ids
    r = client.post("/meetings/api-full-1/confirm-tasks")
    assert r.status_code == 200
    assert [a["external_task_id"] for a in r.json()["action_items"]] == first_ids
    # re-processing a dispatched meeting is rejected, not silently re-run
    assert client.post("/meetings/api-full-1/process", json={}).status_code == 409


def test_duplicate_create_409():
    assert _create("api-dup-1").status_code == 200
    assert _create("api-dup-1").status_code == 409


def test_confirm_before_process_409():
    assert _create("api-guard-1").status_code == 200
    assert client.post("/meetings/api-guard-1/confirm-minutes").status_code == 409
    assert client.post("/meetings/api-guard-1/confirm-tasks").status_code == 409


def test_confirm_tasks_before_minutes_409():
    assert _create("api-guard-2").status_code == 200
    assert client.post("/meetings/api-guard-2/process", json={}).status_code == 200
    # gate2 before gate1 must be refused
    assert client.post("/meetings/api-guard-2/confirm-tasks").status_code == 409


def test_not_found_404():
    assert client.get("/meetings/nope").status_code == 404
    assert client.post("/meetings/nope/process", json={}).status_code == 404
    assert client.post("/meetings/nope/confirm-minutes").status_code == 404
    assert client.post("/meetings/nope/transcript", json={"transcript": []}).status_code == 404


def test_validation_422():
    assert client.post("/meetings", json={}).status_code == 422
    assert _create("api-422-1").status_code == 200
    bad = {"transcript": [{"id": 1, "speaker": "张三"}]}  # missing text
    assert client.post("/meetings/api-422-1/transcript", json=bad).status_code == 422


def test_needs_transcript_then_transcript_then_process():
    payload = {"id": "api-nt-1", "title": "只有录音", "audio_url": "oss://x/a.mp3"}
    assert client.post("/meetings", json=payload).status_code == 200
    r = client.post("/meetings/api-nt-1/process", json={})
    body = r.json()
    assert r.status_code == 200
    assert body["status"] == "needs_transcript"
    assert body["minutes"] is None
    assert body["warnings"]
    assert body["action_items"] == []
    # supply the transcript -> meeting becomes processable again
    r = client.post("/meetings/api-nt-1/transcript",
                    json={"transcript": SAMPLE["transcript"]})
    assert r.status_code == 200 and r.json()["status"] == "scheduled"
    r = client.post("/meetings/api-nt-1/process", json={})
    assert r.json()["status"] == "awaiting_minutes_confirmation"


def test_calendar_propose():
    assert _create("api-cal-1").status_code == 200
    r = client.post("/calendar/propose", json={"meeting_id": "api-cal-1"})
    assert r.status_code == 200
    candidates = r.json()["candidates"]
    assert candidates and all(c.startswith("2026-10-27") for c in candidates)
    assert client.post("/calendar/propose", json={}).status_code == 400
    assert client.post("/calendar/propose", json={"meeting_id": "nope"}).status_code == 404


def test_webhooks_are_explicitly_not_implemented():
    for path in ("/webhooks/calendar", "/webhooks/recording", "/webhooks/task-status"):
        r = client.post(path, json={"any": "thing"})
        assert r.status_code == 501, path
        body = r.json()
        assert body["ok"] is False and body["error"] == "not_implemented"
    # malformed JSON must still be a clean 501, never a 500
    r = client.post("/webhooks/recording", content=b"not-json{",
                    headers={"content-type": "application/json"})
    assert r.status_code == 501
