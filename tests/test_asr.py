"""Real-ASR wiring, offline: the engine is injected (fake) or its model
is stubbed, and URL downloads go through httpx.MockTransport — the
default test suite NEVER downloads or loads a whisper model.

Covered:
- audio-only meeting transcribes end-to-end with an injected engine
  (leaves needs_transcript behind, diarization note surfaced);
- FasterWhisperEngine segment mapping (ids, placeholder speaker,
  start/end seconds, language / vad_filter kwargs, empty-text skipped);
- failure honesty: engine exception, missing file, HTTP error and
  faster-whisper-not-installed all land in last_error / meeting
  warnings and in the needs_transcript fallback — never an exception;
- POST /meetings/{id}/audio: 200 stores bytes + points audio_url,
  404 unknown meeting, 422 empty file, 422 extension whitelist, and a
  traversal-flavoured filename cannot escape the upload dir;
- importing the transcriber module does not import faster_whisper.
"""
import socket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from src.agents.minutes.agent import MinutesAgent
from src.agents.minutes.transcriber import (
    ASR_SPEAKER_PLACEHOLDER,
    FasterWhisperEngine,
    Transcriber,
)
from src.api.main import app
from src.shared.config import Settings
from src.shared.db import store
from src.shared.schema import Meeting, TranscriptSegment

ROOT = Path(__file__).resolve().parents[1]
client = TestClient(app)


class FakeEngine:
    """Engine-protocol double: records the path it was given."""

    note = "fake engine note (no diarization)"

    def __init__(self, segments=None, exc: Exception | None = None):
        self._segments = segments if segments is not None else [
            TranscriptSegment(id=1, speaker="张三", start_sec=0.0, end_sec=5.0,
                              text="我们决定将上线时间推迟到十一月八号。"),
            TranscriptSegment(id=2, speaker="李四", start_sec=5.0, end_sec=9.0,
                              text="李四负责支付渠道证书续期，下周三之前完成。"),
        ]
        self._exc = exc
        self.seen_paths: list[str] = []

    def transcribe(self, audio_path: str):
        self.seen_paths.append(audio_path)
        assert Path(audio_path).is_file()  # engine must get a real file
        if self._exc is not None:
            raise self._exc
        return list(self._segments)


# ------------------------------------------------------------- happy path
def test_audio_only_meeting_transcribes_with_fake_engine(tmp_path):
    audio = tmp_path / "meeting.mp3"
    audio.write_bytes(b"fake-audio-bytes")
    engine = FakeEngine()
    tr = Transcriber(engine="faster-whisper", engine_impl=engine)
    meeting = Meeting(id="asr-ok", title="支付联调会", audio_url=str(audio))
    m = MinutesAgent(transcriber=tr).run(meeting)
    assert m.status == "minutes_draft"  # NOT needs_transcript
    assert m.minutes is not None
    assert [s.text for s in m.transcript] == [s.text for s in engine._segments]
    assert engine.seen_paths == [str(audio)]  # local path passed through, not copied
    assert tr.last_error is None
    # the diarization caveat is surfaced to humans, not hidden
    assert any("diarization" in w for w in m.warnings)


def test_provided_segments_pass_through_unchanged():
    segs = [TranscriptSegment(id=1, speaker="王五", text="现成的逐字稿")]
    tr = Transcriber(engine="faster-whisper", engine_impl=FakeEngine())
    assert tr.transcribe("https://example.com/ignored.mp3", segments=segs) == segs


# --------------------------------------------- FasterWhisperEngine mapping
def _engine_with_stub_model(settings: Settings, raw_segments):
    engine = FasterWhisperEngine(settings)
    calls: dict = {}

    class FakeModel:
        def transcribe(self, path, **kwargs):
            calls["path"] = path
            calls.update(kwargs)
            return iter(raw_segments), SimpleNamespace(language="zh", duration=6.25)

    engine._model = FakeModel()  # bypass lazy load: no download, no import
    return engine, calls


def test_faster_whisper_engine_maps_segments():
    raw = [
        SimpleNamespace(start=0.0, end=2.5, text=" 第一句。 "),
        SimpleNamespace(start=2.5, end=4.0, text="   "),  # empty -> skipped
        SimpleNamespace(start=4.0, end=6.25, text="第二句"),
    ]
    engine, calls = _engine_with_stub_model(Settings(asr_language="zh"), raw)
    out = engine.transcribe("whatever.mp3")
    assert [s.id for s in out] == [1, 2]  # sequential ids, gap-free
    assert out[0].speaker == ASR_SPEAKER_PLACEHOLDER == "发言人"
    assert (out[0].start_sec, out[0].end_sec, out[0].text) == (0.0, 2.5, "第一句。")
    assert (out[1].start_sec, out[1].end_sec, out[1].text) == (4.0, 6.25, "第二句")
    assert calls["language"] == "zh" and calls["vad_filter"] is True


def test_faster_whisper_engine_auto_language_omits_kwarg():
    engine, calls = _engine_with_stub_model(
        Settings(asr_language="auto"),
        [SimpleNamespace(start=0.0, end=1.0, text="hello")])
    engine.transcribe("x.mp3")
    assert "language" not in calls and calls["vad_filter"] is True


def test_module_import_does_not_load_faster_whisper():
    code = ("import sys; import src.agents.minutes.transcriber; "
            "assert 'faster_whisper' not in sys.modules, 'import must stay lazy'")
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr


# --------------------------------------------------------- failure honesty
def test_engine_exception_becomes_needs_transcript_with_reason(tmp_path):
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"x")
    tr = Transcriber(engine="faster-whisper",
                     engine_impl=FakeEngine(exc=RuntimeError("model exploded")))
    meeting = Meeting(id="asr-err", title="录音会", audio_url=str(audio))
    m = MinutesAgent(transcriber=tr).run(meeting)
    assert m.status == "needs_transcript"
    assert m.minutes is None
    assert tr.last_error and "model exploded" in tr.last_error
    assert any("model exploded" in w for w in m.warnings)


def test_missing_local_file_reason(tmp_path):
    tr = Transcriber(engine="faster-whisper", engine_impl=FakeEngine())
    assert tr.transcribe(str(tmp_path / "nope.mp3")) == []
    assert tr.last_error and "not found" in tr.last_error


def test_mock_engine_gives_actionable_reason():
    tr = Transcriber(engine="mock")
    assert tr.transcribe("https://example.com/a.mp3") == []
    assert tr.last_error and "faster-whisper" in tr.last_error
    assert 'pip install -e ".[asr]"' in tr.last_error


def test_faster_whisper_not_installed_reason(monkeypatch, tmp_path):
    # Simulate the optional package being absent (it may be installed in
    # the dev venv — either way the error path must read the same).
    monkeypatch.setitem(sys.modules, "faster_whisper", None)
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"x")
    tr = Transcriber(engine="faster-whisper")  # real engine resolution
    assert tr.transcribe(str(audio)) == []
    assert tr.last_error and "not installed" in tr.last_error
    assert 'pip install -e ".[asr]"' in tr.last_error


def _stub_public_dns(monkeypatch):
    """The SSRF guard resolves hostnames; stub it so the offline suite
    never depends on real DNS (any hostname -> a public address)."""
    def fake_getaddrinfo(host, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)


# --------------------------------------------------------------- URL source
def test_url_audio_downloaded_then_transcribed_and_temp_cleaned(monkeypatch):
    _stub_public_dns(monkeypatch)
    payload = b"url-audio-bytes"

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://example.com/rec/meeting.mp3"
        return httpx.Response(200, content=payload)

    engine = FakeEngine()
    tr = Transcriber(engine="faster-whisper", engine_impl=engine,
                     transport=httpx.MockTransport(handler))
    out = tr.transcribe("https://example.com/rec/meeting.mp3")
    assert out and out[0].text
    assert engine.seen_paths, "engine never received the downloaded file"
    # the downloaded temp file is removed after transcription
    assert not Path(engine.seen_paths[0]).exists()


def test_url_download_http_error_reason(monkeypatch):
    _stub_public_dns(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b"nope")

    tr = Transcriber(engine="faster-whisper", engine_impl=FakeEngine(),
                     transport=httpx.MockTransport(handler))
    assert tr.transcribe("https://example.com/x.mp3") == []
    assert tr.last_error and "HTTP 404" in tr.last_error


# ------------------------------------------------------------ upload endpoint
@pytest.fixture(autouse=True)
def _clean_store_and_upload_dir(tmp_path, monkeypatch):
    store._data.clear()
    monkeypatch.setenv("ASR_UPLOAD_DIR", str(tmp_path / "uploads"))
    yield tmp_path / "uploads"
    store._data.clear()


def _create(mid: str):
    return client.post("/meetings", json={"id": mid, "title": "录音会"})


def test_upload_audio_200(_clean_store_and_upload_dir):
    upload_dir = _clean_store_and_upload_dir
    assert _create("up-1").status_code == 200
    r = client.post("/meetings/up-1/audio",
                    files={"file": ("rec.mp3", b"audio-bytes")})
    assert r.status_code == 200
    body = r.json()
    assert body["filename"] == "up-1.mp3"
    assert body["size_bytes"] == len(b"audio-bytes")
    saved = Path(body["audio_url"])
    assert saved.parent == upload_dir and saved.name == "up-1.mp3"
    assert saved.read_bytes() == b"audio-bytes"
    # the meeting now points at the stored recording
    assert client.get("/meetings/up-1").json()["audio_url"] == body["audio_url"]


def test_upload_audio_unknown_meeting_404():
    r = client.post("/meetings/nope/audio", files={"file": ("rec.mp3", b"x")})
    assert r.status_code == 404


def test_upload_audio_empty_file_422():
    assert _create("up-2").status_code == 200
    r = client.post("/meetings/up-2/audio", files={"file": ("rec.mp3", b"")})
    assert r.status_code == 422


def test_upload_audio_extension_whitelist(_clean_store_and_upload_dir):
    upload_dir = _clean_store_and_upload_dir
    assert _create("up-3").status_code == 200
    for name in ("evil.exe", "notes.txt", "archive.mp3.sh", "noextension"):
        r = client.post("/meetings/up-3/audio", files={"file": (name, b"x")})
        assert r.status_code == 422, name
    assert not upload_dir.exists() or not list(upload_dir.iterdir())


def test_upload_audio_traversal_filename_stays_in_upload_dir(_clean_store_and_upload_dir):
    upload_dir = _clean_store_and_upload_dir
    assert _create("up-4").status_code == 200
    # extension is fine (.mp3) but the name tries to escape — the stored
    # name is built from the meeting id only, so it cannot.
    r = client.post("/meetings/up-4/audio",
                    files={"file": ("../../evil.mp3", b"x")})
    assert r.status_code == 200
    saved = Path(r.json()["audio_url"])
    assert saved.parent == upload_dir and saved.name == "up-4.mp3"
