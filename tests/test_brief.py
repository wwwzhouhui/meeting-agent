"""Pre-meeting brief: flow.prepare_brief (related-history filtering,
storage on Meeting.brief, regeneration) + API POST/GET /brief."""
import copy
import json
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.orchestrator.flow import MeetingFlow, related_history
from src.shared.db import store
from src.shared.schema import ActionItem, Decision, Meeting, Participant

client = TestClient(app)
SAMPLE = json.loads(
    (Path(__file__).resolve().parents[1] / "examples" / "sample_meeting.json")
    .read_text(encoding="utf-8"))
PEOPLE = [Participant(**p) for p in SAMPLE["participants"]]


@pytest.fixture(autouse=True)
def _clean_store():
    store._data.clear()
    yield
    store._data.clear()


def _history_meeting(mid="hist-1", title="支付联调周会", participants=None):
    return Meeting(
        id=mid, title=title, participants=participants if participants is not None else PEOPLE,
        decisions=[Decision(text="上线推迟至11月8日", segment_ids=[1])],
        action_items=[
            ActionItem(id=f"{mid}-a1", title="完成证书续期", owner_raw="李四",
                       due_date="2026-10-28", source_segment_ids=[1], status="dispatched"),
            ActionItem(id=f"{mid}-a2", title="已办结事项", owner_raw="王五",
                       source_segment_ids=[1], status="done"),
        ],
    )


# ------------------------------------------------------------- filtering
def test_related_history_by_participant_and_topic():
    current = Meeting(id="cur", title="支付联调跟进会", participants=PEOPLE[:1])
    by_person = _history_meeting("h-person", title="完全无关的会", participants=PEOPLE[:1])
    by_topic = _history_meeting("h-topic", title="支付联调复盘",
                                 participants=[Participant(user_id="u-other", name="外人")])
    unrelated = _history_meeting("h-none", title="团建安排",
                                  participants=[Participant(user_id="u-x", name="路人")])
    related = related_history(current, [by_person, by_topic, unrelated, current])
    assert [m.id for m in related] == ["h-person", "h-topic"]


# ----------------------------------------------------------------- flow
def test_flow_prepare_brief_content_and_storage():
    current = Meeting(id="cur-1", title="支付联调跟进会", participants=PEOPLE,
                      start_at=SAMPLE["start_at"])
    store.save(_history_meeting())
    out = MeetingFlow().prepare_brief(current)  # history=None -> from store
    assert out.brief is not None
    assert out.brief.generated_at is not None
    assert out.brief.source_meeting_ids == ["hist-1"]
    content = out.brief.content
    assert "支付联调跟进会" in content          # title
    assert "2026-10-23" in content              # time
    assert "张三" in content and "李四" in content  # participants
    assert "上线推迟至11月8日" in content        # last decisions
    assert "完成证书续期" in content             # open task carried over
    assert "已办结事项" not in content           # done tasks are not
    assert "建议议程" in content                 # agenda section


def test_flow_prepare_brief_no_history_is_honest():
    current = Meeting(id="cur-2", title="全新主题会", participants=PEOPLE)
    out = MeetingFlow().prepare_brief(current, history=[])
    assert out.brief.source_meeting_ids == []
    assert "上次决策" not in out.brief.content
    assert "建议议程" in out.brief.content


def test_flow_prepare_brief_regeneration_overwrites():
    flow = MeetingFlow()
    current = Meeting(id="cur-3", title="支付联调跟进会", participants=PEOPLE)
    first = flow.prepare_brief(current, history=[]).brief
    store.save(_history_meeting("hist-late"))
    second = flow.prepare_brief(current).brief  # now picks up the history
    assert second.generated_at >= first.generated_at
    assert "完成证书续期" in second.content
    assert "完成证书续期" not in first.content


# ------------------------------------------------------------------ API
def _create(mid, **overrides):
    payload = copy.deepcopy(SAMPLE)
    payload["id"] = mid
    payload.update(overrides)
    return client.post("/meetings", json=payload)


def test_api_brief_roundtrip_and_404():
    assert _create("brief-api-1").status_code == 200
    # not generated yet -> explicit 404
    r = client.get("/meetings/brief-api-1/brief")
    assert r.status_code == 404 and "not generated" in r.json()["detail"]
    assert client.get("/meetings/nope/brief").status_code == 404
    assert client.post("/meetings/nope/brief").status_code == 404
    # seed a related history meeting directly in the store
    store.save(_history_meeting("brief-hist"))
    r = client.post("/meetings/brief-api-1/brief")
    assert r.status_code == 200
    brief = r.json()["brief"]
    assert brief["source_meeting_ids"] == ["brief-hist"]
    assert "完成证书续期" in brief["content"]
    datetime.fromisoformat(brief["generated_at"])  # parses as a timestamp
    r = client.get("/meetings/brief-api-1/brief")
    assert r.status_code == 200 and r.json()["content"] == brief["content"]
    # regenerating overwrites and stays 200
    r = client.post("/meetings/brief-api-1/brief")
    assert r.status_code == 200 and r.json()["brief"]["content"] == brief["content"]


def test_api_brief_allowed_in_any_status():
    assert _create("brief-api-2").status_code == 200
    client.post("/meetings/brief-api-2/process", json={})
    client.post("/meetings/brief-api-2/confirm-minutes")
    client.post("/meetings/brief-api-2/confirm-tasks")
    assert client.get("/meetings/brief-api-2").json()["status"] == "dispatched"
    r = client.post("/meetings/brief-api-2/brief")  # even after dispatch
    assert r.status_code == 200 and r.json()["brief"] is not None
