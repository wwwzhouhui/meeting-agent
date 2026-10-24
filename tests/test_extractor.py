from src.agents.minutes.analyst import NoteAnalyst
from src.agents.task_agent.agent import TaskAgent
from src.agents.task_agent.extractor import ActionItemExtractor


def test_extractor_finds_certificate_task(sample_meeting):
    items = ActionItemExtractor().extract(sample_meeting)
    cert = [i for i in items if "证书续期" in i.title]
    assert cert, f"no certificate task in {[i.title for i in items]}"
    assert cert[0].owner_raw == "李四"
    assert cert[0].due_date == "2026-10-28"  # 下周三之前, base Fri 2026-10-23
    assert cert[0].due_raw == "下周三之前"
    assert cert[0].priority == "P1"


def test_extractor_finds_wangwu_tasks(sample_meeting):
    items = ActionItemExtractor().extract(sample_meeting)
    wang = [i for i in items if i.owner_raw == "王五"]
    assert len(wang) >= 1
    assert any("灰度" in i.title and i.due_date == "2026-10-24" for i in wang)  # 明天


def test_extractor_dedup_and_evidence(sample_meeting):
    items = ActionItemExtractor().extract(sample_meeting)
    # segments 5 and 6 describe the same 李四/证书续期 task -> merged, not duplicated
    cert = [i for i in items if "证书续期" in i.title and i.owner_raw == "李四"]
    assert len(cert) == 1
    valid = sample_meeting.valid_segment_ids()
    for i in items:
        assert i.source_segment_ids, "task without evidence must not exist"
        assert all(s in valid for s in i.source_segment_ids)


def test_task_agent_assigns_ids(sample_meeting):
    meeting = TaskAgent().run(sample_meeting)
    by_owner = {i.owner_raw: i for i in meeting.action_items}
    assert by_owner["李四"].owner_id == "u-lisi"
    assert by_owner["李四"].owner_confidence >= 0.7
    assert by_owner["王五"].owner_id == "u-wangwu"


def test_analyst_decision_has_evidence(sample_meeting):
    result = NoteAnalyst().analyze(sample_meeting.transcript, sample_meeting.title)
    assert any("11月8日" in d.text for d in result.decisions)
    valid = sample_meeting.valid_segment_ids()
    for d in result.decisions:
        assert d.segment_ids and all(s in valid for s in d.segment_ids)
    assert any("证书" in r.text and "过期" in r.text for r in result.risks)
    assert result.open_questions, "灰度比例未定 should be an open question"
