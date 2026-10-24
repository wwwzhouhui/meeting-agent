"""Real-ASR smoke test — NOT part of the pytest suite.

    pip install -e ".[asr]"     # faster-whisper (any env that has it)
    python3 examples/asr_smoke.py

What it does:
1. JFK sample (openai/whisper repo's tests/jfk.flac, 11 s, English):
   downloaded through the environment proxy if not already present in
   examples/asr_samples/ (a pre-existing /tmp/jfk.flac is the fallback).
2. Chinese sample (optional): synthesized with edge-tts when available
   (module importable in this interpreter); skipped with a printed
   reason otherwise.
3. Both are transcribed with the project's real Transcriber +
   FasterWhisperEngine (ASR_MODEL, default "small"; cpu/int8) and
   asserted on expected keywords. Transcripts are saved as .txt files
   next to the audio in examples/asr_samples/.

Exit code is non-zero if the JFK transcription fails its assertion.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.agents.minutes.transcriber import Transcriber  # noqa: E402
from src.shared.config import Settings  # noqa: E402
from src.shared.llm import _proxy_kwargs  # noqa: E402

SAMPLES = Path(__file__).with_name("asr_samples")
JFK_URL = "https://github.com/openai/whisper/raw/main/tests/jfk.flac"
ZH_TEXT = "我们决定将上线时间推迟到十一月八号，李四负责支付渠道证书续期。"
ZH_VOICE = "zh-CN-XiaoxiaoNeural"


def _duration_sec(path: Path) -> float | None:
    if not shutil.which("ffprobe"):
        return None
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=30)
        return float(out.stdout.strip())
    except Exception:
        return None


def _ensure_jfk() -> Path | None:
    dest = SAMPLES / "jfk.flac"
    if dest.is_file() and dest.stat().st_size > 0:
        return dest
    try:
        import httpx

        kwargs = {"timeout": 120.0, "trust_env": False, "follow_redirects": True}
        kwargs.update(_proxy_kwargs(JFK_URL))
        with httpx.Client(**kwargs) as http:
            resp = http.get(JFK_URL)
            resp.raise_for_status()
            dest.write_bytes(resp.content)
        print(f"[jfk] downloaded {len(resp.content)} bytes from {JFK_URL}")
        return dest
    except Exception as exc:
        print(f"[jfk] download failed ({exc}); trying /tmp/jfk.flac fallback")
    fallback = Path("/tmp/jfk.flac")
    if fallback.is_file():
        shutil.copyfile(fallback, dest)
        return dest
    return None


def _synth_gtts(dest: Path) -> bool:
    """Fallback TTS route (plain HTTPS, works in proxied sandboxes where
    edge-tts's WSS + strict cert verification is blocked)."""
    if importlib.util.find_spec("gtts") is None:
        return False
    try:
        from gtts import gTTS

        gTTS(ZH_TEXT, lang="zh-CN").save(str(dest))
        ok = dest.is_file() and dest.stat().st_size > 0
        if ok:
            print(f"[zh] synthesized {dest.stat().st_size} bytes with gTTS (fallback)")
        return ok
    except Exception as exc:
        print(f"[zh] gTTS synthesis failed: {exc}")
        return False


def _ensure_zh() -> Path | None:
    dest = SAMPLES / "zh_meeting.mp3"
    if dest.is_file() and dest.stat().st_size > 0:
        return dest
    if importlib.util.find_spec("edge_tts") is None:
        print("[zh] edge-tts not available in this interpreter — trying gTTS fallback")
        return dest if _synth_gtts(dest) else None
    cmd = [sys.executable, "-m", "edge_tts", "--voice", ZH_VOICE,
           "--text", ZH_TEXT, "--write-media", str(dest)]
    # edge-tts talks WSS and does not read *_PROXY env vars by itself;
    # pass the environment's proxy explicitly when one is configured.
    proxy = (os.getenv("HTTPS_PROXY") or os.getenv("https_proxy")
             or os.getenv("ALL_PROXY") or os.getenv("all_proxy"))
    if proxy:
        cmd += ["--proxy", proxy]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180,
                           env=os.environ.copy())
        if r.returncode == 0 and dest.is_file() and dest.stat().st_size > 0:
            print(f"[zh] synthesized {dest.stat().st_size} bytes with edge-tts ({ZH_VOICE})")
            return dest
        tail = (r.stderr or r.stdout).strip().splitlines()
        print(f"[zh] edge-tts synthesis failed (rc={r.returncode}): "
              f"{tail[-1][:300] if tail else 'no output'}")
    except Exception as exc:
        print(f"[zh] edge-tts synthesis error: {exc}")
    dest.unlink(missing_ok=True)  # a failed run may leave a 0-byte file
    return dest if _synth_gtts(dest) else None


def _transcribe(path: Path, language: str):
    """Run the real engine via the project's Transcriber. Returns
    (segments, model_load_sec, transcribe_sec, transcriber)."""
    settings = Settings(
        asr_model=os.getenv("ASR_MODEL", "small"),
        asr_device="cpu", asr_compute_type="int8", asr_language=language)
    tr = Transcriber(engine="faster-whisper", settings=settings)
    engine = tr._resolve_engine()
    t0 = time.perf_counter()
    engine._load()  # first call: may download the model from HF hub
    load_sec = time.perf_counter() - t0
    t0 = time.perf_counter()
    segments = tr.transcribe(str(path))
    wall_sec = time.perf_counter() - t0
    return segments, load_sec, wall_sec, tr


def _report(label: str, path: Path, segments, load_sec: float,
            wall_sec: float, joiner: str) -> str:
    duration = _duration_sec(path)
    if duration is None and segments:
        duration = segments[-1].end_sec
    full_text = joiner.join(s.text for s in segments)
    print(f"[{label}] audio={path.name} duration={duration:.1f}s "
          f"model_load={load_sec:.1f}s transcribe={wall_sec:.1f}s "
          f"segments={len(segments)}")
    print(f"[{label}] text: {full_text}")
    txt_path = path.with_suffix(".txt")
    lines = [
        f"# ASR smoke transcript — {path.name}",
        f"# model=small device=cpu compute_type=int8 "
        f"duration={duration:.1f}s transcribe={wall_sec:.1f}s segments={len(segments)}",
        "",
        full_text,
        "",
    ]
    lines += [f"[{s.start_sec:7.2f} -> {s.end_sec:7.2f}] ({s.speaker}) {s.text}"
              for s in segments]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[{label}] transcript saved to {txt_path}")
    return full_text


def main() -> int:
    SAMPLES.mkdir(parents=True, exist_ok=True)
    failures = 0

    jfk = _ensure_jfk()
    if jfk is None:
        print("[jfk] FAIL: sample unavailable (download failed, no fallback)")
        return 1
    segments, load_sec, wall_sec, tr = _transcribe(jfk, language="en")
    if not segments:
        print(f"[jfk] FAIL: no segments; last_error={tr.last_error}")
        return 1
    text = _report("jfk", jfk, segments, load_sec, wall_sec, joiner=" ")
    low = text.lower()
    if "fellow americans" in low or ("fellow" in low and "american" in low):
        print("[jfk] PASS: contains 'fellow Americans'")
    else:
        print("[jfk] FAIL: expected 'fellow Americans' in transcript")
        failures += 1

    zh = _ensure_zh()
    if zh is None:
        print("[zh] SKIPPED (see reason above)")
    else:
        segments, load_sec, wall_sec, tr = _transcribe(zh, language="zh")
        if not segments:
            print(f"[zh] FAIL: no segments; last_error={tr.last_error}")
            failures += 1
        else:
            text = _report("zh", zh, segments, load_sec, wall_sec, joiner="")
            missing = [kw for kw in ("上线", "证书") if kw not in text]
            if not missing:
                print("[zh] PASS: contains 上线 and 证书")
            else:
                print(f"[zh] FAIL: missing keywords {missing}")
                failures += 1

    print(f"ASR smoke {'OK' if failures == 0 else 'FAILED'}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
