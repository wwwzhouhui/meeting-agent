from src.orchestrator.flow import MeetingFlow


def test_flow_full_run_dispatches(sample_meeting):
    flow = MeetingFlow()
    m = flow.run(sample_meeting, auto_approve_minutes=True, auto_approve_tasks=True)
    assert m.status == "dispatched"
    assert m.decisions, "flow must produce decisions"
    assert any("11月8日" in d.text for d in m.decisions)
    assert m.action_items, "flow must produce tasks"
    assert all(a.status == "dispatched" and a.external_task_id for a in m.action_items)
    # follow-up: transcript says 下周二 (2026-10-27) -> a candidate on that date
    assert m.follow_up_candidate_times
    assert any(t.startswith("2026-10-27") for t in m.follow_up_candidate_times)


def test_flow_stops_at_gate1_without_approval(sample_meeting):
    flow = MeetingFlow()
    m = flow.run(sample_meeting)  # no approvals
    assert m.status == "awaiting_minutes_confirmation"
    assert m.decisions  # minutes are produced before the gate
    assert not m.action_items  # tasks must NOT be extracted before gate1


def test_flow_idempotent_dispatch(sample_meeting):
    flow = MeetingFlow()
    m = flow.run(sample_meeting, auto_approve_minutes=True, auto_approve_tasks=True)
    first_ids = [a.external_task_id for a in m.action_items]
    m = flow.step_dispatch(m)  # re-dispatch must not duplicate
    assert [a.external_task_id for a in m.action_items] == first_ids
