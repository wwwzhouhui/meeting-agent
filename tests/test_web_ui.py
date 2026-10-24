"""Web UI: the static frontend is served by the API, the meeting-list
endpoint the UI depends on exists, and — the anti-drift guard — every
API path the frontend calls really exists in the backend route table."""
import copy
import re
from pathlib import Path

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from src.api.main import app

client = TestClient(app)
ROOT = Path(__file__).resolve().parents[1]
WEB_DIR = ROOT / "web"


def test_index_page_served_with_key_dom():
    r = client.get("/")
    assert r.status_code == 200
    html = r.text
    assert 'id="app"' in html
    assert 'id="meeting-list"' in html
    assert 'id="detail"' in html
    assert 'id="tools-panel"' in html
    assert "会议助手" in html


def test_static_assets_served():
    js = client.get("/app.js")
    assert js.status_code == 200
    assert "/meetings" in js.text
    css = client.get("/style.css")
    assert css.status_code == 200
    assert ".badge" in css.text


def test_api_routes_not_swallowed_by_static_mount():
    assert client.get("/health").json() == {"ok": True}
    assert client.get("/meetings/definitely-not-a-meeting").status_code == 404


def test_meeting_list_endpoint(sample_meeting):
    payload = copy.deepcopy(sample_meeting).model_dump(mode="json")
    payload["id"] = "web-ui-list-1"
    payload["events"] = []
    r = client.post("/meetings", json=payload)
    assert r.status_code == 200, r.text
    r = client.get("/meetings")
    assert r.status_code == 200
    assert any(m["id"] == "web-ui-list-1" for m in r.json())


def _canon(path: str) -> str:
    path = path.split("?")[0].split("#")[0]
    path = re.sub(r"\$\{[^}]+\}", "{}", path)   # JS template ${id}
    path = re.sub(r"\{[^}]+\}", "{}", path)     # FastAPI {meeting_id}
    return path


def test_frontend_paths_exist_in_backend_routes():
    js = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    used = set()
    for m in re.finditer(r"(?:api|fetch)\(\s*([`'\"])(.*?)\1", js):
        path = m.group(2)
        if path.startswith("/"):
            used.add(_canon(path))
    assert used, "no API paths extracted from app.js — the guard itself is broken"
    routes = {_canon(r.path) for r in app.routes if isinstance(r, APIRoute)}
    missing = used - routes
    assert not missing, f"frontend calls endpoints that do not exist: {sorted(missing)}"
