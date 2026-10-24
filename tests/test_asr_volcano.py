"""Volcano Doubao cloud ASR (ASR_ENGINE=volcano), fully offline:
httpx.MockTransport scripts the submit/query endpoints, including the
X-Api-Status-Code response-header state machine.

Covered:
- submit/query protocol shape: all five X-Api-* headers, resource id,
  the SAME X-Api-Request-Id on submit and every query, submit body
  (public URL + format + bigmodel request flags), empty {} query body;
- polling processing -> processing -> success; utterance ms -> sec
  conversion and speaker mapping (additions.speaker -> 说话人N);
- the two live-learned hard constraints: flac is refused client-side
  with ZERO http requests, and local paths are refused with guidance;
- silence (20000003) -> [] with the 人声 reason reaching the meeting
  warning via needs_transcript; error codes surface code + message;
- the task id is persisted to the store AT SUBMIT TIME (asserted from
  inside the first query call, i.e. before polling has finished);
- missing credentials name the missing env var; no-utterance success
  falls back to result.text as one segment; extensionless URL assumes
  wav and says so in engine.note.
"""
import json

import httpx
import pytest

from src.agents.minutes.agent import MinutesAgent
from src.agents.minutes.transcriber import (
    ASR_SPEAKER_PLACEHOLDER,
    ASRError,
    Transcriber,
    VolcanoASREngine,
)
from src.shared.config import Settings
from src.shared.db import InMemoryDB
from src.shared.schema import Meeting

SUCCESS_BODY = {
    "result": {
        "text": "我们决定将上线时间推迟到十一月八号。李四负责支付渠道证书续期。",
        "utterances": [
            {"text": "我们决定将上线时间推迟到十一月八号。",
             "start_time": 0, "end_time": 3500, "additions": {"speaker": "1"}},
            {"text": "李四负责支付渠道证书续期。",
             "start_time": 3500, "end_time": 7200, "additions": {"speaker": "2"}},
        ],
    },
    "audio_info": {"duration": 7200},
}

AUDIO_URL = "https://cdn.example.com/meet/rec.wav"


def volcano_settings(**overrides) -> Settings:
    base = dict(
        volcano_asr_app_id="test-app-id",
        volcano_asr_access_token="test-access-token",
        volcano_asr_poll_interval_sec=0.0,
        volcano_asr_timeout_sec=60.0,
    )
    base.update(overrides)
    return Settings(**base)


class VolcanoFake:
    """Scripted stand-in for submit+query. Query returns the given
    status codes in order (the last one repeats)."""

    def __init__(self, *, submit_code="20000000", query_codes=("20000000",),
                 body=None, submit_message="", query_message=""):
        self.submit_code = submit_code
        self.query_codes = list(query_codes)
        self.body = SUCCESS_BODY if body is None else body
        self.submit_message = submit_message
        self.query_message = query_message
        self.requests: list[tuple[str, httpx.Headers, dict]] = []
        self._query_calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode() or "{}")
        self.requests.append((request.url.path, request.headers, payload))
        if request.url.path.endswith("/submit"):
            return httpx.Response(200, json={}, headers={
                "X-Api-Status-Code": self.submit_code,
                "X-Api-Message": self.submit_message})
        idx = min(self._query_calls, len(self.query_codes) - 1)
        self._query_calls += 1
        code = self.query_codes[idx]
        if code == "20000000":
            return httpx.Response(200, json=self.body,
                                  headers={"X-Api-Status-Code": code})
        return httpx.Response(200, json={}, headers={
            "X-Api-Status-Code": code, "X-Api-Message": self.query_message})

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


def _engine(fake: VolcanoFake, **settings_overrides) -> VolcanoASREngine:
    return VolcanoASREngine(volcano_settings(**settings_overrides),
                            transport=fake.transport)


# ------------------------------------------------------------ protocol shape
def test_submit_and_query_headers_body_and_shared_request_id():
    fake = VolcanoFake(query_codes=("20000001", "20000002", "20000000"))
    segments = _engine(fake).transcribe_url(AUDIO_URL)
    assert segments  # polling sequence eventually succeeded
    assert len(fake.requests) == 4  # 1 submit + 3 queries
    request_ids = set()
    for path, headers, payload in fake.requests:
        assert headers["x-api-app-key"] == "test-app-id"
        assert headers["x-api-access-key"] == "test-access-token"
        assert headers["x-api-resource-id"] == "volc.seedasr.auc"
        assert headers["x-api-sequence"] == "-1"
        assert "application/json" in headers["content-type"]
        request_ids.add(headers["x-api-request-id"])
    assert len(request_ids) == 1 and request_ids.pop()  # one uuid per task
    submit_path, _, submit_body = fake.requests[0]
    assert submit_path.endswith("/api/v3/auc/bigmodel/submit")
    assert submit_body["audio"] == {"url": AUDIO_URL, "format": "wav"}
    req = submit_body["request"]
    assert req["model_name"] == "bigmodel"
    assert req["enable_itn"] is True and req["enable_punc"] is True
    assert req["show_utterances"] is True and req["enable_speaker_info"] is True
    for path, _, payload in fake.requests[1:]:
        assert path.endswith("/api/v3/auc/bigmodel/query")
        assert payload == {}


def test_polling_maps_ms_to_seconds_and_speakers():
    fake = VolcanoFake(query_codes=("20000001", "20000000"))
    engine = _engine(fake)
    segments = engine.transcribe_url(AUDIO_URL)
    assert [s.id for s in segments] == [1, 2]
    assert (segments[0].start_sec, segments[0].end_sec) == (0.0, 3.5)
    assert (segments[1].start_sec, segments[1].end_sec) == (3.5, 7.2)
    assert segments[0].speaker == "说话人1"
    assert segments[1].speaker == "说话人2"
    assert segments[0].text == "我们决定将上线时间推迟到十一月八号。"
    # volcano returns speaker info -> NO no-diarization caveat
    assert engine.note is None
    assert engine.last_task_id  # kept on the engine too


def test_utterance_without_speaker_additions_uses_placeholder():
    body = {"result": {"text": "没有说话人标注的一句。",
                        "utterances": [{"text": "没有说话人标注的一句。",
                                        "start_time": 100, "end_time": 900}]},
            "audio_info": {"duration": 900}}
    segments = _engine(VolcanoFake(body=body)).transcribe_url(AUDIO_URL)
    assert len(segments) == 1
    assert segments[0].speaker == ASR_SPEAKER_PLACEHOLDER
    assert (segments[0].start_sec, segments[0].end_sec) == (0.1, 0.9)


def test_no_utterances_falls_back_to_full_text_single_segment():
    body = {"result": {"text": "只有全文没有分句。"}, "audio_info": {"duration": 2500}}
    segments = _engine(VolcanoFake(body=body)).transcribe_url(AUDIO_URL)
    assert len(segments) == 1
    assert segments[0].text == "只有全文没有分句。"
    assert segments[0].end_sec == 2.5


def test_extensionless_url_assumes_wav_and_says_so():
    fake = VolcanoFake()
    engine = _engine(fake)
    engine.transcribe_url("https://cdn.example.com/stream?id=42")
    _, _, submit_body = fake.requests[0]
    assert submit_body["audio"]["format"] == "wav"
    assert engine.note and "wav" in engine.note


# --------------------------------------------------------- hard constraints
def test_flac_is_refused_client_side_without_any_http_request():
    fake = VolcanoFake()
    engine = _engine(fake)
    with pytest.raises(ASRError) as excinfo:
        engine.transcribe_url("https://cdn.example.com/meet/rec.flac")
    assert "flac" in str(excinfo.value)
    assert fake.requests == []  # never submitted — a flac task would hang server-side
    # ...and through Transcriber it becomes last_error, not an exception
    tr = Transcriber(engine="volcano", settings=volcano_settings(),
                     transport=fake.transport)
    assert tr.transcribe("https://cdn.example.com/meet/rec.flac") == []
    assert tr.last_error and "flac" in tr.last_error
    assert fake.requests == []


def test_local_path_is_refused_with_guidance():
    fake = VolcanoFake()
    tr = Transcriber(engine="volcano", settings=volcano_settings(),
                     transport=fake.transport)
    assert tr.transcribe("/data/uploads/meeting-1.mp3") == []
    assert tr.last_error is not None
    assert "公网可访问" in tr.last_error
    assert "faster-whisper" in tr.last_error
    assert fake.requests == []
    # the engine's local-file protocol entry refuses the same way
    with pytest.raises(ASRError) as excinfo:
        _engine(fake).transcribe("/data/uploads/meeting-1.mp3")
    assert "公网可访问" in str(excinfo.value)


# ------------------------------------------------------------- status codes
def test_silence_returns_empty_and_reason_reaches_meeting_warning():
    fake = VolcanoFake(query_codes=("20000003",))
    engine = _engine(fake)
    assert engine.transcribe_url(AUDIO_URL) == []
    assert engine.empty_reason and "音频中未检测到人声" in engine.empty_reason

    meeting = Meeting(id="vol-silent", title="静音会", audio_url=AUDIO_URL)
    tr = Transcriber(engine="volcano", settings=volcano_settings(),
                     transport=VolcanoFake(query_codes=("20000003",)).transport)
    m = MinutesAgent(transcriber=tr).run(meeting)
    assert m.status == "needs_transcript"
    assert m.minutes is None
    assert any("音频中未检测到人声" in w for w in m.warnings)


def test_submit_error_code_and_message_surface():
    fake = VolcanoFake(submit_code="45000001",
                        submit_message="audio format not supported")
    with pytest.raises(ASRError) as excinfo:
        _engine(fake).transcribe_url(AUDIO_URL)
    assert "45000001" in str(excinfo.value)
    assert "audio format not supported" in str(excinfo.value)

    meeting = Meeting(id="vol-err", title="参数错会", audio_url=AUDIO_URL)
    tr = Transcriber(engine="volcano", settings=volcano_settings(),
                     transport=VolcanoFake(
                         submit_code="45000001",
                         submit_message="audio format not supported").transport)
    m = MinutesAgent(transcriber=tr).run(meeting)
    assert m.status == "needs_transcript"
    assert any("45000001" in w for w in m.warnings)


def test_polling_timeout_is_an_actionable_error():
    fake = VolcanoFake(query_codes=("20000001",))  # never finishes
    engine = _engine(fake, volcano_asr_timeout_sec=0.0)
    with pytest.raises(ASRError) as excinfo:
        engine.transcribe_url(AUDIO_URL)
    assert "timed out" in str(excinfo.value)


# ---------------------------------------------------------------- credentials
def test_missing_credentials_name_the_missing_env_var():
    fake = VolcanoFake()
    engine = VolcanoASREngine(
        volcano_settings(volcano_asr_app_id="", volcano_asr_access_token=""),
        transport=fake.transport)
    with pytest.raises(ASRError) as excinfo:
        engine.transcribe_url(AUDIO_URL)
    assert "VOLCANO_ASR_APP_ID" in str(excinfo.value)
    assert "VOLCANO_ASR_ACCESS_TOKEN" in str(excinfo.value)
    assert fake.requests == []

    engine = VolcanoASREngine(
        volcano_settings(volcano_asr_access_token=""),
        transport=fake.transport)
    with pytest.raises(ASRError) as excinfo:
        engine.transcribe_url(AUDIO_URL)
    assert "VOLCANO_ASR_ACCESS_TOKEN" in str(excinfo.value)
    assert fake.requests == []


# ------------------------------------------------------------ task id 落库
def test_task_id_is_persisted_to_store_at_submit_time():
    store = InMemoryDB()
    meeting = Meeting(id="vol-persist", title="云端转写会", audio_url=AUDIO_URL)
    store.save(meeting)
    observed: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/submit"):
            return httpx.Response(200, json={},
                                  headers={"X-Api-Status-Code": "20000000"})
        # Inside the FIRST query — polling has not finished, yet the
        # task id must already be in the store (the whole point: a
        # crash now would not lose the task).
        observed["request_id"] = request.headers["x-api-request-id"]
        stored = store.get("vol-persist")
        observed["stored_task_id"] = stored.asr_task_id if stored else None
        return httpx.Response(200, json=SUCCESS_BODY,
                              headers={"X-Api-Status-Code": "20000000"})

    tr = Transcriber(engine="volcano", settings=volcano_settings(),
                     transport=httpx.MockTransport(handler))
    m = MinutesAgent(transcriber=tr, store=store).run(meeting)
    assert observed["request_id"]
    assert observed["stored_task_id"] == observed["request_id"]
    assert m.asr_task_id == observed["request_id"]
    assert store.get("vol-persist").asr_task_id == observed["request_id"]
    assert m.status == "minutes_draft"
    # volcano transcript carries speakers -> no diarization caveat
    assert not any("diarization" in w for w in m.warnings)
