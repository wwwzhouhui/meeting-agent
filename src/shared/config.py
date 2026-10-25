"""Settings from environment. No key configured -> mock mode, never crash.

LLM mode (LLM_MODE):
- ``auto`` (default): real LLM when an API key is present, or when
  LLM_BASE_URL is explicitly configured (e.g. a local Ollama / fake
  server / any OpenAI-compatible endpoint); otherwise mock.
- ``mock``: always mock / rule paths, even if a key exists.
- ``llm``: always try the real LLM (a local Ollama needs no key).
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

try:  # optional
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass

DEFAULT_LLM_BASE_URL = "https://api.openai.com/v1"


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        return default


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        return default


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name, "")
    if not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _float_env_opt(name: str) -> float | None:
    """Optional float env var: None when unset/unparseable."""
    raw = os.getenv(name, "")
    if not raw.strip():
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _orch_default(section: str, key: str, fallback: str) -> str:
    """Orchestration-file fallback for provider/engine names
    (src/shared/orchestration.py). Precedence: env var (checked by the
    caller) > orchestration file > the historical built-in default
    passed as `fallback`. Never raises — a broken orchestration file
    must not take Settings construction down with it; the file is
    validated loudly by load_orchestration() at startup."""
    try:
        from src.shared.orchestration import orchestration_defaults

        value = getattr(orchestration_defaults(), section).get(key)
        return str(value) if value else fallback
    except Exception:
        return fallback


@dataclass
class Settings:
    llm_model: str = field(default_factory=lambda: os.getenv("LLM_MODEL", "gpt-4o-mini"))
    # RESERVED (not wired yet): intended for cheap small-model tasks
    # (intent classification etc.) once those get an LLM path. Nothing
    # reads this today except Settings itself — do not rely on it.
    llm_small_model: str = field(
        default_factory=lambda: os.getenv("LLM_SMALL_MODEL", "") or os.getenv("LLM_MODEL", "gpt-4o-mini")
    )
    llm_api_key: str = field(
        default_factory=lambda: os.getenv("LLM_API_KEY", "") or os.getenv("OPENAI_API_KEY", "")
    )
    llm_mode: str = field(
        default_factory=lambda: (os.getenv("LLM_MODE", "") or _orch_default("engines", "llm_mode", "auto")).strip().lower())
    llm_base_url: str = field(
        default_factory=lambda: (os.getenv("LLM_BASE_URL", "") or DEFAULT_LLM_BASE_URL).rstrip("/")
    )
    # Whether LLM_BASE_URL came from the environment (used by auto mode).
    llm_base_url_from_env: bool = field(default_factory=lambda: bool(os.getenv("LLM_BASE_URL", "")))
    llm_timeout: float = field(default_factory=lambda: _float_env("LLM_TIMEOUT", 60.0))
    llm_temperature: float = field(default_factory=lambda: _float_env("LLM_TEMPERATURE", 0.2))
    asr_engine: str = field(
        default_factory=lambda: os.getenv("ASR_ENGINE", "") or _orch_default("engines", "asr", "mock"))
    # Real ASR (src/agents/minutes/transcriber.py, FasterWhisperEngine).
    # Only used when ASR_ENGINE=faster-whisper; the faster-whisper package
    # itself is an optional extra (pip install -e ".[asr]") and the model
    # is downloaded lazily on first transcription, never at import time.
    asr_model: str = field(default_factory=lambda: os.getenv("ASR_MODEL", "small"))
    asr_device: str = field(default_factory=lambda: os.getenv("ASR_DEVICE", "cpu"))
    asr_compute_type: str = field(default_factory=lambda: os.getenv("ASR_COMPUTE_TYPE", "int8"))
    # zh (default) | en | ... | auto (let whisper detect). Anything other
    # than "auto" is passed to whisper as the forced language.
    asr_language: str = field(default_factory=lambda: os.getenv("ASR_LANGUAGE", "zh"))
    # Where POST /meetings/{id}/audio stores uploaded recordings.
    asr_upload_dir: str = field(default_factory=lambda: os.getenv("ASR_UPLOAD_DIR", "data/uploads"))
    # Volcano Doubao recording-file ASR (ASR_ENGINE=volcano;
    # VolcanoASREngine in src/agents/minutes/transcriber.py). Cloud
    # async submit/query API — it takes a PUBLIC audio URL only, never
    # a file. Auth for THIS api is app id + access token (request
    # headers X-Api-App-Key / X-Api-Access-Key).
    volcano_asr_app_id: str = field(default_factory=lambda: os.getenv("VOLCANO_ASR_APP_ID", ""))
    volcano_asr_access_token: str = field(
        default_factory=lambda: os.getenv("VOLCANO_ASR_ACCESS_TOKEN", ""))
    # RESERVED for this integration: the auc/bigmodel API does NOT use
    # the secret key (auth is app id + access token only); the secret
    # belongs to other/older Volcano voice APIs. It is parsed here only
    # so a full credential set can live in one .env — the engine never
    # sends it.
    volcano_asr_secret_key: str = field(default_factory=lambda: os.getenv("VOLCANO_ASR_SECRET_KEY", ""))
    volcano_asr_resource: str = field(
        default_factory=lambda: os.getenv("VOLCANO_ASR_RESOURCE", "volc.seedasr.auc"))
    volcano_asr_poll_interval_sec: float = field(
        default_factory=lambda: _float_env("VOLCANO_ASR_POLL_INTERVAL_SEC", 5.0))
    # How long ONE transcribe() call may wait synchronously while the
    # cloud task is still pending (60s default). Past this the call
    # returns [] with a "still processing" reason and the task id kept
    # on the meeting — re-running /process resumes polling by that id.
    # This is what keeps a long recording from parking a request thread
    # for up to the old 30-minute timeout.
    volcano_asr_sync_wait_sec: float = field(
        default_factory=lambda: _float_env("VOLCANO_ASR_SYNC_WAIT_SEC", 60.0))
    # Hard ceiling on a single call's wait: the effective per-call
    # budget is min(volcano_asr_sync_wait_sec, volcano_asr_timeout_sec).
    # (Historically this was the total poll timeout; with the bounded
    # sync wait it only bites when configured BELOW the sync wait.)
    volcano_asr_timeout_sec: float = field(
        default_factory=lambda: _float_env("VOLCANO_ASR_TIMEOUT_SEC", 1800.0))
    calendar_provider: str = field(
        default_factory=lambda: os.getenv("CALENDAR_PROVIDER", "") or _orch_default("providers", "calendar", "mock"))
    task_provider: str = field(
        default_factory=lambda: os.getenv("TASK_PROVIDER", "") or _orch_default("providers", "task", "mock"))
    # Used by the reminder scanner (src/orchestrator/reminders.py) via
    # build_notify_tool() in src/tools/factory.py; mock -> MockNotifyTool.
    notify_provider: str = field(
        default_factory=lambda: os.getenv("NOTIFY_PROVIDER", "") or _orch_default("providers", "notify", "mock"))
    # Reminder scheduler (APScheduler job in src/api/main.py). Disabled by
    # default so demos/tests never start background threads; the scan can
    # always be triggered manually via POST /reminders/run.
    reminders_enabled: bool = field(default_factory=lambda: _bool_env("REMINDERS_ENABLED", False))
    reminder_scan_interval_min: int = field(
        default_factory=lambda: _int_env("REMINDER_SCAN_INTERVAL_MIN", 30))
    reminder_lead_hours: float = field(
        default_factory=lambda: _float_env("REMINDER_LEAD_HOURS", 24.0))
    # Confirmation-gate timeouts (src/orchestrator/gate_timeouts.py).
    # A meeting sitting in awaiting_minutes_confirmation /
    # awaiting_tasks_confirmation longer than GATE_TIMEOUT_HOURS is
    # acted on by the scanner: GATE_TIMEOUT_ACTION=notify (default)
    # sends ONE escalation notice per gate entry; =reject moves the
    # meeting into the existing minutes_rejected / tasks_rejected state
    # with a "confirmation timeout" audit record, from where the usual
    # re-run / re-extract paths apply. Timeouts NEVER auto-confirm.
    # The scheduler only registers the scan job when
    # GATE_TIMEOUTS_ENABLED=true; POST /gate-timeouts/run always works.
    gate_timeouts_enabled: bool = field(default_factory=lambda: _bool_env("GATE_TIMEOUTS_ENABLED", False))
    gate_timeout_hours: float = field(
        default_factory=lambda: _float_env("GATE_TIMEOUT_HOURS", 48.0))
    # Per-gate overrides (None = fall back to gate_timeout_hours). The
    # API/scheduler path fills these from the orchestration file's
    # gates.<kind>.timeout_hours when neither these env vars nor
    # GATE_TIMEOUT_HOURS are set — see src/api/main.py.
    minutes_gate_timeout_hours: float | None = field(
        default_factory=lambda: _float_env_opt("MINUTES_GATE_TIMEOUT_HOURS"))
    tasks_gate_timeout_hours: float | None = field(
        default_factory=lambda: _float_env_opt("TASKS_GATE_TIMEOUT_HOURS"))
    gate_timeout_action: str = field(
        default_factory=lambda: (os.getenv("GATE_TIMEOUT_ACTION", "notify") or "notify").strip().lower())
    gate_timeout_scan_interval_min: int = field(
        default_factory=lambda: _int_env("GATE_TIMEOUT_SCAN_INTERVAL_MIN", 60))
    # Weekly task summary (src/orchestrator/weekly_summary.py). When
    # WEEKLY_SUMMARY_ENABLED=true the API scheduler registers a
    # CronTrigger job for Mondays 09:00 (DEFAULT_TIMEZONE below);
    # POST /weekly-summary/run builds + sends one on demand regardless.
    weekly_summary_enabled: bool = field(
        default_factory=lambda: _bool_env("WEEKLY_SUMMARY_ENABLED", False))
    google_calendar_token: str = field(default_factory=lambda: os.getenv("GOOGLE_CALENDAR_TOKEN", ""))
    outlook_token: str = field(default_factory=lambda: os.getenv("OUTLOOK_ACCESS_TOKEN", ""))
    feishu_app_id: str = field(default_factory=lambda: os.getenv("FEISHU_APP_ID", ""))
    feishu_app_secret: str = field(default_factory=lambda: os.getenv("FEISHU_APP_SECRET", ""))
    # Kept for backwards compatibility with the original skeleton; new
    # deployments should use FEISHU_WEBHOOK_URL below.
    feishu_webhook: str = field(default_factory=lambda: os.getenv("FEISHU_WEBHOOK", ""))
    feishu_calendar_id: str = field(default_factory=lambda: os.getenv("FEISHU_CALENDAR_ID", ""))
    feishu_calendar_auth: str = field(
        default_factory=lambda: (os.getenv("FEISHU_CALENDAR_AUTH", "tenant") or "tenant").strip().lower()
    )
    feishu_user_access_token: str = field(
        default_factory=lambda: os.getenv("FEISHU_USER_ACCESS_TOKEN", "")
    )
    feishu_refresh_token: str = field(default_factory=lambda: os.getenv("FEISHU_REFRESH_TOKEN", ""))
    feishu_webhook_url: str = field(
        default_factory=lambda: os.getenv("FEISHU_WEBHOOK_URL", "") or os.getenv("FEISHU_WEBHOOK", "")
    )
    feishu_webhook_secret: str = field(default_factory=lambda: os.getenv("FEISHU_WEBHOOK_SECRET", ""))
    # Raw JSON object: internal user id -> Feishu ou_... open_id.
    feishu_user_map: str = field(default_factory=lambda: os.getenv("FEISHU_USER_MAP", ""))
    # Zone used when a Feishu tool receives a naive date/time.  Meetings
    # carry their own timezone, but task due dates do not.
    default_timezone: str = field(
        default_factory=lambda: os.getenv("DEFAULT_TIMEZONE", "Asia/Shanghai") or "Asia/Shanghai"
    )
    linear_api_key: str = field(default_factory=lambda: os.getenv("LINEAR_API_KEY", ""))
    trello_api_key: str = field(default_factory=lambda: os.getenv("TRELLO_API_KEY", ""))
    jira_base_url: str = field(default_factory=lambda: os.getenv("JIRA_BASE_URL", ""))
    jira_api_token: str = field(default_factory=lambda: os.getenv("JIRA_API_TOKEN", ""))
    slack_webhook: str = field(default_factory=lambda: os.getenv("SLACK_WEBHOOK", ""))
    # Persistence for src/shared/db.py (make_store): postgresql://... ->
    # PostgresDB (durable); empty -> InMemoryDB (default; data lost on
    # restart). Any other scheme is an error, never a silent fallback.
    database_url: str = field(default_factory=lambda: os.getenv("DATABASE_URL", ""))
    # HTTP Basic auth for the whole API + web UI (src/api/main.py
    # middleware). BOTH must be non-empty to enable; either empty ->
    # disabled, exactly the historical open behaviour (demo/tests).
    basic_auth_username: str = field(default_factory=lambda: os.getenv("BASIC_AUTH_USERNAME", ""))
    basic_auth_password: str = field(default_factory=lambda: os.getenv("BASIC_AUTH_PASSWORD", ""))
    # Fail-closed guard: when true, startup ABORTS unless both BASIC_AUTH
    # credentials are configured — for deployments that must never run
    # (not even briefly, by config mistake) with auth disabled.
    require_auth: bool = field(default_factory=lambda: _bool_env("REQUIRE_AUTH", False))
    # RESERVED (Phase 1+ queue); present in .env.example, kept for parity.
    redis_url: str = field(default_factory=lambda: os.getenv("REDIS_URL", ""))

    @property
    def feishu_user_map_dict(self) -> dict[str, str]:
        """Parsed FEISHU_USER_MAP; malformed JSON is an empty map + warning."""
        raw = (self.feishu_user_map or "").strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError) as exc:
            logger.warning("FEISHU_USER_MAP is not valid JSON (%s); treating it as empty", exc)
            return {}
        if not isinstance(parsed, dict):
            logger.warning("FEISHU_USER_MAP must be a JSON object; treating it as empty")
            return {}
        return {str(k): str(v) for k, v in parsed.items() if k and v}

    @property
    def mock_mode(self) -> bool:
        """Derived from LLM_MODE (kept for backwards compatibility).

        Old behaviour was ``not llm_api_key``; that is now the ``auto``
        case with no key and no explicitly configured base URL.
        """
        mode = (self.llm_mode or "auto").strip().lower()
        if mode == "mock":
            return True
        if mode == "llm":
            return False
        # auto
        if self.llm_api_key:
            return False
        if self.llm_base_url_from_env:
            return False
        if self.llm_base_url and self.llm_base_url.rstrip("/") != DEFAULT_LLM_BASE_URL:
            # base URL was changed programmatically (e.g. tests / local server)
            return False
        return True


def get_settings() -> Settings:
    return Settings()
