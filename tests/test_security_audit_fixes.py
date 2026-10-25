"""Regressions for the security-audit findings fixed 2026-10-25:

F1  fail-closed REQUIRE_AUTH=true refuses to start without credentials;
F2  CSRF guard (cross-site Origin / Sec-Fetch-Site rejected on
    state-changing methods) + X-Frame-Options / frame-ancestors headers;
F3  SSRF guard on the server-side audio fetch (loopback / private /
    link-local / reserved targets refused before any request, redirect
    hops validated, error messages leak no topology);
F4  compose port removal is asserted by inspection of the file itself
    (no runtime surface — see the last test in this file).

Bounded local evidence only: TestClient + loopback + MockTransport,
dummy principals, no external network.
"""
import json
import socket
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from src.agents.minutes.transcriber import Transcriber
from src.shared.config import Settings
from src.shared.db import store

SAMPLE = json.loads(
    (Path(__file__).resolve().parents[1] / "examples" / "sample_meeting.json")
    .read_text(encoding="utf-8"))

PUBLIC_IP = "93.184.216.34"  # example.com's real address — used for DNS stubs


@pytest.fixture(autouse=True)
def _clean_store():
    store._data.clear()
    yield
    store._data.clear()


@pytest.fixture()
def auth_env(monkeypatch):
    monkeypatch.setenv("BASIC_AUTH_USERNAME", "admin")
    monkeypatch.setenv("BASIC_AUTH_PASSWORD", "pw")
    monkeypatch.setenv("LLM_MODE", "mock")
    return ("admin", "pw")


def _stub_public_dns(monkeypatch):
    """Deterministic, DNS-server-free resolution: every hostname maps to
    a PUBLIC address (loopback/literal-IP hosts are checked without
    resolution and stay blocked)."""
    def fake_getaddrinfo(host, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_IP, 0))]
    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)


# ------------------------------------------------------------------- F1
def test_require_auth_fails_closed_without_credentials(monkeypatch):
    from src.api.main import app

    monkeypatch.setenv("REQUIRE_AUTH", "true")
    monkeypatch.delenv("BASIC_AUTH_USERNAME", raising=False)
    monkeypatch.delenv("BASIC_AUTH_PASSWORD", raising=False)
    with pytest.raises(RuntimeError, match="fail-closed"):
        with TestClient(app):
            pass


def test_require_auth_starts_when_credentials_present(monkeypatch, auth_env):
    from src.api.main import app

    monkeypatch.setenv("REQUIRE_AUTH", "true")
    with TestClient(app) as c:
        assert c.get("/health").status_code == 200
        assert c.get("/meetings", auth=auth_env).status_code == 200


# ------------------------------------------------------------------- F2
def _prepare_awaiting_minutes(c, auth, mid):
    payload = dict(SAMPLE)
    payload["id"] = mid
    payload.pop("audio_url", None)
    payload["status"] = "scheduled"
    assert c.post("/meetings", json=payload, auth=auth).status_code == 200
    assert c.post(f"/meetings/{mid}/process", json={}, auth=auth).status_code == 200
    assert store.get(mid).status == "awaiting_minutes_confirmation"


def test_cross_site_origin_rejected_and_state_unchanged(auth_env):
    from src.api.main import app

    c = TestClient(app)
    _prepare_awaiting_minutes(c, auth_env, "csrf-1")
    # cross-site form-POST shape: urlencoded body, foreign Origin
    r = c.post("/meetings/csrf-1/confirm-minutes", content=b"",
               headers={"Content-Type": "application/x-www-form-urlencoded",
                        "Origin": "http://evil.example"},
               auth=auth_env)
    assert r.status_code == 403
    assert store.get("csrf-1").status == "awaiting_minutes_confirmation"


def test_sec_fetch_cross_site_rejected_even_without_origin(auth_env):
    from src.api.main import app

    c = TestClient(app)
    _prepare_awaiting_minutes(c, auth_env, "csrf-2")
    r = c.post("/meetings/csrf-2/confirm-minutes", content=b"",
               headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate"},
               auth=auth_env)
    assert r.status_code == 403
    assert store.get("csrf-2").status == "awaiting_minutes_confirmation"


def test_same_origin_and_server_to_server_requests_pass(auth_env):
    from src.api.main import app

    c = TestClient(app)
    _prepare_awaiting_minutes(c, auth_env, "csrf-3")
    # browser same-origin POST fetch: Origin matches the request host
    r = c.post("/meetings/csrf-3/confirm-minutes",
               headers={"Origin": "http://testserver"}, auth=auth_env)
    assert r.status_code == 200
    # curl / webhook shape: no Origin, no Sec-Fetch headers
    r = c.post("/meetings/csrf-3/confirm-tasks", auth=auth_env)
    assert r.status_code == 200
    assert store.get("csrf-3").status == "dispatched"


def test_frame_guards_on_responses(auth_env):
    from src.api.main import app

    c = TestClient(app)
    r = c.get("/meetings", auth=auth_env)
    assert r.status_code == 200
    assert r.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]


# ------------------------------------------------------------------- F3
class _NoNetwork:
    """A transport that must never be reached when the guard blocks."""

    def __call__(self, request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"request sent to blocked target: {request.url}")


class _RecordingEngine:
    def __init__(self):
        self.seen_paths = []

    def transcribe(self, audio_path):
        self.seen_paths.append(audio_path)
        return []


@pytest.mark.parametrize("target", [
    "http://127.0.0.1:9/metadata",
    "http://169.254.169.254/latest/meta-data/",
    "http://10.1.2.3/internal.wav",
    "http://[::1]:9/x.wav",
    "http://192.168.1.10/rec.wav",
])
def test_ssrf_non_public_targets_blocked_before_any_request(target, monkeypatch):
    tr = Transcriber(settings=Settings(), engine_impl=_RecordingEngine(),
                     transport=httpx.MockTransport(_NoNetwork()))
    assert tr.transcribe(target) == []
    assert tr.last_error and "SSRF guard" in tr.last_error
    # no topology echo: neither host nor path may appear in the reason
    assert "127.0.0.1" not in tr.last_error
    assert "169.254" not in tr.last_error
    assert "metadata" not in tr.last_error


def test_ssrf_hostname_resolving_to_loopback_blocked(monkeypatch):
    # DNS rebinding shape: a hostname that resolves into a private range
    def fake_getaddrinfo(host, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]
    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    tr = Transcriber(settings=Settings(), engine_impl=_RecordingEngine(),
                     transport=httpx.MockTransport(_NoNetwork()))
    assert tr.transcribe("http://internal-cdn.example/rec.wav") == []
    assert tr.last_error and "SSRF guard" in tr.last_error


def test_ssrf_redirect_into_private_target_blocked(monkeypatch):
    _stub_public_dns(monkeypatch)
    requests_seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://127.0.0.1:9/secret.wav"})

    engine = _RecordingEngine()
    tr = Transcriber(settings=Settings(), engine_impl=engine,
                     transport=httpx.MockTransport(handler))
    assert tr.transcribe("http://public.example/rec.wav") == []
    assert tr.last_error and "SSRF guard" in tr.last_error
    assert len(requests_seen) == 1  # hop 1 fetched, hop 2 refused before sending
    assert not engine.seen_paths


def test_public_url_download_still_works(monkeypatch):
    _stub_public_dns(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"public-audio-bytes")

    engine = _RecordingEngine()
    tr = Transcriber(settings=Settings(), engine_impl=engine,
                     transport=httpx.MockTransport(handler))
    assert tr.transcribe("http://public.example/rec.mp3") == []
    assert len(engine.seen_paths) == 1  # download happened and reached the engine
    assert tr.last_error is None


# ------------------------------------------------------------------- F4
def test_compose_does_not_publish_datastore_ports():
    text = (Path(__file__).resolve().parents[1] / "docker-compose.yml").read_text(
        encoding="utf-8")
    assert '"5432:5432"' not in text
    assert '"6379:6379"' not in text
    assert '"8000:8000"' in text  # the API port stays published
