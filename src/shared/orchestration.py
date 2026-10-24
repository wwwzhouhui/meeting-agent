"""Agent orchestration configuration (config/orchestration.yaml).

The orchestration file owns STRUCTURE switches: which pipeline steps
run, whether the two confirmation gates are on, which background jobs
exist and on what cadence, and the default provider / engine names.
Environment variables keep owning CREDENTIALS and numeric overrides.

Precedence (highest first), applied per key:

  1. environment variable  (e.g. CALENDAR_PROVIDER, ASR_ENGINE,
     LLM_MODE, REMINDERS_ENABLED, GATE_TIMEOUT_HOURS,
     ORCH_STEP_<NAME>_ENABLED, ORCH_GATE_<NAME>_ENABLED)
  2. config/orchestration.yaml  (or $ORCHESTRATION_CONFIG)
  3. built-in defaults in this module (== the pre-config behaviour)

A missing file is NOT an error: the built-in defaults reproduce the
historical behaviour exactly. A malformed file IS an error
(OrchestrationError, a ValueError) naming the file and the problem —
silently ignoring a broken orchestration file would be worse than
crashing loudly at startup.

This module deliberately does not import src.shared.config (config.py
consults orchestration_defaults() for provider/engine fallbacks, so
the dependency runs the other way).
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

STEP_NAMES = ("brief", "transcribe", "minutes", "tasks", "schedule", "dispatch")
GATE_NAMES = ("minutes", "tasks")
JOB_NAMES = ("reminders", "gate_timeouts", "weekly_summary")
PROVIDER_NAMES = ("calendar", "task", "notify")
ENGINE_NAMES = ("asr", "llm_mode")

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "orchestration.yaml"


class OrchestrationError(ValueError):
    """The orchestration file exists but cannot be used."""


@dataclass
class StepCfg:
    enabled: bool = True


@dataclass
class GateCfg:
    enabled: bool = True
    timeout_hours: float = 48.0


@dataclass
class JobCfg:
    enabled: bool = False
    interval_min: int | None = None
    cron: str | None = None


@dataclass
class Orchestration:
    steps: dict[str, StepCfg] = field(default_factory=dict)
    gates: dict[str, GateCfg] = field(default_factory=dict)
    scheduler: dict[str, JobCfg] = field(default_factory=dict)
    providers: dict[str, str] = field(default_factory=dict)
    engines: dict[str, str] = field(default_factory=dict)
    # Where this config came from: the file path, or "builtin-default".
    source: str = "builtin-default"

    def step_enabled(self, name: str) -> bool:
        cfg = self.steps.get(name)
        return True if cfg is None else bool(cfg.enabled)

    def gate(self, name: str) -> GateCfg:
        return self.gates.get(name) or GateCfg()


def builtin_orchestration() -> Orchestration:
    """Defaults that reproduce the pre-config behaviour exactly:
    every step on, both gates on (48h timeout), every background job
    off, every provider/engine at its historical default."""
    return Orchestration(
        steps={name: StepCfg(enabled=True) for name in STEP_NAMES},
        gates={name: GateCfg(enabled=True, timeout_hours=48.0) for name in GATE_NAMES},
        scheduler={
            "reminders": JobCfg(enabled=False, interval_min=30),
            "gate_timeouts": JobCfg(enabled=False, interval_min=60),
            "weekly_summary": JobCfg(enabled=False, cron="0 9 * * mon"),
        },
        providers={"calendar": "mock", "task": "mock", "notify": "mock"},
        engines={"asr": "mock", "llm_mode": "auto"},
        source="builtin-default",
    )


def _as_bool(value, *, where: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("1", "true", "yes", "on"):
            return True
        if lowered in ("0", "false", "no", "off"):
            return False
    raise OrchestrationError(f"{where}: expected a boolean, got {value!r}")


def _section(data: dict, name: str, path: Path) -> dict:
    raw = data.get(name) or {}
    if not isinstance(raw, dict):
        raise OrchestrationError(f"{path}: section {name!r} must be a mapping, got {type(raw).__name__}")
    return raw


# Numeric ranges for the values the runtime acts on. Out-of-range
# values are load-time errors (OrchestrationError naming the key):
# a non-positive gate timeout would make every pending gate
# "timed out" at the first scan (with GATE_TIMEOUT_ACTION=reject
# that is a mass auto-rejection), and a broken cron / interval
# would otherwise only surface as a dead scheduler job at runtime.
_TIMEOUT_HOURS_RANGE = (0.0, 720.0)      # exclusive low, inclusive high
_INTERVAL_MIN_RANGE = (1, 1440)          # inclusive both ends


def _validate_cron(cron: str, *, where: str) -> None:
    """Raise OrchestrationError unless APScheduler can parse `cron`.

    Imported lazily: this module is also consulted by low-level
    config code, and cron validation is the only place it needs
    APScheduler (a hard dependency of the API app)."""
    try:
        from apscheduler.triggers.cron import CronTrigger
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise OrchestrationError(
            f"{where}: cannot validate cron without APScheduler installed") from exc
    try:
        CronTrigger.from_crontab(cron)
    except Exception as exc:
        raise OrchestrationError(
            f"{where}: invalid cron {cron!r}: {exc}") from exc


def _parse(data: dict, path: Path) -> Orchestration:
    if not isinstance(data, dict):
        raise OrchestrationError(f"{path}: top level must be a mapping, got {type(data).__name__}")
    orch = builtin_orchestration()
    orch.source = str(path)
    for key in data:
        if key not in ("pipeline", "gates", "scheduler", "providers", "engines"):
            logger.warning("orchestration %s: unknown top-level key %r ignored", path, key)

    pipeline = _section(data, "pipeline", path)
    steps_raw = pipeline.get("steps") or {}
    if not isinstance(steps_raw, dict):
        raise OrchestrationError(f"{path}: pipeline.steps must be a mapping")
    for name, raw in steps_raw.items():
        if name not in STEP_NAMES:
            logger.warning("orchestration %s: unknown pipeline step %r ignored", path, name)
            continue
        raw = raw or {}
        if not isinstance(raw, dict):
            raise OrchestrationError(f"{path}: pipeline.steps.{name} must be a mapping")
        if "enabled" in raw:
            orch.steps[name].enabled = _as_bool(raw["enabled"], where=f"{path}: pipeline.steps.{name}.enabled")

    gates_raw = _section(data, "gates", path)
    for name, raw in gates_raw.items():
        if name not in GATE_NAMES:
            logger.warning("orchestration %s: unknown gate %r ignored", path, name)
            continue
        raw = raw or {}
        if not isinstance(raw, dict):
            raise OrchestrationError(f"{path}: gates.{name} must be a mapping")
        if "enabled" in raw:
            orch.gates[name].enabled = _as_bool(raw["enabled"], where=f"{path}: gates.{name}.enabled")
        if "timeout_hours" in raw:
            try:
                hours = float(raw["timeout_hours"])
            except (TypeError, ValueError):
                raise OrchestrationError(
                    f"{path}: gates.{name}.timeout_hours must be a number, got {raw['timeout_hours']!r}")
            low, high = _TIMEOUT_HOURS_RANGE
            if not (low < hours <= high):
                raise OrchestrationError(
                    f"{path}: gates.{name}.timeout_hours must be within "
                    f"({low:g}, {high:g}] hours, got {raw['timeout_hours']!r}")
            orch.gates[name].timeout_hours = hours

    sched_raw = _section(data, "scheduler", path)
    for name, raw in sched_raw.items():
        if name not in JOB_NAMES:
            logger.warning("orchestration %s: unknown scheduler job %r ignored", path, name)
            continue
        raw = raw or {}
        if not isinstance(raw, dict):
            raise OrchestrationError(f"{path}: scheduler.{name} must be a mapping")
        job = orch.scheduler[name]
        if "enabled" in raw:
            job.enabled = _as_bool(raw["enabled"], where=f"{path}: scheduler.{name}.enabled")
        if "interval_min" in raw and raw["interval_min"] is not None:
            try:
                interval = int(raw["interval_min"])
            except (TypeError, ValueError):
                raise OrchestrationError(
                    f"{path}: scheduler.{name}.interval_min must be an integer, got {raw['interval_min']!r}")
            low, high = _INTERVAL_MIN_RANGE
            if not (low <= interval <= high):
                raise OrchestrationError(
                    f"{path}: scheduler.{name}.interval_min must be within "
                    f"{low}..{high}, got {raw['interval_min']!r}")
            job.interval_min = interval
        if "cron" in raw and raw["cron"] is not None:
            cron = str(raw["cron"])
            if cron.strip():
                _validate_cron(cron, where=f"{path}: scheduler.{name}.cron")
            job.cron = cron

    for section_name, known, target in (
            ("providers", PROVIDER_NAMES, orch.providers),
            ("engines", ENGINE_NAMES, orch.engines)):
        raw_section = _section(data, section_name, path)
        for name, raw in raw_section.items():
            if name not in known:
                logger.warning("orchestration %s: unknown %s %r ignored", path, section_name, name)
                continue
            target[name] = str(raw).strip().lower()
    return orch


def _apply_env_overrides(orch: Orchestration) -> None:
    """ORCH_STEP_<NAME>_ENABLED / ORCH_GATE_<NAME>_ENABLED beat the
    file. A malformed override is warned about and ignored (an env
    typo must not take the service down; the file value still applies).
    Scheduler/provider/engine env vars are NOT applied here — they
    live on Settings / the scheduler wiring, which outrank this file
    (see the precedence list in the module docstring)."""
    for name in STEP_NAMES:
        raw = os.getenv(f"ORCH_STEP_{name.upper()}_ENABLED")
        if raw is None or not raw.strip():
            continue
        try:
            orch.steps[name].enabled = _as_bool(raw, where=f"ORCH_STEP_{name.upper()}_ENABLED")
        except OrchestrationError as exc:
            logger.warning("ignoring bad env override: %s", exc)
    for name in GATE_NAMES:
        raw = os.getenv(f"ORCH_GATE_{name.upper()}_ENABLED")
        if raw is None or not raw.strip():
            continue
        try:
            orch.gates[name].enabled = _as_bool(raw, where=f"ORCH_GATE_{name.upper()}_ENABLED")
        except OrchestrationError as exc:
            logger.warning("ignoring bad env override: %s", exc)


def resolve_config_path(path: str | Path | None = None) -> Path:
    if path is not None:
        return Path(path)
    env_path = os.getenv("ORCHESTRATION_CONFIG", "").strip()
    if env_path:
        return Path(env_path)
    return DEFAULT_CONFIG_PATH


def load_orchestration(path: str | Path | None = None) -> Orchestration:
    """Load the orchestration config.

    Missing file -> built-in defaults (historical behaviour), logged
    at INFO. Existing but unreadable/malformed file ->
    OrchestrationError naming the file and the problem.
    """
    resolved = resolve_config_path(path)
    if not resolved.is_file():
        logger.info("orchestration config %s not found; using built-in defaults", resolved)
        orch = builtin_orchestration()
        _apply_env_overrides(orch)
        return orch
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise OrchestrationError(
            f"orchestration config {resolved} exists but PyYAML is not installed; "
            "run: pip install pyyaml") from exc
    try:
        raw_text = resolved.read_text(encoding="utf-8")
    except OSError as exc:
        raise OrchestrationError(f"orchestration config {resolved}: cannot read file: {exc}") from exc
    try:
        data = yaml.safe_load(raw_text)
    except Exception as exc:
        raise OrchestrationError(f"orchestration config {resolved}: invalid YAML: {exc}") from exc
    orch = _parse(data or {}, resolved)
    _apply_env_overrides(orch)
    logger.info("orchestration config loaded from %s", resolved)
    return orch


_cached_defaults: Orchestration | None = None


def orchestration_defaults() -> Orchestration:
    """Process-wide cached config for low-level consumers
    (src/shared/config.py provider/engine fallbacks). Never raises:
    on any load problem it logs and falls back to the built-ins, so a
    broken file surfaces via load_orchestration() at startup instead
    of crashing every Settings construction."""
    global _cached_defaults
    if _cached_defaults is None:
        try:
            _cached_defaults = load_orchestration()
        except Exception as exc:
            logger.warning("orchestration config unusable (%s); using built-in defaults", exc)
            _cached_defaults = builtin_orchestration()
    return _cached_defaults
