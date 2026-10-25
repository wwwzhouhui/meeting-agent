"""Regressions for the four high-priority review findings (2026-10-25):

1. task extraction: two different items citing the same FIRST segment
   used to share one id AND one idempotency key, so dispatch silently
   reused the first task's external result for the second (a real
   reproduced task loss) — now a run-scoped -2/-3 suffix disambiguates;
2. audio upload/download size caps, upload orphan cleanup and
   cloud-ASR task-id invalidation on re-upload;
3. optional whole-app HTTP Basic auth (BASIC_AUTH_USERNAME/PASSWORD,
   disabled by default), /health exempt, timing-safe comparison;
4. volcano cloud ASR: bounded synchronous wait + resume by the
   persisted task id (no duplicate submit, no unbounded blocking).
"""
import json
import socket

import httpx
import pytest
from fastapi.testclient import TestClient

from src.agents.minutes.agent import MinutesAgent
from src.agents.minutes.transcriber import Transcriber, VolcanoASREngine
from src.agents.task_agent.agent import TaskAgent
from src.agents.task_agent.extractor import ActionItemExtractor
from src.shared.config import Settings
from src.shared.db import store
from src.shared.schema import Meeting, Participant, TranscriptSegment


@pytest.fixture(autouse=True)
def _clean_store():
    store._data.clear()
    yield
    store._data.clear()


# ------------------------------------------------------------------- fix 1
class _TwoTasksLLM:
    """Returns two DIFFERENT tasks whose source_segment_ids both start
    at segment 3 — the old id scheme collided on task-m1-3."""

    model = "fake"

    @property
    def is_mock(self) -> bool:
        return False

    @property
    def available(self) -> bool:
        return True

    def chat_json(self, messages) -> dict:
        return {"action_items": [
            {"title": "完成证书续期", "owner_raw": "李四", "due_raw": None,
             "priority": "P1", "source_segment_ids": [3, 5]},
            {"title": "提交联调报告", "owner_raw": "王五", "due_raw": None,
             "priority": "P2", "source_segment_ids": [3, 7]},
        ]}


def _meeting_with_transcript(meeting_id: str) -> Meeting:
    return Meeting(
        id=meeting_id, title="撞号会",
        participants=[Participant(user_id="u1", name="李四"),
                      Participant(user_id="u2", name="王五")],
        transcript=[TranscriptSegment(id=i, speaker="说话人", text=f"内容{i}")
                    for i in range(1, 10)],
    )


def test_two_tasks_on_same_first_segment_get_distinct_ids_and_keys():
    out = TaskAgent(extractor=ActionItemExtractor(llm=_TwoTasksLLM())).run(
        _meeting_with_transcript("m1"))
    assert len(out.action_items) == 2
    ids = [i.id for i in out.action_items]
    assert len(set(ids)) == 2  # was: both "task-m1-3"
    assert ids[0] == "task-m1-3"      # first keeps the stable rule-path shape
    assert ids[1] == "task-m1-3-2"    # second disambiguated
    keys = [i.idempotency_key for i in out.action_items]
    assert keys == ["m1:3", "m1:3:2"]  # was: both "m1:3" -> silent dispatch reuse


# ------------------------------------------------------------------- fix 2
@pytest.fixture()
def client(tmp_path, monkeypatch):
    from src.api.main import app

    monkeypatch.setenv("ASR_UPLOAD_DIR", str(tmp_path / "uploads"))
    return TestClient(app)


def _create_meeting(client, mid: str):
    r = client.post("/meetings", json={"id": mid, "title": "上传会"})
    assert r.status_code == 200


def test_upload_size_cap_returns_413_and_leaves_no_partial_file(client, tmp_path, monkeypatch):
    from src.api import main as api_main

    _create_meeting(client, "cap-1")
    monkeypatch.setattr(api_main, "MAX_AUDIO_UPLOAD_BYTES", 4)
    r = client.post("/meetings/cap-1/audio",
                    files={"file": ("rec.wav", b"1234567890", "audio/wav")})
    assert r.status_code == 413
    assert "upload limit" in r.json()["detail"]
    assert not (tmp_path / "uploads" / "cap-1.wav").exists()


def test_upload_replaces_old_file_and_invalidates_cloud_task_id(client, tmp_path):
    _create_meeting(client, "cap-2")
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    # a previous upload under another extension + a previous cloud task
    (upload_dir / "cap-2.flac").write_bytes(b"old-recording")
    m = store.get("cap-2")
    m.asr_task_id = "cloud-task-1"
    store.save(m)

    r = client.post("/meetings/cap-2/audio",
                    files={"file": ("rec.wav", b"new-recording", "audio/wav")})
    assert r.status_code == 200
    assert r.json()["size_bytes"] == len(b"new-recording")
    assert (upload_dir / "cap-2.wav").read_bytes() == b"new-recording"
    assert not (upload_dir / "cap-2.flac").exists()  # orphan of the old upload gone
    assert store.get("cap-2").asr_task_id is None   # old task must not be resumed


def test_download_size_cap_aborts(tmp_path, monkeypatch):
    from src.agents.minutes import transcriber as tr_mod

    # stub DNS to a public address: the SSRF guard resolves the host and
    # this machine's real DNS may map example hosts into reserved ranges
    def fake_getaddrinfo(host, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(tr_mod, "MAX_AUDIO_DOWNLOAD_BYTES", 8)
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200, content=b"chunk-1" * 10))

    class _NeverCalledEngine:  # the cap must fire before any engine runs
        def transcribe(self, audio_path):
            raise AssertionError("download cap should have aborted first")

    t = Transcriber(settings=Settings(), engine_impl=_NeverCalledEngine(),
                    transport=transport)
    assert t.transcribe("https://cdn.example.com/big.wav") == []
    assert t.last_error and "MB limit" in t.last_error


# ------------------------------------------------------------------- fix 3
def test_basic_auth_enforced_when_configured(monkeypatch):
    from src.api.main import app

    monkeypatch.setenv("BASIC_AUTH_USERNAME", "admin")
    monkeypatch.setenv("BASIC_AUTH_PASSWORD", "s3cret")
    c = TestClient(app)
    assert c.get("/meetings").status_code == 401
    assert "basic" in c.get("/meetings").headers["www-authenticate"].lower()
    # health stays open for load-balancer probes
    assert c.get("/health").status_code == 200
    # wrong password -> 401; correct credentials -> full access incl. POST
    assert c.get("/meetings", auth=("admin", "wrong")).status_code == 401
    assert c.get("/meetings", auth=("admin", "s3cret")).status_code == 200
    r = c.post("/meetings", json={"id": "auth-x", "title": "鉴权会"},
               auth=("admin", "s3cret"))
    assert r.status_code == 200
    # malformed / non-Basic Authorization headers are a clean 401, never a 500
    assert c.get("/meetings", headers={"Authorization": "Basic !!!bad!!!"}).status_code == 401
    assert c.get("/meetings", headers={"Authorization": "Bearer xyz"}).status_code == 401


def test_basic_auth_disabled_by_default(monkeypatch):
    from src.api.main import app

    monkeypatch.delenv("BASIC_AUTH_USERNAME", raising=False)
    monkeypatch.delenv("BASIC_AUTH_PASSWORD", raising=False)
    c = TestClient(app)
    assert c.get("/meetings").status_code == 200


# ------------------------------------------------------------------- fix 4
AUDIO_URL = "https://cdn.example.com/meet/rec.wav"

SUCCESS_BODY = {
    "result": {
        "text": "转写完成的一句话。",
        "utterances": [{"text": "转写完成的一句话。", "start_time": 0,
                        "end_time": 2000, "additions": {"speaker": "1"}}],
    },
    "audio_info": {"duration": 2000},
}


def _volcano_fake(query_codes):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls["n"] += 1
        if path.endswith("/submit"):
            return httpx.Response(200, json={},
                                  headers={"X-Api-Status-Code": "20000000"})
        idx = min(calls["n"] - 2, len(query_codes) - 1)  # queries only
        code = query_codes[max(0, idx)]
        if code == "20000000":
            return httpx.Response(200, json=SUCCESS_BODY,
                                  headers={"X-Api-Status-Code": code})
        return httpx.Response(200, json={}, headers={"X-Api-Status-Code": code})

    return httpx.MockTransport(handler)


def test_volcano_resume_by_task_id_never_resubmits():
    s = Settings(volcano_asr_app_id="a", volcano_asr_access_token="t",
                 volcano_asr_poll_interval_sec=0.0)
    fake = _volcano_fake(["20000000"])
    engine = VolcanoASREngine(s, transport=fake)
    segments = engine.transcribe_url(AUDIO_URL, resume_task_id="existing-task-id")
    assert [seg.text for seg in segments] == ["转写完成的一句话。"]
    assert engine.last_task_id == "existing-task-id"


def test_volcano_meeting_resume_flow_ends_with_minutes():
    s = Settings(volcano_asr_app_id="a", volcano_asr_access_token="t",
                 volcano_asr_poll_interval_sec=0.0)
    observed: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed["request_id"] = request.headers["x-api-request-id"]
        observed["paths"] = observed.get("paths", []) + [request.url.path]
        return httpx.Response(200, json=SUCCESS_BODY,
                              headers={"X-Api-Status-Code": "20000000"})

    meeting = Meeting(id="vol-resume", title="续查会", audio_url=AUDIO_URL,
                      asr_task_id="tid-123")
    tr = Transcriber(engine="volcano", settings=s, transport=httpx.MockTransport(handler))
    m = MinutesAgent(transcriber=tr).run(meeting)
    # resumed by the persisted id: query only — no /submit was sent
    assert m.status == "minutes_draft" and m.transcript
    assert m.asr_task_id == "tid-123"
    assert observed["request_id"] == "tid-123"
    assert all(not p.endswith("/submit") for p in observed["paths"])
