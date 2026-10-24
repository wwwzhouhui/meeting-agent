"""Audio -> transcript segments.

Sources, in order:
- segments already provided (manual transcript upload) — used as-is;
- text — parsed by :meth:`Transcriber.from_text` ('说话人: 内容' lines);
- audio (``audio_url``: http(s) URL or local file path) — transcribed
  by the configured ASR engine.

Engines (``ASR_ENGINE``):
- ``mock`` (default): no transcription. ``transcribe`` returns [] and
  MinutesAgent falls back to its ``needs_transcript`` contract.
- ``faster-whisper``: real transcription via faster-whisper
  (``pip install -e ".[asr]"``). The model is loaded lazily on first
  use — importing this module never downloads or loads anything.
- ``volcano``: Volcano Doubao recording-file ASR (cloud, async
  submit/query). Takes a PUBLIC audio URL only — local files are
  rejected with guidance (use faster-whisper, or host the file first).

Failure contract: transcription problems (engine not installed, model
load failure, audio download failure, missing file) NEVER raise out of
``Transcriber.transcribe``. It returns [] and puts a human-readable
reason on ``self.last_error``; MinutesAgent appends that reason to the
meeting warning so a human sees exactly what to fix.

Speaker diarization: faster-whisper performs NONE (pyannote is not
integrated) — its segments all carry the placeholder speaker
``ASR_SPEAKER_PLACEHOLDER`` and ``self.last_note`` says so, which
MinutesAgent surfaces in ``meeting.warnings``. The volcano engine
returns real speaker labels (``enable_speaker_info``), so no such note
is attached to its transcripts.
"""
from __future__ import annotations

import logging
import re
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

from src.shared.config import Settings, get_settings
from src.shared.llm import _proxy_kwargs
from src.shared.schema import TranscriptSegment

logger = logging.getLogger(__name__)

#: Speaker label for ASR-produced segments (no diarization, see module doc).
ASR_SPEAKER_PLACEHOLDER = "发言人"

_DIARIZATION_NOTE = (
    "ASR transcript has no speaker diarization (pyannote not integrated): "
    f"every segment is labelled with the placeholder speaker "
    f"'{ASR_SPEAKER_PLACEHOLDER}'"
)


class ASRError(Exception):
    """An expected, human-actionable ASR failure (missing package, model
    load failure, unreachable audio, ...). Carries the fix in its message."""


class ASREngine(Protocol):
    """Engine protocol: local audio file -> transcript segments.

    Implementations may raise ASRError (or any exception — Transcriber
    converts it into ``last_error``) on failure.
    """

    def transcribe(self, audio_path: str) -> list[TranscriptSegment]:
        ...


class FasterWhisperEngine:
    """faster-whisper engine. The WhisperModel is imported and loaded
    lazily on the first transcribe call, with a clear ASRError when the
    optional package or the model is unavailable."""

    note = _DIARIZATION_NOTE

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self._model: Any = None

    def _load(self) -> Any:
        if self._model is not None:
            return self._model
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise ASRError(
                "faster-whisper is not installed; install the ASR extra: "
                'pip install -e ".[asr]"'
            ) from exc
        s = self.settings
        try:
            self._model = WhisperModel(
                s.asr_model, device=s.asr_device, compute_type=s.asr_compute_type)
        except Exception as exc:
            raise ASRError(
                f"failed to load whisper model {s.asr_model!r} "
                f"(device={s.asr_device}, compute_type={s.asr_compute_type}): {exc}"
            ) from exc
        return self._model

    def transcribe(self, audio_path: str) -> list[TranscriptSegment]:
        model = self._load()
        kwargs: dict[str, Any] = {"vad_filter": True}
        language = (self.settings.asr_language or "zh").strip().lower()
        if language and language != "auto":
            kwargs["language"] = language
        segments, _info = model.transcribe(audio_path, **kwargs)
        out: list[TranscriptSegment] = []
        for seg in segments:
            text = (seg.text or "").strip()
            if not text:
                continue
            out.append(TranscriptSegment(
                id=len(out) + 1,
                speaker=ASR_SPEAKER_PLACEHOLDER,
                start_sec=float(seg.start),
                end_sec=float(seg.end),
                text=text,
            ))
        return out


# ---------------------------------------------------------------------------
# Volcano Doubao recording-file ASR (cloud engine)
#
# Protocol (verified live against openspeech.bytedance.com):
#   submit: POST /api/v3/auc/bigmodel/submit
#   query:  POST /api/v3/auc/bigmodel/query   (body: {})
#   headers: X-Api-App-Key / X-Api-Access-Key / X-Api-Resource-Id /
#            X-Api-Request-Id (one uuid per task, same on submit+query) /
#            X-Api-Sequence: -1
#   status comes back in the X-Api-Status-Code RESPONSE header:
#     20000000 ok | 20000001 processing | 20000002 queued |
#     20000003 silence (no speech) | 45000xxx param error |
#     550xxxxx server error / task timeout
# Auth is app id + access token ONLY. The VOLCANO_ASR_SECRET_KEY config
# value is deliberately never sent: it belongs to other/older Volcano
# voice APIs, not to this one.
#
# Two hard constraints, both learned from live runs (see AUDIT.md):
# 1. The API takes a public URL, never a file — local paths are refused
#    client-side with guidance instead of being submitted.
# 2. Only wav/mp3/ogg/spx/amr/aac/m4a are accepted. A flac task is not
#    rejected: it hangs server-side until it times out (55001010), so
#    unsupported suffixes are refused client-side before any request.
VOLCANO_ASR_BASE_URL = "https://openspeech.bytedance.com"
_VOLCANO_SUBMIT_PATH = "/api/v3/auc/bigmodel/submit"
_VOLCANO_QUERY_PATH = "/api/v3/auc/bigmodel/query"
VOLCANO_AUDIO_FORMATS = frozenset({"wav", "mp3", "ogg", "spx", "amr", "aac", "m4a"})
_VOLCANO_STATUS_OK = "20000000"
_VOLCANO_STATUS_PENDING = frozenset({"20000001", "20000002"})
_VOLCANO_STATUS_SILENCE = "20000003"

_VOLCANO_LOCAL_GUIDANCE = (
    "volcano 引擎需要公网可访问的音频 URL，本地文件请用 faster-whisper "
    "或先把文件放到对象存储/可公网访问的地址"
    " (the volcano ASR API accepts only publicly reachable audio URLs, "
    "not local files)"
)


class VolcanoASREngine:
    """Volcano Doubao cloud ASR. Async: submit once, then poll query
    with the same X-Api-Request-Id until success / failure / timeout.

    The request id is reported via ``on_task_submitted`` (and kept on
    ``last_task_id``) IMMEDIATELY after a successful submit, before any
    polling — Transcriber/MinutesAgent persist it on the meeting at
    that moment, so a process dying mid-poll does not lose the task.

    ``note`` stays None on success: this engine returns speaker info,
    so the faster-whisper no-diarization caveat does not apply. It is
    only set when the audio format had to be guessed (URL without a
    file extension -> wav assumed).
    """

    #: Transcriber routes the raw audio_url here instead of resolving
    #: it to a local file first.
    requires_public_url = True

    note: str | None = None

    def __init__(self, settings: Settings | None = None, *,
                 transport: Any = None,
                 http_client: Any = None,
                 on_task_submitted: Any = None):
        self.settings = settings or get_settings()
        self._transport = transport
        self._http_client = http_client
        self.on_task_submitted = on_task_submitted
        self.last_task_id: str | None = None
        # Set when the service reports "no speech" (or empty text):
        # transcribe returns [] and Transcriber adopts this as the
        # user-facing reason instead of a generic "no segments".
        self.empty_reason: str | None = None

    # -- plumbing ---------------------------------------------------------
    def _new_client(self):
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - httpx is a core dep
            raise ASRError(f"httpx is not available for volcano ASR: {exc}") from exc
        if self._http_client is not None:
            return self._http_client, False
        kwargs: dict[str, Any] = {
            "timeout": 60.0, "trust_env": False, "follow_redirects": True}
        if self._transport is not None:
            # Injected transport (tests/fakes) IS the network; adding a
            # proxy would make httpx bypass it entirely (see llm.py).
            kwargs["transport"] = self._transport
        else:
            kwargs.update(_proxy_kwargs(VOLCANO_ASR_BASE_URL))
        return httpx.Client(**kwargs), True

    def _check_credentials(self) -> None:
        missing = []
        if not (self.settings.volcano_asr_app_id or "").strip():
            missing.append("VOLCANO_ASR_APP_ID")
        if not (self.settings.volcano_asr_access_token or "").strip():
            missing.append("VOLCANO_ASR_ACCESS_TOKEN")
        if missing:
            raise ASRError(
                "volcano ASR credentials are not configured; set "
                + " and ".join(missing)
                + " (see .env.example, VOLCANO_ASR section)")

    def _headers(self, request_id: str) -> dict[str, str]:
        s = self.settings
        return {
            "X-Api-App-Key": s.volcano_asr_app_id,
            "X-Api-Access-Key": s.volcano_asr_access_token,
            "X-Api-Resource-Id": s.volcano_asr_resource,
            "X-Api-Request-Id": request_id,
            "X-Api-Sequence": "-1",
            "Content-Type": "application/json",
        }

    def _post(self, http, path: str, request_id: str, payload: dict) -> Any:
        try:
            return http.post(VOLCANO_ASR_BASE_URL + path, json=payload,
                             headers=self._headers(request_id))
        except ASRError:
            raise
        except Exception as exc:
            raise ASRError(f"volcano ASR request to {path} failed: {exc}") from exc

    @staticmethod
    def _status_of(resp) -> tuple[str, str]:
        code = (resp.headers.get("X-Api-Status-Code") or "").strip()
        message = (resp.headers.get("X-Api-Message") or "").strip()
        return code, message

    def _raise_for_status(self, resp, stage: str, request_id: str) -> None:
        code, message = self._status_of(resp)
        detail = f"X-Api-Status-Code {code or '(missing)'}"
        if message:
            detail += f", X-Api-Message: {message}"
        if not code:
            detail += f", HTTP {resp.status_code}: {resp.text[:200]}"
        raise ASRError(f"volcano ASR {stage} failed for task {request_id}: {detail}")

    # -- input validation ---------------------------------------------------
    def _format_for(self, audio_url: str, parsed) -> str:
        suffix = Path(parsed.path).suffix.lower().lstrip(".")
        if not suffix:
            self.note = (
                "volcano ASR: the audio URL has no file extension, so "
                "format 'wav' was assumed (the API requires an explicit "
                "format); rename the file or use an extension-bearing URL "
                "if the audio is not wav")
            return "wav"
        if suffix not in VOLCANO_AUDIO_FORMATS:
            raise ASRError(
                f"volcano ASR does not support audio format {suffix!r} "
                f"(URL: {audio_url}); supported formats: "
                + ", ".join(sorted(VOLCANO_AUDIO_FORMATS))
                + ". flac in particular is NOT supported — a submitted "
                  "flac task is not rejected, it hangs server-side until "
                  "it times out; convert the audio to wav/mp3 first, or "
                  "use ASR_ENGINE=faster-whisper for local files")
        return suffix

    # -- engine protocol ------------------------------------------------------
    def transcribe(self, audio_path: str) -> list[TranscriptSegment]:
        """Local-file entry point of the shared engine protocol. The
        volcano API cannot accept files at all, so this always refuses
        — with the guidance a human needs, never a silent no-op."""
        raise ASRError(f"{_VOLCANO_LOCAL_GUIDANCE}; got local path: {audio_path}")

    def transcribe_url(self, audio_url: str) -> list[TranscriptSegment]:
        self._check_credentials()
        parsed = urlparse(audio_url)
        if parsed.scheme not in ("http", "https"):
            raise ASRError(f"{_VOLCANO_LOCAL_GUIDANCE}; got: {audio_url}")
        audio_format = self._format_for(audio_url, parsed)
        request_id = str(uuid.uuid4())
        http, close = self._new_client()
        try:
            submit = self._post(http, _VOLCANO_SUBMIT_PATH, request_id, {
                "audio": {"url": audio_url, "format": audio_format},
                "request": {
                    "model_name": "bigmodel",
                    "enable_itn": True,
                    "enable_punc": True,
                    "show_utterances": True,
                    "enable_speaker_info": True,
                },
            })
            code, _msg = self._status_of(submit)
            if code == _VOLCANO_STATUS_SILENCE:
                self.empty_reason = self._silence_reason(request_id)
                return []
            if code != _VOLCANO_STATUS_OK and code not in _VOLCANO_STATUS_PENDING:
                self._raise_for_status(submit, "submit", request_id)
            # Submitted. Persist the task id BEFORE polling starts, so a
            # crash during the (potentially long) poll loses nothing.
            self.last_task_id = request_id
            if self.on_task_submitted is not None:
                self.on_task_submitted(request_id)
            return self._poll(http, request_id)
        finally:
            if close:
                http.close()

    @staticmethod
    def _silence_reason(request_id: str) -> str:
        return (
            "音频中未检测到人声 (volcano ASR reported silence, "
            f"X-Api-Status-Code 20000003, task {request_id}); "
            "no transcript could be produced from this recording")

    def _poll(self, http, request_id: str) -> list[TranscriptSegment]:
        timeout = max(0.0, float(self.settings.volcano_asr_timeout_sec))
        interval = max(0.0, float(self.settings.volcano_asr_poll_interval_sec))
        deadline = time.monotonic() + timeout
        while True:
            resp = self._post(http, _VOLCANO_QUERY_PATH, request_id, {})
            code, _msg = self._status_of(resp)
            if code == _VOLCANO_STATUS_OK:
                return self._segments_from(resp, request_id)
            if code == _VOLCANO_STATUS_SILENCE:
                self.empty_reason = self._silence_reason(request_id)
                return []
            if code in _VOLCANO_STATUS_PENDING:
                if time.monotonic() >= deadline:
                    raise ASRError(
                        f"volcano ASR task {request_id} timed out after "
                        f"{timeout:g}s of polling (last status {code}); "
                        "raise VOLCANO_ASR_TIMEOUT_SEC for very long "
                        "recordings, and check the audio URL is reachable "
                        "from the public internet")
                time.sleep(interval)
                continue
            self._raise_for_status(resp, "query", request_id)

    def _segments_from(self, resp, request_id: str) -> list[TranscriptSegment]:
        try:
            body = resp.json()
        except Exception as exc:
            raise ASRError(
                f"volcano ASR task {request_id}: success response was "
                f"not valid JSON: {exc}") from exc
        result = (body or {}).get("result") or {}
        segments: list[TranscriptSegment] = []
        for utt in result.get("utterances") or []:
            text = (utt.get("text") or "").strip()
            if not text:
                continue
            additions = utt.get("additions") or {}
            raw_speaker = additions.get("speaker")
            if raw_speaker is None or str(raw_speaker).strip() == "":
                speaker = ASR_SPEAKER_PLACEHOLDER
            else:
                # Keep the raw service value visible: 说话人1, 说话人2...
                speaker = f"说话人{raw_speaker}"
            segments.append(TranscriptSegment(
                id=len(segments) + 1,
                speaker=speaker,
                start_sec=float(utt.get("start_time") or 0) / 1000.0,
                end_sec=float(utt.get("end_time") or 0) / 1000.0,
                text=text,
            ))
        if segments:
            return segments
        text = (result.get("text") or "").strip()
        if text:
            duration_ms = float(((body or {}).get("audio_info") or {}).get("duration") or 0)
            return [TranscriptSegment(
                id=1, speaker=ASR_SPEAKER_PLACEHOLDER,
                start_sec=0.0, end_sec=duration_ms / 1000.0, text=text)]
        self.empty_reason = (
            f"volcano ASR task {request_id} succeeded but returned no "
            "transcript text (empty result)")
        return []


class Transcriber:
    """Resolves audio to a local file and delegates to the ASR engine.

    ``engine`` is the ASR_ENGINE name (mock | faster-whisper |
    volcano). Tests inject a fake via ``engine_impl`` (any object with
    ``transcribe(audio_path) -> list[TranscriptSegment]``) and/or a fake
    HTTP ``transport``/``http_client`` for the URL-download path.
    Engines with ``requires_public_url`` (volcano) instead receive the
    raw audio_url via ``transcribe_url`` — no local resolution happens
    for them at all.
    """

    def __init__(self, engine: str = "mock", *,
                 settings: Settings | None = None,
                 engine_impl: ASREngine | None = None,
                 transport: Any = None,
                 http_client: Any = None):
        self.engine = engine
        self.settings = settings or get_settings()
        self._engine_impl = engine_impl
        self._transport = transport
        self._http_client = http_client
        # Set by transcribe(): why it returned [] (failure), or a note
        # about the produced transcript (success, e.g. no diarization).
        self.last_error: str | None = None
        self.last_note: str | None = None
        # Cloud ASR bookkeeping: the task id of the latest submission
        # (set the moment the service accepts the task, before polling
        # finishes) and an optional consumer callback for it.
        # MinutesAgent wires the callback to persist the id on the
        # meeting immediately — see MinutesAgent._persist_asr_task_id.
        self.last_task_id: str | None = None
        self.on_task_submitted: Any = None

    def _on_engine_task_submitted(self, task_id: str) -> None:
        self.last_task_id = task_id
        callback = self.on_task_submitted
        if callback is not None:
            try:
                callback(task_id)
            except Exception:
                logger.exception("on_task_submitted callback failed for task %s", task_id)

    # -- engine selection --------------------------------------------------
    def _resolve_engine(self) -> ASREngine | None:
        if self._engine_impl is not None:
            return self._engine_impl
        name = (self.engine or "mock").strip().lower()
        if name in ("faster-whisper", "faster_whisper", "whisper"):
            self._engine_impl = FasterWhisperEngine(self.settings)
            return self._engine_impl
        if name in ("volcano", "volcano-asr", "volcano_asr", "doubao"):
            self._engine_impl = VolcanoASREngine(
                self.settings,
                transport=self._transport,
                http_client=self._http_client,
                on_task_submitted=self._on_engine_task_submitted)
            return self._engine_impl
        return None

    # -- audio source resolution --------------------------------------------
    def _resolve_audio_path(self, audio_url: str) -> tuple[str, bool]:
        """Return (local_path, is_temp). Raises ASRError on any problem."""
        parsed = urlparse(audio_url)
        if parsed.scheme in ("http", "https"):
            return self._download(audio_url, parsed), True
        path = Path(audio_url)
        if not path.is_file():
            raise ASRError(
                f"audio file not found: {audio_url} "
                "(audio_url must be an http(s) URL or an existing local path)")
        return str(path), False

    def _download(self, url: str, parsed) -> str:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - httpx is a core dep
            raise ASRError(f"httpx is not available to download audio: {exc}") from exc
        suffix = Path(parsed.path).suffix.lower()
        if not re.fullmatch(r"\.[a-z0-9]{1,8}", suffix or ""):
            suffix = ".audio"
        tmp = tempfile.NamedTemporaryFile(
            prefix="meeting-agent-asr-", suffix=suffix, delete=False)
        tmp_path = tmp.name
        tmp.close()
        if self._http_client is not None:
            http = self._http_client
            close = False
        else:
            kwargs: dict[str, Any] = {
                "timeout": 120.0, "trust_env": False, "follow_redirects": True}
            if self._transport is not None:
                # Injected transport (tests/fakes) IS the network; adding
                # a proxy would make httpx bypass it entirely (see llm.py).
                kwargs["transport"] = self._transport
            else:
                kwargs.update(_proxy_kwargs(url))
            http = httpx.Client(**kwargs)
            close = True
        try:
            with http.stream("GET", url) as resp:
                if resp.status_code >= 400:
                    raise ASRError(f"audio download failed: HTTP {resp.status_code} for {url}")
                with open(tmp_path, "wb") as fh:
                    for chunk in resp.iter_bytes():
                        fh.write(chunk)
        except ASRError:
            Path(tmp_path).unlink(missing_ok=True)
            raise
        except Exception as exc:
            Path(tmp_path).unlink(missing_ok=True)
            raise ASRError(f"audio download failed for {url}: {exc}") from exc
        finally:
            if close:
                http.close()
        return tmp_path

    # -- main entry -----------------------------------------------------------
    def transcribe(self, audio_url: str | None = None,
                   segments: list[TranscriptSegment] | None = None) -> list[TranscriptSegment]:
        if segments:
            return segments
        self.last_error = None
        self.last_note = None
        self.last_task_id = None
        if not audio_url:
            return []
        engine = self._resolve_engine()
        if engine is None:
            name = (self.engine or "mock").strip().lower()
            if name == "mock":
                self.last_error = (
                    "ASR engine is 'mock' (no real transcription); set "
                    "ASR_ENGINE=faster-whisper and install the ASR extra "
                    '(pip install -e ".[asr]") to transcribe audio, or '
                    "ASR_ENGINE=volcano with VOLCANO_ASR_* credentials "
                    "for the cloud engine")
            else:
                self.last_error = (
                    f"unknown ASR engine {self.engine!r}; "
                    "supported: mock, faster-whisper, volcano")
            logger.warning("ASR skipped for %s: %s", audio_url, self.last_error)
            return []
        if getattr(engine, "requires_public_url", False):
            # Cloud engine: it consumes the URL itself. No download, no
            # local file — and a local path is refused by the engine
            # with guidance, never submitted.
            try:
                result = engine.transcribe_url(audio_url)  # type: ignore[attr-defined]
            except Exception as exc:
                self.last_error = f"ASR transcription failed: {exc}"
                logger.warning("ASR failed for %s: %s", audio_url, exc)
                return []
            self.last_note = getattr(engine, "note", None)
            self._adopt_empty_reason(engine, result)
            return result
        try:
            path, is_temp = self._resolve_audio_path(audio_url)
        except ASRError as exc:
            self.last_error = str(exc)
            logger.warning("ASR failed for %s: %s", audio_url, exc)
            return []
        try:
            result = engine.transcribe(path)
        except Exception as exc:
            self.last_error = f"ASR transcription failed: {exc}"
            logger.warning("ASR failed for %s: %s", audio_url, exc)
            return []
        finally:
            if is_temp:
                Path(path).unlink(missing_ok=True)
        self.last_note = getattr(engine, "note", None)
        self._adopt_empty_reason(engine, result)
        return result

    def _adopt_empty_reason(self, engine: ASREngine, result: list) -> None:
        """An engine that legitimately produced nothing (e.g. volcano
        reporting silence) explains WHY via ``empty_reason``; surface
        that as last_error so the needs_transcript warning is specific
        instead of a generic 'no segments'."""
        if not result:
            reason = getattr(engine, "empty_reason", None)
            if reason:
                self.last_error = reason

    @staticmethod
    def from_text(text: str, default_speaker: str = "未知") -> list[TranscriptSegment]:
        segments: list[TranscriptSegment] = []
        for line in (text or "").splitlines():
            line = line.strip()
            if not line:
                continue
            m = re.match(r"^([^:：]{1,20})[:：]\s*(.+)$", line)
            if m:
                speaker, content = m.group(1).strip(), m.group(2).strip()
            else:
                speaker, content = default_speaker, line
            idx = len(segments) + 1
            segments.append(TranscriptSegment(
                id=idx, speaker=speaker, start_sec=float((idx - 1) * 30),
                end_sec=float(idx * 30), text=content,
            ))
        return segments
