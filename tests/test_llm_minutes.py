"""LLM minutes path tests — all offline, fake transport injected.

No real network and no API key: httpx.MockTransport handlers stand in
for an OpenAI-compatible server.
"""
import json

import httpx

from src.agents.minutes.analyst import NoteAnalyst
from src.shared.config import Settings
from src.shared.llm import LLMClient

GOOD = {
    "summary": "上线推迟至11月8日。支付渠道证书11月1日过期，李四下周三之前完成续期。",
    "executive_summary": "上线推迟至11月8日。支付渠道证书11月1日过期，李四下周三之前完成续期。",
    "detailed_minutes": "## 上线时间\n推迟至11月8日。\n\n## 证书续期\n李四负责续期并提交联调。",
    "topics": [
        {"title": "上线时间", "segment_ids": [3, 4], "summary": "推迟至11月8日。"},
    ],
    "decisions": [
        {"text": "上线推迟至11月8日", "decided_by": "张三", "segment_ids": [3, 4]},
    ],
    "risks": [
        {"text": "支付渠道证书11月1日过期", "segment_ids": [2], "severity": "high"},
    ],
    "open_questions": ["灰度比例尚未确定，待产品确认"],
}


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


def test_valid_json_adopted_source_llm(sample_meeting):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["model"] == "fake-model"
        assert request.url.path.endswith("/chat/completions")
        return _ok(json.dumps(GOOD, ensure_ascii=False))

    result = NoteAnalyst(llm=_client(handler)).analyze(
        sample_meeting.transcript, sample_meeting.title)
    assert result.generation_source == "llm"
    assert result.generation_model == "fake-model"
    assert any("11月8日" in d.text for d in result.decisions)
    assert len(result.risks) == 1 and "证书" in result.risks[0].text


def test_fenced_json_with_prose_is_parsed(sample_meeting):
    content = "好的，这是纪要：\n```json\n" + json.dumps(GOOD, ensure_ascii=False) + "\n```\n以上。"
    result = NoteAnalyst(llm=_client(lambda req: _ok(content))).analyze(
        sample_meeting.transcript, sample_meeting.title)
    assert result.generation_source == "llm"
    assert result.decisions and "11月8日" in result.decisions[0].text


def test_fabricated_segment_id_is_filtered(sample_meeting):
    data = json.loads(json.dumps(GOOD))
    data["decisions"].append(
        {"text": "编造的决策：预算增加一百万", "decided_by": "张三", "segment_ids": [999]})
    data["risks"].append(
        {"text": "编造的风险", "segment_ids": [999], "severity": "high"})
    result = NoteAnalyst(llm=_client(lambda req: _ok(json.dumps(data, ensure_ascii=False)))).analyze(
        sample_meeting.transcript, sample_meeting.title)
    assert result.generation_source == "llm"
    assert all("编造" not in d.text for d in result.decisions)
    assert all("编造" not in r.text for r in result.risks)
    valid = sample_meeting.valid_segment_ids()
    for d in result.decisions:
        assert all(s in valid for s in d.segment_ids)


def test_string_segment_ids_are_coerced(sample_meeting):
    data = json.loads(json.dumps(GOOD))
    data["decisions"][0]["segment_ids"] = ["3", "4"]
    result = NoteAnalyst(llm=_client(lambda req: _ok(json.dumps(data, ensure_ascii=False)))).analyze(
        sample_meeting.transcript, sample_meeting.title)
    assert result.generation_source == "llm"
    assert result.decisions[0].segment_ids == [3, 4]


def test_repair_after_bad_json_source_llm_repaired(sample_meeting):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return _ok("这不是 JSON，我随便说说。")
        return _ok(json.dumps(GOOD, ensure_ascii=False))

    result = NoteAnalyst(llm=_client(handler)).analyze(
        sample_meeting.transcript, sample_meeting.title)
    assert calls["n"] == 2  # first attempt + exactly one repair
    assert result.generation_source == "llm_repaired"
    assert any("11月8日" in d.text for d in result.decisions)


def test_llm_total_failure_falls_back_to_rules(sample_meeting):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    result = NoteAnalyst(llm=_client(handler)).analyze(
        sample_meeting.transcript, sample_meeting.title)
    assert result.generation_source == "rule_fallback"
    assert result.decisions, "rule fallback must still produce minutes"
    assert any("11月8日" in d.text for d in result.decisions)
    assert result.executive_summary
