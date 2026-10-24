"""LLM quality fixes (2026-10-23 field reports) — all offline, fake LLM.

Fix 1 — minutes decisions / todo boundary: assignment sentences
("李四负责…") must not sit in decisions next to the task list. The
prompt now defines the boundary; the analyst flags *suspect* decisions
(participant name + assignment marker) into the single repair retry —
never silently stripped — and keeps + notes whatever survives repair.

Fix 2 — task extraction must not emit meeting-arrangement items
("召开进度跟进会…"): those belong to the Calendar Agent. The prompt
excludes them and a deterministic safety net (filter_scheduling_items)
backstops EVERY extraction path, recording each drop.
"""
import json

import httpx

from src.agents.minutes.analyst import NoteAnalyst, find_suspect_decisions
from src.agents.minutes.prompts import SYSTEM_PROMPT as MINUTES_SYSTEM_PROMPT
from src.agents.task_agent.agent import TaskAgent
from src.agents.task_agent.extractor import (
    ActionItemExtractor,
    filter_scheduling_items,
    is_scheduling_title,
)
from src.agents.task_agent.prompts import SYSTEM_PROMPT as TASK_SYSTEM_PROMPT
from src.agents.task_agent.prompts import build_extractor_messages
from src.shared.config import Settings
from src.shared.llm import LLMClient
from src.shared.schema import ActionItem, Decision, Meeting

PARTICIPANTS = ["张三", "李四", "王五"]


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


def _minutes_payload(decisions) -> str:
    return json.dumps({
        "summary": "上线推迟至11月8日。",
        "executive_summary": "上线推迟至11月8日。",
        "detailed_minutes": "## 上线时间\n推迟至11月8日。",
        "topics": [{"title": "上线时间", "segment_ids": [3, 4], "summary": "推迟至11月8日。"}],
        "decisions": decisions,
        "risks": [{"text": "支付渠道证书11月1日过期", "segment_ids": [2], "severity": "high"}],
        "open_questions": [],
    }, ensure_ascii=False)


TRUE_DECISION = {"text": "上线推迟至11月8日", "decided_by": "张三", "segment_ids": [3, 4]}
SUSPECT_1 = {"text": "李四负责支付渠道证书续期", "decided_by": None, "segment_ids": [5]}
SUSPECT_2 = {"text": "王五在下周三之前完成联调报告提交", "decided_by": None, "segment_ids": [8]}


# ---------------------------------------------------------------- prompts
def test_minutes_prompt_defines_decision_todo_boundary():
    assert "决策与待办的边界" in MINUTES_SYSTEM_PROMPT
    assert "删掉人名" in MINUTES_SYSTEM_PROMPT  # the one-line test
    assert "不是决策" in MINUTES_SYSTEM_PROMPT
    assert "李四负责证书续期" in MINUTES_SYSTEM_PROMPT  # counter-example present


def test_task_prompt_excludes_meeting_arrangements():
    assert "会议安排类事项" in TASK_SYSTEM_PROMPT
    assert "日程" in TASK_SYSTEM_PROMPT
    assert "实质交付物" in TASK_SYSTEM_PROMPT  # deliverables still high-recall


def test_extractor_prompt_notes_decisions_are_reference_only(sample_meeting):
    messages = build_extractor_messages(
        sample_meeting.title, sample_meeting.base_date(), PARTICIPANTS,
        sample_meeting.transcript, summary="摘要", decisions=[])
    assert "仅供参考" in messages[-1]["content"]
    assert "不要因此漏抽" in messages[-1]["content"]


# ------------------------------------------------------- suspect detector
def test_suspect_detector_flags_assignments_only():
    def suspects(text):
        return find_suspect_decisions([Decision(text=text, segment_ids=[1])], PARTICIPANTS)

    assert suspects("李四负责支付渠道证书续期")
    assert suspects("王五负责跟进支付联调测试")
    assert suspects("王五在下周三之前完成联调报告提交")
    # genuine decisions that merely mention a person or a date: clean
    assert not suspects("由李四牵头成立专项组")       # 牵头 w/o 负责 is organisational
    assert not suspects("上线推迟至11月8日")           # date, but names nobody
    assert not suspects("证书到期前必须完成续期")     # collective rule, names nobody
    assert not suspects("灰度范围定为5%用户")
    assert not suspects("张三宣布会议改到线上举行")   # name, but no assignment marker


# ------------------------------------------------------------ minutes chain
def test_suspect_decisions_trigger_repair_then_clean(sample_meeting):
    calls = {"n": 0}
    repair_bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return _ok(_minutes_payload([TRUE_DECISION, SUSPECT_1, SUSPECT_2]))
        repair_bodies.append(request.content.decode("utf-8"))
        return _ok(_minutes_payload([TRUE_DECISION]))

    result = NoteAnalyst(llm=_client(handler)).analyze(
        sample_meeting.transcript, sample_meeting.title, participants=PARTICIPANTS)
    assert calls["n"] == 2  # first attempt + exactly one repair
    assert result.generation_source == "llm_repaired"
    # the repair prompt carries the suspect diagnosis (which 条 + why)
    assert "疑似待办" in repair_bodies[0]
    assert "第2条" in repair_bodies[0] and "第3条" in repair_bodies[0]
    assert "李四负责支付渠道证书续期" in repair_bodies[0]
    # clean result adopted; the true decision survives
    assert [d.text for d in result.decisions] == ["上线推迟至11月8日"]
    assert result.generation_notes == []


def test_suspect_surviving_repair_is_kept_verbatim_and_noted(sample_meeting):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return _ok(_minutes_payload([TRUE_DECISION, SUSPECT_1]))  # model insists

    result = NoteAnalyst(llm=_client(handler)).analyze(
        sample_meeting.transcript, sample_meeting.title, participants=PARTICIPANTS)
    assert calls["n"] == 2
    assert result.generation_source == "llm_repaired"
    # NEVER silently stripped: the suspect text is still there, verbatim…
    assert "李四负责支付渠道证书续期" in [d.text for d in result.decisions]
    # …and the retention is on the record
    assert result.generation_notes
    assert "李四负责支付渠道证书续期" in result.generation_notes[0]
    assert "未自动删除" in result.generation_notes[0]


def test_true_decisions_do_not_trigger_repair(sample_meeting):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return _ok(_minutes_payload([
            {"text": "由李四牵头成立专项组", "decided_by": "张三", "segment_ids": [3]},
            TRUE_DECISION,
            {"text": "证书到期前必须完成续期", "decided_by": None, "segment_ids": [2]},
        ]))

    result = NoteAnalyst(llm=_client(handler)).analyze(
        sample_meeting.transcript, sample_meeting.title, participants=PARTICIPANTS)
    assert calls["n"] == 1  # no false positive -> no wasted repair
    assert result.generation_source == "llm"
    assert len(result.decisions) == 3
    assert result.generation_notes == []


# -------------------------------------------------------------- safety net
def _item(title: str) -> ActionItem:
    return ActionItem(id=f"t-{abs(hash(title)) % 10**6}", title=title, source_segment_ids=[1])


def test_scheduling_filter_drops_convening_items():
    for title in ("召开进度跟进会确认证书和联调结果", "安排下周评审会",
                  "组织下周的评审会", "主持月度例会", "召开项目启动会"):
        assert is_scheduling_title(title), title
        kept, dropped = filter_scheduling_items([_item(title)])
        assert kept == [] and [d.title for d in dropped] == [title]


def test_scheduling_filter_keeps_deliverables_about_meetings():
    keep_cases = ["准备评审会演示材料", "提交会议纪要", "预订会议室",
                  "安排联调环境", "组织联调测试"]
    for title in keep_cases:
        assert not is_scheduling_title(title), title
        kept, dropped = filter_scheduling_items([_item(title)])
        assert [k.title for k in kept] == [title] and dropped == []


GOOD_TASKS = {
    "action_items": [
        {"title": "完成支付渠道证书续期并提交联调", "owner_raw": "李四",
         "due_raw": "下周三之前", "priority": "P1", "source_segment_ids": [5, 6]},
        {"title": "提交灰度方案供评审", "owner_raw": "王五",
         "due_raw": "明天", "priority": "P2", "source_segment_ids": [7]},
        {"title": "完成支付联调测试报告", "owner_raw": "王五",
         "due_raw": "11月1日前", "priority": "P2", "source_segment_ids": [8]},
        {"title": "召开进度跟进会确认证书和联调结果", "owner_raw": "张三",
         "due_raw": "下周二下午", "priority": "P2", "source_segment_ids": [9, 10]},
    ],
}


def test_llm_scheduling_item_filtered_and_traced(sample_meeting):
    extractor = ActionItemExtractor(
        llm=_client(lambda req: _ok(json.dumps(GOOD_TASKS, ensure_ascii=False))))
    meeting = TaskAgent(extractor=extractor).run(sample_meeting)
    assert meeting.tasks_generation_source == "llm"
    assert len(meeting.action_items) == 3
    assert all("跟进会" not in i.title for i in meeting.action_items)
    # the drop is on the record — extractor notes AND meeting warnings
    assert len(extractor.extraction_notes) == 1
    assert "召开进度跟进会确认证书和联调结果" in extractor.extraction_notes[0]
    assert any("召开进度跟进会" in w for w in meeting.warnings)


def test_rule_path_scheduling_item_filtered_and_traced(sample_meeting):
    # The rule extractor DOES produce a convening item from this segment
    # ("张三负责组织下周评审会…") — the net must catch it there too,
    # while the same-path deliverable "准备评审会演示材料" survives.
    data = sample_meeting.model_dump()
    data["transcript"] = data["transcript"] + [
        {"id": 11, "speaker": "张三", "start_sec": 310, "end_sec": 335,
         "text": "张三负责组织下周评审会，确认联调结果。"},
        {"id": 12, "speaker": "李四", "start_sec": 335, "end_sec": 360,
         "text": "李四负责准备评审会演示材料，下周一之前提交。"},
    ]
    meeting = Meeting.model_validate(data)
    extractor = ActionItemExtractor()  # no LLM configured -> rules path
    items = extractor.extract(meeting)
    titles = [i.title for i in items]
    assert extractor.generation_source == "rules"
    assert all("评审会，" not in t and not t.startswith("组织下周评审会") for t in titles)
    assert any("准备评审会演示材料" in t for t in titles)
    assert len(extractor.extraction_notes) == 1
    assert "组织下周评审会" in extractor.extraction_notes[0]
