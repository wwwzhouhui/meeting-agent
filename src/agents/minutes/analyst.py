"""NoteAnalyst: transcript -> structured minutes.

Primary path (real LLM, when configured):
    prompt -> LLMClient.chat_json -> MinutesResult.model_validate
    -> validate_evidence filter -> decision dedup -> suspect-decision
    check (decisions that read like dispatched todos).
    On call/parse/validation failure — suspects included — ONE repair
    retry (the error is fed back to the model); if that also fails,
    fall back to rules. Suspects surviving the repair are KEPT
    verbatim and flagged in MinutesResult.generation_notes: the
    detector never deletes or rewrites decision text (see
    find_suspect_decisions for why hard-stripping would false-positive).
Fallback path (rules, ``_analyze_rules``): keyword/regex extraction.
    Used with generation_source="rules" when no LLM is configured, or
    "rule_fallback" when an LLM was tried and failed. Never raises.

Red line executed in both paths: any decision/risk/topic whose
segment_ids are empty or not present in the transcript is dropped.
"""
from __future__ import annotations

import copy
import json
import logging
import re

from src.shared.llm import LLMClient, LLMError, extract_json
from src.shared.schema import Decision, MinutesResult, Risk, Topic, TranscriptSegment

from .prompts import build_analyst_messages, build_repair_messages

logger = logging.getLogger(__name__)

DECISION_CUES = ("决定", "确认", "同意", "推迟到", "推迟至", "改为", "就这么定", "敲定")
RISK_CUES = ("风险", "过期", "未完成", "还没完成", "还差", "阻塞", "延期", "来不及")
QUESTION_CUES = ("未定", "还没定", "待确认", "待定", "没有定", "再确认")
TOPIC_KEYWORDS = {
    "上线时间": ("上线", "发布", "灰度"),
    "支付联调": ("支付", "联调", "接口"),
    "证书续期": ("证书", "续期", "过期"),
    "灰度方案": ("灰度",),
}


def validate_evidence(items, valid_ids: set[int]):
    """Drop items with no evidence or evidence outside the transcript."""
    kept = []
    for it in items:
        ids = getattr(it, "segment_ids", None) or getattr(it, "source_segment_ids", None) or []
        if ids and all(i in valid_ids for i in ids):
            kept.append(it)
    return kept


def _coerce_ids_in_place(data: dict) -> dict:
    """Best-effort cleanup of segment_ids before validation.

    - a single int / numeric string becomes a one-item list;
    - numeric strings inside lists become ints ("3" -> 3);
    - non-numeric entries are removed (validation/filtering then
      decides whether the item itself survives).
    """
    for key in ("decisions", "risks", "topics"):
        items = data.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            ids = item.get("segment_ids")
            if ids is None:
                continue
            if isinstance(ids, (int, str)):
                ids = [ids]
            if isinstance(ids, list):
                coerced: list[int] = []
                for v in ids:
                    if isinstance(v, bool):
                        continue
                    if isinstance(v, int):
                        coerced.append(v)
                    elif isinstance(v, str) and v.strip().lstrip("-").isdigit():
                        coerced.append(int(v.strip()))
                    elif isinstance(v, float) and v.is_integer():
                        coerced.append(int(v))
                item["segment_ids"] = coerced
    return data


# --- suspect-decision detection (decision / todo boundary) -------------
# Field report (2026-10-23, live Ark run): the LLM minutes put three
# assignment sentences ("李四负责…", "王五负责…") into decisions,
# duplicating the task list. A decision that (a) names a participant AND
# (b) carries an assignment marker reads like a dispatched todo, not a
# collective conclusion. Both conditions are required and the marker set
# is deliberately conservative, because hard-stripping would
# false-positive on genuine decisions:
#   "由李四牵头成立专项组" — names a person, but 牵头 without 负责 is an
#       organisational conclusion, not a dispatch -> clean;
#   "上线推迟至11月8日" — carries a date, but names nobody -> clean;
#   "证书到期前必须完成续期" — deadline-flavoured collective rule, but
#       names nobody -> clean.
# Suspects are therefore NEVER deleted or rewritten here: they are fed
# into the existing single repair retry for the model to re-judge, and
# whatever survives repair is kept verbatim with a generation note.
_ASSIGNMENT_MARKERS: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    ("负责", re.compile(r"负责")),
    ("在…前完成/提交/交付", re.compile(r"(?:在|于)[^，。；、]{0,12}前(?:完成|提交|交付)")),
)


def find_suspect_decisions(decisions: list[Decision], names) -> list[tuple[int, Decision, str, str]]:
    """Decisions that look like todos: participant name + assignment marker.

    Returns [(1-based index, decision, matched name, marker label)].
    """
    clean_names = [n for n in dict.fromkeys(names or []) if n and len(n) >= 2]
    suspects: list[tuple[int, Decision, str, str]] = []
    for idx, d in enumerate(decisions, start=1):
        text = d.text or ""
        hit_name = next((n for n in clean_names if n in text), None)
        if not hit_name:
            continue
        hit_marker = next(
            (label for label, pat in _ASSIGNMENT_MARKERS if pat.search(text)), None)
        if hit_marker:
            suspects.append((idx, d, hit_name, hit_marker))
    return suspects


def _suspect_error_message(suspects) -> str:
    lines = ["纪要校验未通过：decisions 中有条目疑似待办（分派句），不属于集体决策："]
    for idx, d, name, marker in suspects:
        lines.append(
            f"- 第{idx}条「{d.text}」：同时出现参会人姓名「{name}」与分派表述（{marker}）。")
    lines.append(
        "请修正：把这类条目移出 decisions（系统会另行从逐字稿抽取为任务，"
        "不要在纪要中重复）；若其内容构成集体结论（例如「证书须在到期前完成续期」），"
        "请改写为不带人名分派的结论句后保留在 decisions 中。其余内容保持不变。")
    return "\n".join(lines)


class _SuspectDecisionsError(Exception):
    """Validation-style failure: decisions contain suspected todos.

    Raised inside the first LLM attempt so the existing repair retry
    re-judges them; str(exc) is the instruction fed to the repair.
    """

    def __init__(self, suspects):
        self.suspects = suspects
        super().__init__(_suspect_error_message(suspects))


def _dedup_decisions(decisions: list[Decision]) -> list[Decision]:
    merged: dict[str, Decision] = {}
    for d in decisions:
        if d.text in merged:
            for sid in d.segment_ids:
                if sid not in merged[d.text].segment_ids:
                    merged[d.text].segment_ids.append(sid)
        else:
            merged[d.text] = d
    return list(merged.values())


class NoteAnalyst:
    def __init__(self, llm: LLMClient | None = None):
        self.llm = llm or LLMClient()

    # ------------------------------------------------------------------ API
    def analyze(self, transcript: list[TranscriptSegment], title: str = "",
                participants: list[str] | None = None,
                glossary: list[str] | None = None) -> MinutesResult:
        valid = {s.id for s in transcript}
        if self._llm_available():
            try:
                result = self._analyze_llm(transcript, title, valid, participants, glossary)
                return result
            except _RepairExhausted:
                pass  # _analyze_llm already logged; fall through to rules
            except Exception as exc:  # defensive: LLM path must never crash callers
                logger.warning("LLM minutes path failed unexpectedly: %s", exc)
            result = self._analyze_rules(transcript, title)
            result.generation_source = "rule_fallback"
            result.generation_model = getattr(self.llm, "model", None)
            return result
        result = self._analyze_rules(transcript, title)
        result.generation_source = "rules"
        result.generation_model = None
        return result

    # ------------------------------------------------------------- LLM path
    def _llm_available(self) -> bool:
        llm = self.llm
        if llm is None:
            return False
        available = getattr(llm, "available", None)
        if isinstance(available, bool):
            return available
        is_mock = getattr(llm, "is_mock", True)
        if callable(is_mock):
            is_mock = is_mock()
        return not bool(is_mock)

    def _call_json(self, messages) -> dict:
        llm = self.llm
        if hasattr(llm, "chat_json"):
            return llm.chat_json(messages)
        # Duck-typed fallback for stubs exposing only .chat()
        text = llm.chat(messages)  # type: ignore[attr-defined]
        return extract_json(text)

    def _analyze_llm(self, transcript, title, valid, participants, glossary) -> MinutesResult:
        # Names a decision must not be "assigned to": the declared
        # participants plus everyone who actually speaks in the transcript.
        names = {p for p in (participants or []) if p}
        names |= {s.speaker for s in transcript if s.speaker}
        messages = build_analyst_messages(title, participants or [], transcript, glossary)
        previous = ""
        try:
            data = self._call_json(messages)
            previous = json.dumps(data, ensure_ascii=False) if isinstance(data, (dict, list)) else str(data)
            result = self._validate_and_clean(data, valid)
            suspects = find_suspect_decisions(result.decisions, names)
            if suspects:
                # Not a hard failure of the payload — a quality defect.
                # Route it through the same repair retry as validation
                # errors so the MODEL re-judges; never strip here.
                raise _SuspectDecisionsError(suspects)
            result.generation_source = "llm"
            result.generation_model = getattr(self.llm, "model", None)
            return result
        except Exception as exc:
            first_error = exc
            if isinstance(exc, LLMError) and exc.raw:
                previous = exc.raw
            logger.warning("LLM minutes attempt failed (%s); trying one repair retry", exc)
        # --- one repair retry, feeding the error back to the model ---
        try:
            repair = build_repair_messages(str(first_error), previous, title, transcript, valid)
            data = self._call_json(repair)
            result = self._validate_and_clean(data, valid)
            result.generation_source = "llm_repaired"
            result.generation_model = getattr(self.llm, "model", None)
            remaining = find_suspect_decisions(result.decisions, names)
            if remaining:
                # Repair did not move them: accept the result (the model
                # may know they are genuine conclusions) but leave a
                # trace — kept verbatim, flagged, never silently cut.
                note = (
                    "decisions 中仍有疑似待办条目（经一次 repair 复判后保留原文，"
                    "未自动删除，请人工确认）："
                    + "；".join(f"第{idx}条「{d.text}」" for idx, d, _n, _m in remaining))
                result.generation_notes.append(note)
                logger.warning("minutes quality note for %r: %s", title, note)
            return result
        except Exception as exc:
            logger.warning("LLM minutes repair failed (%s); caller falls back to rules", exc)
            raise _RepairExhausted(str(exc)) from exc

    @staticmethod
    def _validate_and_clean(data, valid: set[int]) -> MinutesResult:
        if not isinstance(data, dict):
            raise LLMError(f"LLM JSON is not an object: {type(data).__name__}")
        data = _coerce_ids_in_place(copy.deepcopy(data))
        result = MinutesResult.model_validate(data)  # may raise ValidationError
        result.decisions = _dedup_decisions(validate_evidence(result.decisions, valid))
        result.risks = validate_evidence(result.risks, valid)
        result.topics = validate_evidence(result.topics, valid) if result.topics else []
        # summary <-> executive_summary backfill
        if not result.summary and result.executive_summary:
            result.summary = result.executive_summary
        if not result.executive_summary and result.summary:
            result.executive_summary = result.summary
        if not result.detailed_minutes and result.topics:
            result.detailed_minutes = "\n\n".join(f"## {t.title}\n{t.summary}" for t in result.topics)
        return result

    # ------------------------------------------------------------ rule path
    def _analyze_rules(self, transcript: list[TranscriptSegment], title: str = "") -> MinutesResult:
        valid = {s.id for s in transcript}
        decisions: list[Decision] = []
        risks: list[Risk] = []
        open_questions: list[str] = []

        for seg in transcript:
            text = seg.text.strip()
            if any(c in text for c in DECISION_CUES):
                decisions.append(Decision(
                    text=self._decision_text(text), decided_by=seg.speaker,
                    segment_ids=[seg.id],
                ))
            if any(c in text for c in RISK_CUES):
                severity = "high" if ("过期" in text or "阻塞" in text) else "medium"
                risks.append(Risk(text=text, segment_ids=[seg.id], severity=severity))
            if any(c in text for c in QUESTION_CUES):
                open_questions.append(text)

        decisions = _dedup_decisions(validate_evidence(decisions, valid))
        risks = validate_evidence(risks, valid)
        topics = self._topics(transcript)

        executive = self._executive_summary(title, decisions, risks)
        detailed_lines = [f"## {t.title}\n{t.summary}" for t in topics]
        detailed = "\n\n".join(detailed_lines) if detailed_lines else executive
        return MinutesResult(
            summary=executive, executive_summary=executive, detailed_minutes=detailed,
            topics=topics, decisions=decisions, risks=risks,
            open_questions=list(dict.fromkeys(open_questions)),
        )

    @staticmethod
    def _decision_text(text: str) -> str:
        m = re.search(r"上线推迟到(\d{1,2}月\d{1,2}日)", text)
        if m:
            return f"上线推迟至{m.group(1)}"
        cleaned = re.sub(r"^(那就决定一下|那就|好的|好|嗯)[，,：:]?\s*", "", text)
        return cleaned.rstrip("。")

    @staticmethod
    def _topics(transcript: list[TranscriptSegment]) -> list[Topic]:
        topics: list[Topic] = []
        for name, kws in TOPIC_KEYWORDS.items():
            segs = [s for s in transcript if any(k in s.text for k in kws)]
            if segs:
                topics.append(Topic(
                    title=name, segment_ids=[s.id for s in segs],
                    summary="；".join(s.text.rstrip("。") for s in segs[:3]),
                ))
        return topics

    @staticmethod
    def _executive_summary(title: str, decisions: list[Decision], risks: list[Risk]) -> str:
        parts = [f"本次会议{('围绕' + title + '进行讨论') if title else '已召开'}。"]
        if decisions:
            parts.append("主要决策：" + "；".join(d.text for d in decisions[:3]) + "。")
        if risks:
            parts.append("主要风险：" + risks[0].text.rstrip("。") + "。")
        return "".join(parts)


class _RepairExhausted(Exception):
    """Internal signal: LLM attempt + repair both failed -> rules."""
