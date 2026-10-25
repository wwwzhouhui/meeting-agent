"""FastAPI app. Runs with zero keys / zero DB (in-memory store + mock LLM).

    uvicorn src.api.main:app --reload --port 8000

Endpoints:
  POST /meetings                         create
  GET  /meetings                         list all meetings
  GET  /                                 web UI (web/index.html; the rest
                                         of web/ is served as static files)
  POST /meetings/{id}/transcript         set/replace transcript segments
  POST /meetings/{id}/audio              upload a recording (multipart);
                                         stored under ASR_UPLOAD_DIR as
                                         {meeting_id}{ext}, audio_url
                                         pointed at it for ASR on /process
  POST /meetings/{id}/process            minutes (+tasks if gate1 auto) flow;
                                         also the re-run path after a
                                         minutes rejection
  GET  /meetings/{id}                    fetch
  POST /meetings/{id}/confirm-minutes    gate1: confirm, then extract tasks
  POST /meetings/{id}/confirm-tasks      gate2: confirm, propose calendar, dispatch
  POST /meetings/{id}/reject-minutes     gate1: reject (body {reason}); status
                                         becomes minutes_rejected, /process re-runs
  POST /meetings/{id}/reject-tasks      gate2: reject (body {reason}); status
                                         becomes tasks_rejected
  POST /meetings/{id}/reextract-tasks   re-extract tasks after a tasks rejection
  PATCH /meetings/{id}/tasks/{item_id}  reassign owner / due_date / priority
                                         (only while awaiting task confirmation)
  POST /meetings/{id}/brief              generate/overwrite the pre-meeting brief
  GET  /meetings/{id}/brief              read the brief (404 until generated)
  POST /calendar/propose                 body: {meeting_id} or a full meeting JSON
  POST /reminders/run                    run one reminder scan now
  POST /gate-timeouts/run                run one confirmation-gate timeout
                                         scan now; returns the action list
  POST /weekly-summary/run               build + send the weekly task
                                         summary now (?window_days=7)
"""
from __future__ import annotations

import base64
import logging
import os
import secrets
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from src.agents.calendar_agent.agent import CalendarAgent
from src.orchestrator.eventbus import Event
from src.orchestrator.flow import MeetingFlow, stamp_gate_entry
from src.orchestrator.gate_timeouts import scan_gate_timeouts
from src.orchestrator.gates import ConfirmationGate
from src.orchestrator.reminders import scan_and_notify
from src.orchestrator.weekly_summary import build_weekly_summary
from src.shared.config import get_settings
from src.shared.db import store
from src.shared.orchestration import Orchestration, load_orchestration
from src.shared.schema import Meeting, TranscriptSegment
from src.tools.factory import build_notify_tool

from .webhooks import router as webhook_router

logger = logging.getLogger(__name__)

_gate = ConfirmationGate(auto_approve=False)
_orchestration = load_orchestration()
_flow = MeetingFlow(gate=_gate, orchestration=_orchestration)
_bus = _flow.bus
_calendar = CalendarAgent()


def _run_reminder_scan() -> dict:
    settings = get_settings()

    def _stamps(meeting: Meeting) -> dict:
        return {i.id: (i.reminder_sent_at, i.overdue_notified_at)
                for i in meeting.action_items}

    before = {m.id: _stamps(m) for m in store.list_all()}
    counts = scan_and_notify(store, build_notify_tool(settings), datetime.now(),
                             lead_hours=settings.reminder_lead_hours)
    # Bus: one reminders.sent event per meeting that actually got a
    # notification in this scan (found by diffing the idempotency
    # stamps the scanner set), appended to that meeting's event log.
    for m in store.list_all():
        old = before.get(m.id, {})
        sent = [item_id for item_id, stamps in _stamps(m).items()
                if stamps != old.get(item_id) and any(stamps)]
        if sent:
            _bus.emit(m, "reminders.sent", {"items": sent, "count": len(sent)})
            store.save(m)
    logger.info("reminder scan: %s", counts)
    return counts


def _gate_timeout_settings():
    """Effective settings for a gate-timeout scan: per-gate thresholds
    come from the orchestration file (gates.<kind>.timeout_hours) when
    neither GATE_TIMEOUT_HOURS nor the per-gate env vars are set —
    env always wins (see src/shared/orchestration.py precedence)."""
    settings = get_settings()
    if os.getenv("GATE_TIMEOUT_HOURS", "").strip():
        return settings
    updates = {}
    if settings.minutes_gate_timeout_hours is None:
        updates["minutes_gate_timeout_hours"] = \
            _orchestration.gate("minutes").timeout_hours
    if settings.tasks_gate_timeout_hours is None:
        updates["tasks_gate_timeout_hours"] = \
            _orchestration.gate("tasks").timeout_hours
    return replace(settings, **updates) if updates else settings


def _run_gate_timeout_scan() -> list[dict]:
    settings = _gate_timeout_settings()
    records = scan_gate_timeouts(store, build_notify_tool(settings),
                                 datetime.now(timezone.utc), settings)
    for rec in records:
        if rec["action"] == "rejected":
            # Keep the in-process gate in sync with the persisted state,
            # exactly like the /reject-* endpoints do.
            if rec["gate"] == "minutes":
                _gate.reject_minutes(rec["meeting_id"], "confirmation timeout (auto-reject)")
            else:
                _gate.reject_tasks(rec["meeting_id"], "confirmation timeout (auto-reject)")
        if rec["action"] in ("notified", "notify_failed", "rejected"):
            m = store.get(rec["meeting_id"])
            if m is not None:
                _bus.emit(m, "gate.timeout", {
                    "gate": rec["gate"], "action": rec["action"],
                    "waited_hours": rec["waited_hours"],
                    "delivered": rec["delivered"],
                })
                store.save(m)
    logger.info("gate timeout scan: %s", records)
    return records


def _run_weekly_summary(window_days: int = 7) -> dict:
    settings = get_settings()
    result = build_weekly_summary(store, datetime.now(), window_days=window_days,
                                  notify_tool=build_notify_tool(settings))
    # Process-wide event (belongs to no single meeting): published on
    # the bus for subscribers, not written to any meeting's log.
    _bus.publish(Event(type="weekly_summary.generated", payload={
        "period_start": str(result["period_start"]),
        "period_end": str(result["period_end"]),
        "window_days": window_days,
        "delivered": result["delivered"],
    }))
    logger.info("weekly summary %s..%s delivered=%s (%s)",
                result["period_start"], result["period_end"],
                result["delivered"], result["delivery_note"])
    return result


def build_scheduler(settings, orchestration: Orchestration | None = None):
    """Build (but do not start) the BackgroundScheduler holding every
    enabled background job: reminder scans (every
    REMINDER_SCAN_INTERVAL_MIN minutes), gate-timeout scans (every
    GATE_TIMEOUT_SCAN_INTERVAL_MIN minutes, default hourly) and the
    weekly summary (CronTrigger Mondays 09:00 in DEFAULT_TIMEZONE).
    Returns None when nothing is enabled. Split out of the lifespan so
    registration can be asserted in tests without starting threads.

    Which jobs exist comes from the orchestration file
    (scheduler.<job>.enabled / interval_min / cron) UNLESS the matching
    *_ENABLED env var is set, in which case the Settings value (which
    is env-derived) wins — the precedence documented in
    src/shared/orchestration.py. With orchestration=None the Settings
    alone decide (the historical behaviour, kept for direct callers).

    Jobs are registered one by one, each isolated: a job whose
    trigger cannot be built is skipped with an error log naming it,
    the others still register. Returns None when nothing ends up
    registered.
    """
    def _enabled(settings_value: bool, env_name: str, job_name: str) -> bool:
        if os.getenv(env_name, "").strip():
            return settings_value
        if orchestration is not None:
            return orchestration.scheduler[job_name].enabled
        return settings_value

    def _interval(settings_value: int, env_name: str, job_name: str) -> int:
        if not os.getenv(env_name, "").strip() and orchestration is not None:
            file_value = orchestration.scheduler[job_name].interval_min
            if file_value:
                return file_value
        return settings_value

    reminders_on = _enabled(settings.reminders_enabled, "REMINDERS_ENABLED", "reminders")
    timeouts_on = _enabled(settings.gate_timeouts_enabled, "GATE_TIMEOUTS_ENABLED", "gate_timeouts")
    weekly_on = _enabled(settings.weekly_summary_enabled, "WEEKLY_SUMMARY_ENABLED", "weekly_summary")
    if not (reminders_on or timeouts_on or weekly_on):
        return None
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger

    scheduler = BackgroundScheduler()
    registered: list[str] = []

    def _add(job_id: str, build) -> None:
        """Register ONE job, isolated: a bad trigger for this job
        (e.g. an unparsable cron that reached us via Settings or a
        programmatically built orchestration) skips only this job
        with a loud log line — it must not take the other jobs down
        with it. (The orchestration file itself already range-checks
        cron/interval at load; this is the second line of defence.)"""
        try:
            build()
        except Exception as exc:
            logger.error("scheduler: job %r failed to register and was "
                         "skipped: %s: %s", job_id, type(exc).__name__, exc)
        else:
            registered.append(job_id)

    if reminders_on:
        _add("reminder-scan", lambda: scheduler.add_job(
            _run_reminder_scan, "interval",
            minutes=_interval(settings.reminder_scan_interval_min,
                              "REMINDER_SCAN_INTERVAL_MIN", "reminders"),
            id="reminder-scan"))
    if timeouts_on:
        _add("gate-timeout-scan", lambda: scheduler.add_job(
            _run_gate_timeout_scan, "interval",
            minutes=_interval(settings.gate_timeout_scan_interval_min,
                              "GATE_TIMEOUT_SCAN_INTERVAL_MIN", "gate_timeouts"),
            id="gate-timeout-scan"))
    if weekly_on:
        def _add_weekly() -> None:
            cron = orchestration.scheduler["weekly_summary"].cron if orchestration else None
            if cron:
                trigger = CronTrigger.from_crontab(
                    cron, timezone=ZoneInfo(settings.default_timezone))
            else:
                trigger = CronTrigger(day_of_week="mon", hour=9, minute=0,
                                      timezone=ZoneInfo(settings.default_timezone))
            scheduler.add_job(_run_weekly_summary, trigger, id="weekly-summary")

        _add("weekly-summary", _add_weekly)
    if not registered:
        return None
    return scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Optionally start the APScheduler background jobs.

    Only the jobs whose *_ENABLED flag is true are registered (all
    default to false). Any failure (e.g. apscheduler not installed)
    degrades to a warning — the API must still start, and the
    POST /reminders/run, /gate-timeouts/run and /weekly-summary/run
    endpoints remain available for manual runs.
    """
    scheduler = None
    settings = get_settings()
    if settings.require_auth and not (settings.basic_auth_username and settings.basic_auth_password):
        raise RuntimeError(
            "REQUIRE_AUTH=true but BASIC_AUTH_USERNAME/BASIC_AUTH_PASSWORD are not "
            "both set: refusing to start with auth disabled (fail-closed)")
    if settings.basic_auth_username and settings.basic_auth_password:
        logger.info("API auth enabled: HTTP Basic (BASIC_AUTH_USERNAME/BASIC_AUTH_PASSWORD)")
    else:
        logger.warning(
            "API auth is DISABLED (BASIC_AUTH_USERNAME/BASIC_AUTH_PASSWORD unset) — "
            "anyone who can reach this service can create meetings and dispatch "
            "tasks to configured providers; set both env vars to enable HTTP Basic auth")
    app.state.orchestration = _orchestration
    try:
        scheduler = build_scheduler(settings, _orchestration)
        if scheduler is not None:
            scheduler.start()
            logger.info("background scheduler started: jobs=%s",
                        [j.id for j in scheduler.get_jobs()])
    except Exception as exc:
        logger.warning("background scheduler failed to start: %s", exc)
        scheduler = None
    app.state.scheduler = scheduler
    app.state.reminder_scheduler = scheduler  # alias kept from the reminders round
    try:
        yield
    finally:
        if scheduler is not None:
            scheduler.shutdown(wait=False)


app = FastAPI(title="Meeting Agent", version="0.1.0", lifespan=lifespan)
app.include_router(webhook_router)


@app.middleware("http")
async def basic_auth_middleware(request: Request, call_next):
    """Optional whole-app HTTP Basic auth: enabled when BOTH
    BASIC_AUTH_USERNAME and BASIC_AUTH_PASSWORD are set, disabled
    (historical open behaviour) when either is empty. Covers every
    route including the static web UI; only /health stays open for
    load-balancer probes and reveals nothing. Browser-native Basic
    auth covers the page AND same-origin fetch automatically, so the
    zero-build frontend needs no changes. Credentials are compared
    with secrets.compare_digest (timing-safe)."""
    settings = get_settings()
    user, password = settings.basic_auth_username, settings.basic_auth_password
    if user and password and request.url.path != "/health":
        supplied = request.headers.get("authorization", "")
        ok = False
        if supplied.startswith("Basic "):
            try:
                decoded = base64.b64decode(supplied[6:].strip()).decode("utf-8")
                got_user, _, got_password = decoded.partition(":")
                ok = (secrets.compare_digest(got_user, user)
                      and secrets.compare_digest(got_password, password))
            except Exception:
                ok = False
        if not ok:
            return Response(status_code=401, headers={"WWW-Authenticate": "Basic"})
    return await call_next(request)


@app.middleware("http")
async def csrf_and_frame_guard(request: Request, call_next):
    """Two browser-facing guards (both no-ops for server-to-server
    clients like curl, scripts and provider webhooks):

    - CSRF: state-changing methods are rejected when they carry a
      cross-site context — an Origin that does not match the (forwarded)
      host, or Sec-Fetch-Site: cross-site. Browser ambient credentials
      (cached HTTP Basic) ride along on cross-site form posts, which
      would otherwise drive the body-less confirm endpoints from a
      third-party page. Requests WITHOUT these headers pass — CSRF
      needs a browser, and same-origin browser requests match on Origin.
    - Framing: every response carries X-Frame-Options: DENY and CSP
      frame-ancestors 'none' so the UI cannot be framed (clickjacking).
    """
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        origin = request.headers.get("origin")
        if origin is not None:
            host = (request.headers.get("x-forwarded-host")
                    or request.headers.get("host") or "")
            try:
                origin_host = urlparse(origin).netloc
            except Exception:
                origin_host = ""
            if origin_host != host:
                return JSONResponse(status_code=403, content={
                    "detail": "cross-origin state-changing request rejected"})
        if request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse(status_code=403, content={
                "detail": "cross-site state-changing request rejected"})
    response = await call_next(request)
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
    return response


class TranscriptIn(BaseModel):
    transcript: list[TranscriptSegment]


class ProcessIn(BaseModel):
    auto_approve_minutes: bool = False
    auto_approve_tasks: bool = False


class ProposeIn(BaseModel):
    meeting_id: str | None = None
    meeting: Meeting | None = None
    text: str | None = None


class RejectIn(BaseModel):
    reason: str = ""


class TaskPatchIn(BaseModel):
    owner_id: str | None = None
    due_date: str | None = None
    priority: str | None = None


def _get_or_404(meeting_id: str) -> Meeting:
    m = store.get(meeting_id)
    if not m:
        raise HTTPException(404, f"meeting {meeting_id} not found")
    return m


DISPATCHED_STATES = ("dispatched", "dispatched_with_failures", "dispatch_failed")


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/meetings")
def create_meeting(meeting: Meeting):
    if store.get(meeting.id) is not None:
        raise HTTPException(409, f"meeting {meeting.id} already exists")
    _bus.emit(meeting, "meeting.created", {"title": meeting.title, "via": "api"})
    store.save(meeting)
    return meeting


@app.get("/meetings")
def list_meetings():
    return store.list_all()


@app.get("/meetings/{meeting_id}")
def get_meeting(meeting_id: str):
    return _get_or_404(meeting_id)


@app.post("/meetings/{meeting_id}/transcript")
def set_transcript(meeting_id: str, body: TranscriptIn):
    m = _get_or_404(meeting_id)
    if m.status in DISPATCHED_STATES:
        raise HTTPException(
            409, f"meeting {meeting_id} is {m.status}; create a new meeting to reprocess")
    m.transcript = body.transcript
    if m.status == "needs_transcript":
        m.status = "scheduled"  # transcript arrived; process can run again
    _bus.emit(m, "transcript.ready", {"segments": len(m.transcript), "via": "api"})
    store.save(m)
    return m


# Audio extensions accepted by the upload endpoint (faster-whisper /
# PyAV can decode all of these). The stored filename is built ONLY from
# the meeting id + one of these suffixes — the client-supplied filename
# is never used as a path (path-traversal safe).
ALLOWED_AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".webm", ".mp4"}

# Cap for one audio upload. The request body is streamed to disk in
# chunks and refused the moment it exceeds this, so an oversized upload
# can neither sit fully in memory nor fill the disk.
MAX_AUDIO_UPLOAD_BYTES = 100 * 1024 * 1024  # 100 MB


@app.post("/meetings/{meeting_id}/audio")
async def upload_audio(meeting_id: str, file: UploadFile = File(...)):
    """Store a recording for this meeting and point audio_url at it, so
    the next /process transcribes it with the configured ASR engine."""
    m = _get_or_404(meeting_id)
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_AUDIO_EXTENSIONS:
        raise HTTPException(
            422, f"unsupported audio extension {ext or '(none)'!r}; "
                 f"allowed: {sorted(ALLOWED_AUDIO_EXTENSIONS)}")
    upload_dir = Path(get_settings().asr_upload_dir)
    upload_dir.mkdir(parents=True, exist_ok=True)
    dest = upload_dir / f"{meeting_id}{ext}"
    size = 0
    try:
        with dest.open("wb") as fh:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_AUDIO_UPLOAD_BYTES:
                    raise HTTPException(
                        413, f"audio file exceeds the "
                             f"{MAX_AUDIO_UPLOAD_BYTES // (1024 * 1024)} MB upload limit")
                fh.write(chunk)
    except HTTPException:
        dest.unlink(missing_ok=True)  # never leave a partial file behind
        raise
    if size == 0:
        dest.unlink(missing_ok=True)
        raise HTTPException(422, "empty audio file")
    # Replacing the recording: drop the previous file (possibly stored
    # under a different extension) and invalidate any cloud-ASR task id
    # earned by the OLD audio — a new recording must never resume it.
    for old in upload_dir.glob(f"{meeting_id}.*"):
        if old.is_file() and old != dest:
            old.unlink()
    m.audio_url = str(dest)
    m.asr_task_id = None
    if m.status == "needs_transcript":
        m.status = "scheduled"  # audio arrived; /process can retry with ASR
    store.save(m)
    return {"meeting_id": meeting_id, "filename": dest.name,
            "size_bytes": size, "audio_url": m.audio_url}


@app.post("/meetings/{meeting_id}/process")
def process_meeting(meeting_id: str, body: ProcessIn | None = None):
    body = body or ProcessIn()
    m = _get_or_404(meeting_id)
    if m.status in DISPATCHED_STATES:
        raise HTTPException(
            409, f"meeting {meeting_id} is {m.status}; create a new meeting to reprocess")
    m = _flow.run(m, auto_approve_minutes=body.auto_approve_minutes,
                  auto_approve_tasks=body.auto_approve_tasks)
    store.save(m)
    return m


@app.post("/meetings/{meeting_id}/confirm-minutes")
def confirm_minutes(meeting_id: str):
    m = _get_or_404(meeting_id)
    if m.minutes_confirmed:
        return m  # idempotent: a repeat confirm changes nothing
    if m.minutes is None or m.status not in ("awaiting_minutes_confirmation", "minutes_draft"):
        raise HTTPException(
            409, f"meeting {meeting_id} is in status {m.status!r}; "
                 "call /process first and confirm only after minutes exist")
    # gate1 passed -> the flow publishes minutes.confirmed and the bus
    # handler extracts the tasks (two separate confirmations by design)
    try:
        m = _flow.confirm_minutes(m)
    except Exception:
        # The critical bus handoff failed and the exception propagates
        # (-> 500). The flow left minutes_confirmed unset, so a retry
        # works; save first so the failure warning the handoff wrote
        # onto the meeting is visible on the record, not only in logs.
        store.save(m)
        raise
    store.save(m)
    return m


@app.post("/meetings/{meeting_id}/confirm-tasks")
def confirm_tasks(meeting_id: str):
    m = _get_or_404(meeting_id)
    if m.tasks_confirmed and m.status in DISPATCHED_STATES:
        return m  # idempotent: already dispatched, repeat confirm is a no-op
    if m.status == "minutes_confirmed":
        # Gate1 passed but the tasks step is disabled in the
        # orchestration config: there are no tasks to confirm.
        raise HTTPException(
            409, f"meeting {meeting_id} is in status 'minutes_confirmed'; "
                 "the tasks step is disabled in the orchestration config, "
                 "so no tasks were extracted and there is nothing to confirm")
    if not m.minutes_confirmed or m.status not in ("awaiting_tasks_confirmation", "tasks_draft"):
        raise HTTPException(
            409, f"meeting {meeting_id} is in status {m.status!r}; "
                 "confirm minutes (gate1) before confirming tasks (gate2)")
    # gate2 passed -> the flow publishes tasks.confirmed and the bus
    # handler proposes follow-up times and dispatches
    try:
        m = _flow.confirm_tasks(m)
    except Exception:
        # Critical bus handoff failed (-> 500): tasks_confirmed was
        # left unset by the flow, so a retry works; save so the
        # handoff's warning is visible on the record (see gate1).
        store.save(m)
        raise
    store.save(m)
    return m


@app.post("/calendar/propose")
def propose_calendar(body: ProposeIn):
    if body.meeting is not None:
        meeting = body.meeting
    elif body.meeting_id:
        meeting = _get_or_404(body.meeting_id)
    else:
        raise HTTPException(400, "meeting_id or meeting is required")
    candidates = _calendar.propose(meeting, text=body.text)
    return {"meeting_id": meeting.id, "candidates": candidates}


# ------------------------------------------------------------ brief
@app.post("/meetings/{meeting_id}/brief")
def generate_brief(meeting_id: str):
    """Generate (or regenerate) the pre-meeting brief. Pre-meeting
    material, so it is allowed in ANY meeting status; regenerating
    overwrites the previous brief and refreshes its timestamp."""
    m = _get_or_404(meeting_id)
    m = _flow.prepare_brief(m)
    store.save(m)
    return m


@app.get("/meetings/{meeting_id}/brief")
def get_brief(meeting_id: str):
    m = _get_or_404(meeting_id)
    if m.brief is None:
        raise HTTPException(
            404, f"brief not generated yet for meeting {meeting_id}; "
                 f"POST /meetings/{meeting_id}/brief first")
    return m.brief


# ------------------------------------------------------- rejection path
@app.post("/meetings/{meeting_id}/reject-minutes")
def reject_minutes(meeting_id: str, body: RejectIn | None = None):
    body = body or RejectIn()
    m = _get_or_404(meeting_id)
    if m.status != "awaiting_minutes_confirmation":
        raise HTTPException(
            409, f"meeting {meeting_id} is in status {m.status!r}; "
                 "minutes can only be rejected while awaiting_minutes_confirmation")
    m = _flow.reject_minutes(m, body.reason)
    store.save(m)
    return m


@app.post("/meetings/{meeting_id}/reject-tasks")
def reject_tasks(meeting_id: str, body: RejectIn | None = None):
    body = body or RejectIn()
    m = _get_or_404(meeting_id)
    if m.status != "awaiting_tasks_confirmation":
        raise HTTPException(
            409, f"meeting {meeting_id} is in status {m.status!r}; "
                 "tasks can only be rejected while awaiting_tasks_confirmation")
    m = _flow.reject_tasks(m, body.reason)
    store.save(m)
    return m


@app.post("/meetings/{meeting_id}/reextract-tasks")
def reextract_tasks(meeting_id: str):
    """Re-extract tasks from the confirmed minutes after a tasks
    rejection; lands back at awaiting_tasks_confirmation."""
    m = _get_or_404(meeting_id)
    if m.status != "tasks_rejected":
        raise HTTPException(
            409, f"meeting {meeting_id} is in status {m.status!r}; "
                 "reextract-tasks is only available after reject-tasks")
    m = _flow.step_tasks(m)
    m.status = "awaiting_tasks_confirmation"
    stamp_gate_entry(m, "tasks")  # re-entering the gate restarts its clock
    _bus.emit(m, "gate.tasks.entered", {"gate": "tasks", "via": "reextract"})
    store.save(m)
    return m


@app.patch("/meetings/{meeting_id}/tasks/{item_id}")
def patch_task(meeting_id: str, item_id: str, body: TaskPatchIn):
    """Manual reassignment while tasks await confirmation: owner_id /
    due_date / priority. A patched owner counts as human-assigned
    (assignment_source="manual", confidence 1.0) and dispatch uses the
    patched values."""
    m = _get_or_404(meeting_id)
    if m.status != "awaiting_tasks_confirmation":
        raise HTTPException(
            409, f"meeting {meeting_id} is in status {m.status!r}; "
                 "tasks can only be edited while awaiting_tasks_confirmation")
    item = next((i for i in m.action_items if i.id == item_id), None)
    if item is None:
        raise HTTPException(404, f"task {item_id} not found in meeting {meeting_id}")
    provided = body.model_fields_set
    if "due_date" in provided and body.due_date is not None:
        try:
            date.fromisoformat(body.due_date)
        except ValueError:
            raise HTTPException(422, f"due_date {body.due_date!r} is not an ISO date (YYYY-MM-DD)")
    if "priority" in provided and body.priority is not None \
            and body.priority not in ("P0", "P1", "P2"):
        raise HTTPException(422, f"priority {body.priority!r} must be one of P0/P1/P2")
    if "owner_id" in provided:
        item.owner_id = body.owner_id
        item.assignment_source = "manual"
        item.owner_confidence = 1.0
        name = next((p.name for p in m.participants if p.user_id == body.owner_id), None)
        if name:
            item.owner_raw = name
    if "due_date" in provided:
        item.due_date = body.due_date
    if "priority" in provided and body.priority is not None:
        item.priority = body.priority
    store.save(m)
    return m


# ------------------------------------------------------------ reminders
@app.post("/reminders/run")
def run_reminders():
    """Run one reminder scan right now (does not need the scheduler).
    Returns {"due_soon_sent", "overdue_sent", "mocked", "failed"}."""
    return _run_reminder_scan()


# ------------------------------------------------- gate timeouts / weekly
@app.post("/gate-timeouts/run")
def run_gate_timeouts():
    """Run one confirmation-gate timeout scan right now (does not need
    the scheduler). Returns {"actions": [...]} — one record per meeting
    acted on: {"meeting_id", "gate", "action", "delivered",
    "delivery_note", "waited_hours"}. A timeout never auto-confirms:
    action is an escalation notice (GATE_TIMEOUT_ACTION=notify) or an
    auto-reject into the existing rejected state (=reject)."""
    return {"actions": _run_gate_timeout_scan()}


@app.post("/weekly-summary/run")
def run_weekly_summary(window_days: int = 7):
    """Build the weekly task summary now and try to deliver it via the
    configured notify tool. Returns {"period_start", "period_end",
    "summary", "delivered", "delivery_note"} — the full summary text is
    returned even when delivery is mock/unconfigured (delivered=false
    with the reason), so the report is never lost with the send."""
    return _run_weekly_summary(window_days=window_days)


# ------------------------------------------------------------ web UI
# Zero-build frontend in web/ (plain HTML/CSS/JS, no CDN — works
# offline). Registered LAST on purpose: Starlette matches routes in
# registration order, so every API route above (and the webhook router)
# wins over the static mount; the mount only serves /, /index.html,
# /app.js, /style.css and any other file placed in web/.
WEB_DIR = Path(__file__).resolve().parents[2] / "web"

if WEB_DIR.is_dir():
    @app.get("/", include_in_schema=False)
    def web_index():
        return FileResponse(WEB_DIR / "index.html")

    app.mount("/", StaticFiles(directory=WEB_DIR), name="web")
else:  # pragma: no cover - web/ ships with the repo
    logger.warning("web UI directory %s not found; UI disabled", WEB_DIR)
