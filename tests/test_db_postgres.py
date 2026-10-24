"""Store contract tests: the SAME assertions run against both store
implementations, proving InMemoryDB and PostgresDB honour one interface
(save / get / list_all / delete, overwrite-on-save, read isolation).

- The InMemoryDB parameter always runs.
- The PostgresDB parameter needs TEST_DATABASE_URL pointing at a real
  Postgres (e.g. postgresql://postgres@127.0.0.1:5432/meeting_agent_test).
  When it is unset the Postgres cases skip — the default pytest run is
  neither slowed nor reddened, and nothing is faked: a skip here means
  "not verified against a server", by design.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

from src.shared.config import Settings
from src.shared.db import InMemoryDB, PostgresDB, make_store
from src.shared.schema import ActionItem, BriefResult, Meeting, Rejection

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "")


def _rich_meeting(mid: str = "m-contract-1") -> Meeting:
    """A meeting exercising the fields that must survive a round trip:
    action items with external ids, the rejection audit trail, a brief,
    and timezone-aware datetimes."""
    return Meeting(
        id=mid,
        title="支付联调与上线时间评审会",
        status="dispatched",
        start_at=datetime(2026, 10, 23, 10, 0, tzinfo=timezone.utc),
        action_items=[
            ActionItem(
                id="task-1", title="提交灰度方案", owner_id="u-zhangsan",
                due_date="2026-10-28", priority="P1",
                source_segment_ids=[3, 5],
                external_system="feishu", external_task_id="guid-abc-123",
                status="dispatched",
                reminder_sent_at=datetime(2026, 10, 27, 9, 0, tzinfo=timezone.utc),
            ),
        ],
        rejections=[
            Rejection(gate="minutes", reason="摘要不准",
                      at=datetime(2026, 10, 23, 11, 0, tzinfo=timezone.utc)),
        ],
        brief=BriefResult(
            content="# 会前 Brief\n...", source_meeting_ids=["m-old-1"],
            generated_at=datetime(2026, 10, 22, 9, 0, tzinfo=timezone.utc)),
        minutes_confirmed=True,
        tasks_confirmed=True,
        minutes_gate_entered_at=datetime(2026, 10, 23, 11, 30, tzinfo=timezone.utc),
        tasks_gate_entered_at=datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc),
        minutes_gate_timeout_notified_at=datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc),
    )


@pytest.fixture(params=["memory", "postgres"])
def store(request):
    if request.param == "postgres":
        if not TEST_DATABASE_URL:
            pytest.skip("TEST_DATABASE_URL not set — Postgres contract not run")
        db = PostgresDB(TEST_DATABASE_URL)
        for m in db.list_all():  # start from a clean table
            db.delete(m.id)
        yield db
        for m in db.list_all():
            db.delete(m.id)
    else:
        yield InMemoryDB()


def test_save_get_round_trip_rich_object(store):
    m = _rich_meeting()
    assert store.save(m) is m
    back = store.get(m.id)
    assert back is not None
    assert back.model_dump() == m.model_dump()
    # spot-check the fields the round trip must not lose
    assert back.action_items[0].external_task_id == "guid-abc-123"
    assert back.rejections[0].reason == "摘要不准"
    assert back.brief.source_meeting_ids == ["m-old-1"]


def test_get_missing_returns_none(store):
    assert store.get("no-such-meeting") is None


def test_save_overwrites_same_id(store):
    store.save(_rich_meeting())
    changed = _rich_meeting()
    changed.status = "tasks_rejected"
    changed.title = "改过的标题"
    store.save(changed)
    back = store.get(changed.id)
    assert back.status == "tasks_rejected"
    assert back.title == "改过的标题"
    assert len(store.list_all()) == 1  # overwrite, not duplicate


def test_list_all_and_delete(store):
    store.save(_rich_meeting("m-a"))
    store.save(_rich_meeting("m-b"))
    assert {m.id for m in store.list_all()} == {"m-a", "m-b"}
    assert store.delete("m-a") is True
    assert store.get("m-a") is None
    assert store.delete("m-a") is False  # already gone
    assert {m.id for m in store.list_all()} == {"m-b"}


def test_reads_are_isolated_from_caller_mutation(store):
    store.save(_rich_meeting())
    leaked = store.get("m-contract-1")
    leaked.title = "被外部改掉"
    leaked.action_items.clear()
    back = store.get("m-contract-1")
    assert back.title == "支付联调与上线时间评审会"
    assert len(back.action_items) == 1


# ------------------------------------------------------------- make_store
def test_make_store_empty_url_is_memory():
    assert isinstance(make_store(Settings(database_url="")), InMemoryDB)


def test_make_store_unknown_scheme_is_a_loud_error():
    with pytest.raises(ValueError, match="unsupported scheme"):
        make_store(Settings(database_url="sqlite:///tmp/x.db"))


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set")
def test_make_store_postgres_url_is_postgres():
    db = make_store(Settings(database_url=TEST_DATABASE_URL))
    assert isinstance(db, PostgresDB)


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set")
def test_postgres_survives_across_store_instances():
    """The whole point of the exercise: data written by one store
    instance is readable, field for field, by a brand-new instance on a
    brand-new connection (i.e. it would survive a process restart)."""
    a = PostgresDB(TEST_DATABASE_URL)
    for m in a.list_all():
        a.delete(m.id)
    m = _rich_meeting("m-persist-1")
    a.save(m)
    b = PostgresDB(TEST_DATABASE_URL)  # new instance, new connections
    back = b.get("m-persist-1")
    assert back is not None
    assert back.model_dump() == m.model_dump()
    b.delete("m-persist-1")
