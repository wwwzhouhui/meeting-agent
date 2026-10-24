"""LLM task-extraction path tests — all offline, fake transport injected.

Mirrors tests/test_llm_minutes.py: httpx.MockTransport handlers stand in
for an OpenAI-compatible server. Red lines under test:

- the LLM only extracts; any owner_id it returns is discarded and the
  Assigner alone resolves owners;
- due_date is computed by date_parser from due_raw — an absurd LLM
  due_date (2099-01-01) loses to the parser;
- fabricated segment ids are dropped, fabricated owners go to humans.
"""
import json

import httpx

from src.agents.task_agent.agent import TaskAgent
from src.agents.task_agent.extractor import ActionItemExtractor
from src.shared.config import Settings
from src.shared.llm import LLMClient

GOOD = {
    "action_items": [
        {"title": "完成支付渠道证书续期并提交联调", "owner_raw": "李四",
         "due_raw": "下周三之前", "priority": "P1", "source_segment_ids": [5, 6]},
        {"title": "提交灰度方案供评审", "owner_raw": "王五",
         "due_raw": "明天", "priority": "P2", "source_segment_ids": [7]},
        {"title": "完成支付联调测试报告", "owner_raw": "王五",
         "due_raw": "11月1日前", "priority": "P2", "source_segment_ids": [8]},
    ],
}


def _payload(mutate=None) -> str:
    data = json.loads(json.dumps(GOOD))  # deep copy
    if mutate:
        mutate(data)
    return json.dumps(data, ensure_ascii=False)


def _client(handler) -> LLMClient:
    settings = Settings(
        llm_mode="llm",
        llm_api_key="test-key",
        llm_base_url="http://fake-llm/v1",
        llm_model="fake-model",
    )
    return LLMClient(settings=settings, transport=httpx.MockTransport(handler))


def _ok(content: str) -> httpx.Response:
    return httpx.Response(200, json={
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                     "finish_reason": "stop"}],
    })


def _run(meeting, handler):
    agent = TaskAgent(extractor=ActionItemExtractor(llm=_client(handler)))
    return agent.run(meeting)


def _by_title(meeting, needle: str):
    return [i for i in meeting.action_items if needle in i.title]


def test_valid_json_adopted_source_llm_assigner_resolves(sample_meeting):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["model"] == "fake-model"
        prompt = json.dumps(body["messages"], ensure_ascii=False)
        assert "action_items" in prompt  # extractor prompt names the field
        return _ok(_payload())

    meeting = _run(sample_meeting, handler)
    assert meeting.tasks_generation_source == "llm"
    assert meeting.tasks_generation_model == "fake-model"
    assert len(meeting.action_items) == 3
    cert = _by_title(meeting, "证书续期")
    assert len(cert) == 1
    cert = cert[0]
    # owner resolution still came from the Assigner, not the LLM
    assert cert.owner_id == "u-lisi"
    assert cert.owner_confidence == 0.95
    assert cert.due_raw == "下周三之前"
    assert cert.due_date == "2026-10-28"  # 下周三, base Fri 2026-10-23
    assert cert.source_segment_ids == [5, 6]
    assert cert.generation_source == "llm"
    assert cert.id == f"task-{sample_meeting.id}-5"
    assert cert.idempotency_key == f"{sample_meeting.id}:5"


def test_llm_owner_id_is_discarded(sample_meeting):
    def mutate(data):
        # LLM tries to smuggle in owner ids (wrong ones) + a bogus priority
        data["action_items"][0]["owner_id"] = "u-zhangsan"
        data["action_items"][1]["owner_id"] = "u-lisi"
        data["action_items"][0]["priority"] = "URGENT"

    meeting = _run(sample_meeting, lambda req: _ok(_payload(mutate)))
    cert = _by_title(meeting, "证书续期")[0]
    assert cert.owner_id == "u-lisi"  # Assigner result, NOT the smuggled u-zhangsan
    assert cert.priority == "P1"  # invalid priority fell back to rule priority
    gray = _by_title(meeting, "灰度方案")[0]
    assert gray.owner_id == "u-wangwu"  # NOT the smuggled u-lisi


def test_fabricated_segment_id_item_is_dropped(sample_meeting):
    def mutate(data):
        data["action_items"].append(
            {"title": "编造的任务：给大家发奖金", "owner_raw": "张三",
             "due_raw": "明天", "priority": "P2", "source_segment_ids": [999]})
        # string ids on a real item are coerced, not dropped
        data["action_items"][2]["source_segment_ids"] = ["8"]

    meeting = _run(sample_meeting, lambda req: _ok(_payload(mutate)))
    assert all("编造" not in i.title for i in meeting.action_items)
    valid = sample_meeting.valid_segment_ids()
    for i in meeting.action_items:
        assert i.source_segment_ids and all(s in valid for s in i.source_segment_ids)
    report = _by_title(meeting, "联调测试报告")[0]
    assert report.source_segment_ids == [8]


def test_fabricated_owner_goes_to_human(sample_meeting):
    def mutate(data):
        data["action_items"].append(
            {"title": "完成季度安全审计", "owner_raw": "赵六",
             "due_raw": "明天", "priority": "P2", "source_segment_ids": [2]})

    meeting = _run(sample_meeting, lambda req: _ok(_payload(mutate)))
    audit = _by_title(meeting, "安全审计")
    assert len(audit) == 1
    assert audit[0].owner_id is None
    assert audit[0].owner_confidence == 0.3  # <0.7 -> forced human assignment


def test_due_date_parser_wins_over_llm(sample_meeting):
    def mutate(data):
        # LLM converts 明天 to an absurd date; the parser must win.
        data["action_items"][1]["due_date"] = "2099-01-01"

    meeting = _run(sample_meeting, lambda req: _ok(_payload(mutate)))
    gray = _by_title(meeting, "灰度方案")[0]
    assert gray.due_raw == "明天"
    assert gray.due_date == "2026-10-24"  # base 2026-10-23 + 1 day, NOT 2099


def test_repair_after_bad_json_source_llm_repaired(sample_meeting):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return _ok("任务就是那些啦，你自己看逐字稿吧。")
        return _ok(_payload())

    meeting = _run(sample_meeting, handler)
    assert calls["n"] == 2  # first attempt + exactly one repair
    assert meeting.tasks_generation_source == "llm_repaired"
    assert len(meeting.action_items) == 3
    assert all(i.generation_source == "llm_repaired" for i in meeting.action_items)


def test_llm_total_failure_falls_back_to_rules(sample_meeting):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    meeting = _run(sample_meeting, handler)
    assert meeting.tasks_generation_source == "rule_fallback"
    assert meeting.action_items, "rule fallback must still produce tasks"
    assert all(i.generation_source == "rule_fallback" for i in meeting.action_items)
    cert = _by_title(meeting, "证书续期")
    assert cert and cert[0].owner_id == "u-lisi"
    assert cert[0].due_date == "2026-10-28"


def test_duplicate_llm_items_merged_with_evidence(sample_meeting):
    def mutate(data):
        # same task returned twice, evidence split across the two copies
        first = dict(data["action_items"][0])
        first["source_segment_ids"] = [5]
        second = dict(data["action_items"][0])
        second["source_segment_ids"] = [6]
        data["action_items"][0] = first
        data["action_items"].insert(1, second)

    meeting = _run(sample_meeting, lambda req: _ok(_payload(mutate)))
    cert = _by_title(meeting, "证书续期")
    assert len(cert) == 1, "duplicate LLM items must be merged, not listed twice"
    assert cert[0].source_segment_ids == [5, 6]  # evidence merged
    assert len(meeting.action_items) == 3
