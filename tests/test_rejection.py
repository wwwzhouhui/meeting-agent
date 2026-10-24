"""Rejection path: gate-level reject, API reject-minutes / reject-tasks
state guards (409), rejection records, re-run after rejection, task
reassignment via PATCH, and dispatch honouring the manual assignment."""
import copy
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.orchestrator.gates import ConfirmationGate
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


def _create(mid):
    payload = copy.deepcopy(SAMPLE)
    payload["id"] = mid
    assert client.post("/meetings", json=payload).status_code == 200


def _to_tasks(mid):
    _create(mid)
    r = client.post(f"/meetings/{mid}/process", json={})
    assert r.json()["status"] == "awaiting_minutes_confirmation"
    r = client.post(f"/meetings/{mid}/confirm-minutes")
    assert r.json()["status"] == "awaiting_tasks_confirmation"
    return r.json()


# ------------------------------------------------------------ gate unit
def test_gate_reject_is_symmetric_to_confirm():
    gate = ConfirmationGate()
    assert gate.request("minutes", "m1") is False
    gate.confirm("minutes", "m1")
    assert gate.is_approved("minutes", "m1") is True
    gate.reject_minutes("m1", "纪要摘要不对")
    assert gate.is_approved("minutes", "m1") is False  # approval revoked
    assert gate.is_rejected("minutes", "m1") is True
    assert gate.rejection_reason("minutes", "m1") == "纪要摘要不对"
    gate.reject_tasks("m1", "负责人全错")
    assert gate.is_rejected("tasks", "m1") is True
    gate.confirm("tasks", "m1")  # a later confirm clears the rejection
    assert gate.is_rejected("tasks", "m1") is False
    assert gate.is_approved("tasks", "m1") is True


# ------------------------------------------------------------ state 409
def test_reject_and_reextract_wrong_states_409():
    _create("rej-guard-1")
    assert client.post("/meetings/rej-guard-1/reject-minutes",
                       json={"reason": "x"}).status_code == 409
    assert client.post("/meetings/rej-guard-1/reject-tasks",
                       json={"reason": "x"}).status_code == 409
    assert client.post("/meetings/rej-guard-1/reextract-tasks").status_code == 409
    assert client.post("/meetings/nope/reject-minutes",
                       json={"reason": "x"}).status_code == 404
    # at gate1, rejecting tasks is still the wrong state
    client.post("/meetings/rej-guard-1/process", json={})
    assert client.post("/meetings/rej-guard-1/reject-tasks",
                       json={"reason": "x"}).status_code == 409


# ---------------------------------------------------- minutes rejection
def test_reject_minutes_then_process_reruns_to_dispatched():
    _create("rej-min-1")
    client.post("/meetings/rej-min-1/process", json={})
    r = client.post("/meetings/rej-min-1/reject-minutes",
                    json={"reason": "执行摘要写错了上线日期"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "minutes_rejected"
    assert body["minutes_confirmed"] is False
    assert body["rejections"][-1]["gate"] == "minutes"
    assert body["rejections"][-1]["reason"] == "执行摘要写错了上线日期"
    assert body["rejections"][-1]["at"]
    # cannot confirm from the rejected state; must re-run process
    assert client.post("/meetings/rej-min-1/confirm-minutes").status_code == 409
    r = client.post("/meetings/rej-min-1/process", json={})
    assert r.status_code == 200
    assert r.json()["status"] == "awaiting_minutes_confirmation"
    assert r.json()["minutes"] is not None  # minutes regenerated
    r = client.post("/meetings/rej-min-1/confirm-minutes")
    assert r.json()["status"] == "awaiting_tasks_confirmation"
    r = client.post("/meetings/rej-min-1/confirm-tasks")
    assert r.json()["status"] == "dispatched"
    # the rejection stays on the audit trail after recovery
    assert len(r.json()["rejections"]) == 1


# ------------------------------------------------------ tasks rejection
def test_reject_tasks_reextract_and_manual_reassign_then_dispatch():
    body = _to_tasks("rej-task-1")
    r = client.post("/meetings/rej-task-1/reject-tasks",
                    json={"reason": "证书续期不该派给王五"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "tasks_rejected"
    assert body["tasks_confirmed"] is False
    assert body["rejections"][-1]["gate"] == "tasks"
    # confirm is blocked from the rejected state
    assert client.post("/meetings/rej-task-1/confirm-tasks").status_code == 409
    # patch is also blocked until tasks are re-extracted
    item_id = body["action_items"][0]["id"]
    assert client.patch(f"/meetings/rej-task-1/tasks/{item_id}",
                        json={"owner_id": "u-zhangsan"}).status_code == 409
    # re-extract from the confirmed minutes -> back to gate2
    r = client.post("/meetings/rej-task-1/reextract-tasks")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "awaiting_tasks_confirmation"
    assert body["action_items"]
    # manual reassignment of the first task to 张三
    item = body["action_items"][0]
    new_owner = next(p["user_id"] for p in SAMPLE["participants"]
                     if p["user_id"] != item["owner_id"])
    r = client.patch(f"/meetings/rej-task-1/tasks/{item['id']}",
                     json={"owner_id": new_owner, "due_date": "2026-12-01",
                           "priority": "P0"})
    assert r.status_code == 200
    patched = next(i for i in r.json()["action_items"] if i["id"] == item["id"])
    assert patched["owner_id"] == new_owner
    assert patched["assignment_source"] == "manual"
    assert patched["owner_confidence"] == 1.0
    assert patched["due_date"] == "2026-12-01"
    assert patched["priority"] == "P0"
    # dispatch honours the manual assignment
    r = client.post("/meetings/rej-task-1/confirm-tasks")
    assert r.status_code == 200 and r.json()["status"] == "dispatched"
    dispatched = next(i for i in r.json()["action_items"] if i["id"] == item["id"])
    assert dispatched["owner_id"] == new_owner
    assert dispatched["assignment_source"] == "manual"
    assert dispatched["status"] == "dispatched"
    # and editing is closed after dispatch
    assert client.patch(f"/meetings/rej-task-1/tasks/{item['id']}",
                        json={"priority": "P1"}).status_code == 409


def test_patch_task_404_and_422():
    body = _to_tasks("rej-patch-1")
    item_id = body["action_items"][0]["id"]
    assert client.patch("/meetings/rej-patch-1/tasks/nope",
                        json={"priority": "P0"}).status_code == 404
    assert client.patch("/meetings/nope/tasks/x",
                        json={"priority": "P0"}).status_code == 404
    assert client.patch(f"/meetings/rej-patch-1/tasks/{item_id}",
                        json={"due_date": "下周三"}).status_code == 422
    assert client.patch(f"/meetings/rej-patch-1/tasks/{item_id}",
                        json={"priority": "P9"}).status_code == 422
