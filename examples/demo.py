"""One-click demo: python3 examples/demo.py [--mode mock|llm|auto]

- mock (default when run without flags in a key-less env): rule minutes.
- llm:  force the real LLM path (needs LLM_BASE_URL / LLM_API_KEY in env
  or .env, e.g. the fake server in examples/fake_llm_server.py).
- auto: real LLM when a key / base URL is configured, otherwise rules.
"""
import argparse, json, os, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.orchestrator.flow import MeetingFlow  # noqa: E402
from src.shared.schema import Meeting  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="meeting-agent demo")
    parser.add_argument("--mode", choices=["mock", "llm", "auto"], default=None,
                        help="LLM mode (overrides LLM_MODE env); default: env / auto")
    args = parser.parse_args()
    if args.mode:
        os.environ["LLM_MODE"] = args.mode

    sample = Path(__file__).with_name("sample_meeting.json")
    meeting = Meeting.model_validate(json.loads(sample.read_text(encoding="utf-8")))
    flow = MeetingFlow()
    meeting = flow.run(meeting, auto_approve_minutes=True, auto_approve_tasks=True)
    minutes = meeting.minutes
    source = getattr(minutes, "generation_source", None) or meeting.generation_source or "rules"
    model = getattr(minutes, "generation_model", None) or meeting.generation_model or "-"
    print("=" * 60)
    print(f"Meeting: {meeting.title}  Status: {meeting.status}")
    print(f"Generation: source={source} model={model} mode={os.getenv('LLM_MODE', 'auto')}")
    tasks_source = meeting.tasks_generation_source or (
        meeting.action_items[0].generation_source if meeting.action_items else "rules")
    tasks_model = meeting.tasks_generation_model or "-"
    print(f"Tasks Generation: source={tasks_source} model={tasks_model} mode={os.getenv('LLM_MODE', 'auto')}")
    print("=" * 60)
    print("\n[Minutes - executive summary]")
    print(meeting.executive_summary)
    print("\n[Decisions]")
    for d in meeting.decisions:
        print(f"  - {d.text} (segments={d.segment_ids})")
    print("\n[Risks]")
    for r in meeting.risks:
        print(f"  - [{r.severity}] {r.text} (segments={r.segment_ids})")
    print("\n[Open questions]")
    for q in meeting.open_questions:
        print(f"  - {q}")
    print("\n[Tasks]")
    for a in meeting.action_items:
        print(f"  - [{a.priority}] {a.title} | owner: {a.owner_raw} ({a.owner_id}, conf {a.owner_confidence}) | due: {a.due_raw} -> {a.due_date} | status: {a.status} | ext: {a.external_task_id} | segments: {a.source_segment_ids}")
    print("\n[Follow-up candidates]")
    for t in meeting.follow_up_candidate_times:
        print(f"  - {t}")
    print(f"\nDemo OK: full flow completed (generation_source={source})")


if __name__ == "__main__":
    main()
