"""Feishu tools against httpx MockTransport (fully offline).

These tests verify the real request shapes, not just return values:
- tenant token is fetched once and reused;
- Calendar timestamps are seconds, Task due timestamps milliseconds;
- invalid attendee/owner ids are skipped with warnings;
- webhook signing is reproducible;
- provider code != 0 becomes FeishuAPIError and, through the task
  tracker, a per-item dispatch_failed rather than a crashed flow.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from src.agents.task_agent.tracker import TaskTracker
from src.shared.config import Settings
from src.shared.schema import ActionItem
from src.tools.calendar.feishu import FeishuCalendarTool
from src.tools.factory import build_calendar_tool, build_notify_tool, build_task_tool
from src.tools.feishu_client import FeishuAPIError, FeishuClient, map_user_id
from src.tools.notify.feishu import FeishuNotifyTool
from src.tools.tasks.feishu_task import FeishuTaskTool

TZ = ZoneInfo("Asia/Shanghai")


def _json_response(payload, status=200):
    return httpx.Response(status, json=payload)


class Recorder:
    def __init__(self, api_handler):
        self.api_handler = api_handler
        self.requests = []
        self.token_calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8")) if request.content else {}
        record = {
            "method": request.method,
            "url": request.url,
            "path": request.url.path,
            "params": dict(request.url.params),
            "headers": request.headers,
            "json": body,
        }
        self.requests.append(record)
        if request.url.path.endswith("/tenant_access_token/internal"):
            self.token_calls += 1
            assert body == {"app_id": "cli_test", "app_secret": "test-secret"}
            return _json_response({
                "code": 0,
                "msg": "ok",
                "tenant_access_token": "t-test-token",
                "expire": 7200,
            })
        return self.api_handler(record)

    @property
    def transport(self):
        return httpx.MockTransport(self)


def _client(recorder: Recorder, **kw):
    return FeishuClient("cli_test", "test-secret", transport=recorder.transport, **kw)


def test_map_user_id_and_malformed_settings_map():
    mapping = {"u-lisi": "ou_lisi"}
    assert map_user_id("u-lisi", mapping) == "ou_lisi"
    assert map_user_id("ou_direct", mapping) == "ou_direct"
    assert map_user_id("lisi@example.com", mapping) is None
    assert map_user_id("u-lisi", {"u-lisi": "not-open-id"}) is None
    assert Settings(feishu_user_map="{not-json").feishu_user_map_dict == {}


def test_tenant_token_reused_and_calendar_seconds_vs_task_milliseconds():
    def api(record):
        path = record["path"]
        if path == "/open-apis/calendar/v4/calendars/cal-1/events" and record["method"] == "POST":
            assert record["headers"]["authorization"] == "Bearer t-test-token"
            return _json_response({"code": 0, "data": {"event": {"event_id": "evt-1"}}})
        if path.endswith("/events/evt-1/attendees"):
            return _json_response({"code": 0, "data": {"attendees": []}})
        if path == "/open-apis/task/v2/tasks":
            assert record["headers"]["authorization"] == "Bearer t-test-token"
            return _json_response({"code": 0, "data": {"task": {"guid": "task-guid-1"}}})
        raise AssertionError(f"unexpected request {record['method']} {path}")

    recorder = Recorder(api)
    client = _client(recorder)
    calendar = FeishuCalendarTool(
        client=client,
        calendar_id="cal-1",
        user_map={"u-lisi": "ou_lisi"},
        timezone="Asia/Shanghai",
    )
    tasks = FeishuTaskTool(
        client=client,
        user_map={"u-lisi": "ou_lisi"},
        timezone="Asia/Shanghai",
    )

    event = calendar.create_event(
        "评审会",
        "2026-10-27T10:00:00",
        ["u-lisi", "ou_direct", "张三", "lisi@example.com"],
        description="议程",
        end="2026-10-27T11:30:00",
    )
    task = tasks.create_task(
        "完成证书续期", owner_id="u-lisi", due_date="2026-10-28", idempotency_key="meeting:2"
    )

    assert event["id"] == "evt-1"
    assert event["attendees_added"] == ["ou_lisi", "ou_direct"]
    assert event["skipped_attendees"] == ["张三", "lisi@example.com"]
    assert any("skipped non-open_id attendees" in w for w in event["warnings"])
    assert task["id"] == "task-guid-1"

    create_event_body = next(r["json"] for r in recorder.requests if r["path"].endswith("/cal-1/events"))
    expected_start = str(int(datetime(2026, 10, 27, 10, 0, tzinfo=TZ).timestamp()))
    expected_end = str(int(datetime(2026, 10, 27, 11, 30, tzinfo=TZ).timestamp()))
    assert create_event_body["start_time"] == {"timestamp": expected_start, "timezone": "Asia/Shanghai"}
    assert create_event_body["end_time"] == {"timestamp": expected_end, "timezone": "Asia/Shanghai"}
    assert len(expected_start) == 10  # seconds

    attendee_body = next(r["json"] for r in recorder.requests if r["path"].endswith("/attendees"))
    assert attendee_body["attendees"] == [
        {"type": "user", "attendee_id": "ou_lisi"},
        {"type": "user", "attendee_id": "ou_direct"},
    ]

    task_body = next(r["json"] for r in recorder.requests if r["path"] == "/open-apis/task/v2/tasks")
    expected_due_ms = str(int(datetime(2026, 10, 28, 23, 59, 59, tzinfo=TZ).timestamp() * 1000))
    assert task_body["due"] == {"timestamp": expected_due_ms, "is_all_day": False}
    assert len(expected_due_ms) == 13  # milliseconds: the Feishu calendar/task difference
    assert task_body["members"] == [{"id": "ou_lisi", "type": "user", "role": "assignee"}]

    # Three OpenAPI calls (event, attendees, task) shared one cached token.
    assert recorder.token_calls == 1


def test_task_invalid_owner_is_omitted_with_warning_and_update_complete():
    def api(record):
        if record["path"] == "/open-apis/task/v2/tasks":
            return _json_response({"code": 0, "data": {"task": {"guid": "guid-2"}}})
        if record["path"] == "/open-apis/task/v2/tasks/guid-2" and record["method"] == "PATCH":
            return _json_response({"code": 0, "data": {"task": {"guid": "guid-2"}}})
        raise AssertionError(record)

    recorder = Recorder(api)
    tool = FeishuTaskTool(client=_client(recorder), user_map={})
    result = tool.create_task("无有效负责人", owner_id="u-unknown", due_date=None)
    assert "members" not in next(r["json"] for r in recorder.requests if r["path"].endswith("/tasks"))
    assert any("skipped invalid/non-open_id owner_id" in w for w in result["warnings"])

    completed = tool.complete_task("guid-2")
    assert completed["id"] == "guid-2"
    patch = next(r for r in recorder.requests if r["method"] == "PATCH")
    assert patch["json"]["update_fields"] == ["completed_at"]
    assert len(patch["json"]["task"]["completed_at"]) == 13


def test_calendar_freebusy_get_update_delete_and_missing_calendar_id():
    def api(record):
        path = record["path"]
        if path == "/open-apis/calendar/v4/freebusy/list":
            return _json_response({"code": 0, "data": {"freebusy_list": [{"user_id": "ou_lisi"}]}})
        if path.endswith("/events/evt-9") and record["method"] == "GET":
            return _json_response({"code": 0, "data": {"event": {"event_id": "evt-9"}}})
        if path.endswith("/events/evt-9") and record["method"] == "PATCH":
            return _json_response({"code": 0, "data": {"event": {"event_id": "evt-9", "summary": "新标题"}}})
        if path.endswith("/events/evt-9") and record["method"] == "DELETE":
            return _json_response({"code": 0, "data": {}})
        raise AssertionError(record)

    recorder = Recorder(api)
    tool = FeishuCalendarTool(client=_client(recorder), calendar_id="cal-1", user_map={"u-lisi": "ou_lisi"})
    freebusy = tool.query_freebusy(["u-lisi", "bad-id"], "2026-10-27T09:00:00", "2026-10-27T18:00:00")
    body = next(r["json"] for r in recorder.requests if r["path"].endswith("/freebusy/list"))
    assert body["user_id"] == {"user_ids": ["ou_lisi"], "id_type": "open_id"}
    assert len(body["time_min"]) == 10 and len(body["time_max"]) == 10
    assert freebusy["warnings"]
    assert tool.get_event("evt-9")["id"] == "evt-9"
    assert tool.update_event("evt-9", title="新标题")["event"]["summary"] == "新标题"
    assert tool.delete_event("evt-9")["deleted"] is True

    missing_id = FeishuCalendarTool(client=_client(Recorder(api)), calendar_id="")
    with pytest.raises(FeishuAPIError, match="FEISHU_CALENDAR_ID is not configured"):
        missing_id.create_event("x", "2026-10-27T10:00:00", [])


def test_user_refresh_and_expired_user_token_retry():
    calls = {"protected": 0, "refresh": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content.decode("utf-8")) if request.content else {}
        if path.endswith("/oidc/refresh_access_token"):
            calls["refresh"] += 1
            expected_basic = "Basic " + base64.b64encode(b"cli_test:test-secret").decode()
            assert request.headers["authorization"] == expected_basic
            assert body == {"grant_type": "refresh_token", "refresh_token": "refresh-old"}
            return _json_response({
                "code": 0,
                "data": {"access_token": "u-new", "refresh_token": "refresh-new", "expires_in": 7200},
            })
        if path == "/open-apis/calendar/v4/calendars":
            calls["protected"] += 1
            if request.headers["authorization"] == "Bearer u-old":
                return _json_response({"code": 99991663, "msg": "user token expired", "data": {}})
            assert request.headers["authorization"] == "Bearer u-new"
            return _json_response({"code": 0, "data": {"ok": True}})
        raise AssertionError(path)

    client = FeishuClient(
        "cli_test",
        "test-secret",
        user_access_token="u-old",
        refresh_token="refresh-old",
        transport=httpx.MockTransport(handler),
    )
    result = client.request("GET", "/open-apis/calendar/v4/calendars", token_type="user")
    assert result == {"ok": True}
    assert calls == {"protected": 2, "refresh": 1}
    assert client.user_access_token == "u-new"
    assert client.refresh_token == "refresh-new"


def test_webhook_card_signature_and_provider_error():
    captured = {}

    def ok_handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content.decode("utf-8")))
        return _json_response({"code": 0, "msg": "success", "data": {}})

    client = FeishuClient(transport=httpx.MockTransport(ok_handler))
    tool = FeishuNotifyTool(
        endpoint="https://open.feishu.cn/open-apis/bot/v2/hook/test-hook",
        secret="sign-secret",
        client=client,
        time_fn=lambda: 1234567890,
    )
    result = tool.send(to="u-lisi", title="提醒", content="正文")
    expected_sign = base64.b64encode(
        hmac.new(b"1234567890\nsign-secret", msg=b"", digestmod=hashlib.sha256).digest()
    ).decode()
    assert captured["timestamp"] == "1234567890"
    assert captured["sign"] == expected_sign
    assert captured["msg_type"] == "interactive"
    assert captured["card"]["header"]["title"]["content"] == "提醒"
    assert captured["card"]["elements"][0]["text"]["content"] == "正文"
    assert result["code"] == 0 and result["msg"] == "success" and result["delivered"] is True

    def bad_handler(request: httpx.Request) -> httpx.Response:
        return _json_response({"code": 19001, "msg": "webhook invalid", "data": {}})

    bad_tool = FeishuNotifyTool(
        endpoint="https://open.feishu.cn/open-apis/bot/v2/hook/bad",
        client=FeishuClient(transport=httpx.MockTransport(bad_handler)),
    )
    with pytest.raises(FeishuAPIError) as excinfo:
        bad_tool.send(to="u-lisi", title="x", content="y")
    assert excinfo.value.code == 19001


def test_feishu_api_error_through_dispatch_is_per_item_failure():
    def api(record):
        if record["path"] == "/open-apis/task/v2/tasks":
            return _json_response({"code": 1470403, "msg": "permission denied", "data": {}})
        raise AssertionError(record)

    recorder = Recorder(api)
    tool = FeishuTaskTool(client=_client(recorder))
    item = ActionItem(
        id="t-feishu-fail",
        title="会被飞书拒绝的任务",
        owner_id="ou_lisi",
        due_date="2026-10-28",
        source_segment_ids=[1],
        status="confirmed",
        idempotency_key="meeting:1",
    )
    out = TaskTracker(task_tool=tool).dispatch([item])
    assert out[0].status == "dispatch_failed"
    assert "FeishuAPIError" in (out[0].dispatch_error or "")
    assert "1470403" in (out[0].dispatch_error or "")


def test_factory_builds_real_feishu_tools_and_unconfigured_stays_mock():
    settings = Settings(
        calendar_provider="feishu",
        task_provider="feishu",
        notify_provider="feishu",
        feishu_app_id="cli_test",
        feishu_app_secret="test-secret",
        feishu_calendar_id="cal-1",
        feishu_calendar_auth="user",
        feishu_user_access_token="u-test",
        feishu_webhook_url="https://open.feishu.cn/open-apis/bot/v2/hook/test-hook",
        feishu_webhook_secret="sign-secret",
        feishu_user_map='{"u-lisi":"ou_lisi"}',
    )
    calendar = build_calendar_tool(settings)
    task = build_task_tool(settings)
    notify = build_notify_tool(settings)
    assert isinstance(calendar, FeishuCalendarTool) and calendar.configured
    assert calendar.calendar_id == "cal-1" and calendar.auth_mode == "user"
    assert calendar.user_map == {"u-lisi": "ou_lisi"}
    assert isinstance(task, FeishuTaskTool) and task.configured
    assert isinstance(notify, FeishuNotifyTool) and notify.configured

    empty = Settings(
        calendar_provider="feishu", task_provider="feishu", notify_provider="feishu",
        feishu_app_id="", feishu_app_secret="", feishu_webhook_url="",
    )
    assert build_task_tool(empty).create_task("x")["mock"] is True
    assert build_calendar_tool(empty).create_event("x", "2026-10-27T10:00:00", [])["mock"] is True
    assert build_notify_tool(empty).send(to="u", title="x", content="y")["delivered"] is False
