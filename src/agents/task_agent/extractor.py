"""ActionItem extraction: LLM primary path, rules fallback.

Primary path (real LLM, when configured):
    prompt -> LLMClient.chat_json -> per-item validate & clean
    -> dedup (merging evidence). On call/parse/validation failure,
    ONE repair retry (the error is fed back to the model); if that
    also fails, fall back to rules. Never raises.
Fallback path (rules, ``_extract_rules``): assignment cues + owner +
due date by regex. Used with generation_source="rules" when no LLM
is configured, or "rule_fallback" when an LLM was tried and failed.

Red lines executed here (mirroring the minutes analyst):
- The LLM only EXTRACTS title / owner_raw / due_raw / priority /
  source_segment_ids. Any owner_id it returns is discarded — owner
  resolution is exclusively the Assigner's job.
- due_date is NOT trusted from the LLM: date_parser.parse_due(due_raw,
  base_date) wins; the LLM's due_date is only a fallback when the
  parser cannot parse due_raw at all.
- Items whose source_segment_ids are not all real transcript ids are
  dropped; numeric-string ids are coerced to int first; a priority
  outside P0/P1/P2 falls back to the rule priority for that text.
- id / idempotency_key are generated exactly like the rule path
  (task-{meeting.id}-{first segment id} / {meeting.id}:{first id}),
  so switching paths never double-creates tasks downstream; when two
  different items cite the same first segment a run-scoped "-2"/"-3"
  suffix disambiguates (a shared id once made dispatch silently reuse
  the first item's external task for the second).
- Meeting-arrangement items ("召开跟进会…") belong to the Calendar
  Agent, not the task list: a deterministic safety net
  (filter_scheduling_items) drops them on EVERY path (LLM, repaired,
  rules, rule_fallback) and records each drop in extraction_notes —
  never silently.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date

from src.agents.calendar_agent.date_parser import parse_due
from src.shared.llm import LLMClient, LLMError, extract_json
from src.shared.schema import ActionItem, Meeting

from .prompts import build_extractor_messages, build_repair_messages

logger = logging.getLogger(__name__)

ASSIGN_CUES = ("负责", "我来", "你来", "跟进", "你跟一下")
CORE_NOUNS = ("证书续期", "灰度方案", "联调测试", "联调", "证书", "灰度", "报告", "方案")
VALID_PRIORITIES = ("P0", "P1", "P2")

# --- scheduling-item safety net ---------------------------------------
# Field report (2026-10-23, live Ark run): extraction produced a 4th
# item "召开进度跟进会确认证书和联调结果" — convening the follow-up
# meeting is the Calendar Agent's job, not a task. The LLM prompt now
# excludes such items, and this regex is the deterministic backstop.
# It is deliberately conservative — a convening verb must sit within
# 4 chars before a meeting noun — so deliverables ABOUT meetings are
# never caught:
#   "准备评审会演示材料" (verb is 准备, not a convening verb)  -> keep
#   "提交会议纪要"       (no convening verb)                  -> keep
#   "预订会议室"         (no convening verb; 会议(?!室))      -> keep
#   "安排联调环境"       (安排 + non-meeting object)         -> keep
#   "组织联调测试"       (组织 + 测试, not a meeting)         -> keep
_SCHEDULING_RE = re.compile(
    r"(?:召开|组织|主持|安排)[^，。；、：:]{0,4}"
    r"(?:跟进会|评审会|例会|同步会|启动会|碰头会|会议(?!室))")


def is_scheduling_title(title: str) -> bool:
    """True when a task title is really a meeting-arrangement item."""
    return bool(_SCHEDULING_RE.search(title or ""))


def filter_scheduling_items(items: list[ActionItem]) -> tuple[list[ActionItem], list[ActionItem]]:
    """Split items into (kept, dropped-scheduling). Pure; no mutation."""
    kept: list[ActionItem] = []
    dropped: list[ActionItem] = []
    for it in items:
        (dropped if is_scheduling_title(it.title) else kept).append(it)
    return kept, dropped


def _coerce_ids(value) -> list[int]:
    """Numeric strings / floats in an id list become ints; junk removed."""
    if isinstance(value, (int, str)):
        value = [value]
    if not isinstance(value, list):
        return []
    out: list[int] = []
    for v in value:
        if isinstance(v, bool):
            continue
        if isinstance(v, int):
            out.append(v)
        elif isinstance(v, str) and v.strip().lstrip("-").isdigit():
            out.append(int(v.strip()))
        elif isinstance(v, float) and v.is_integer():
            out.append(int(v))
    return out


class ActionItemExtractor:
    def __init__(self, llm: LLMClient | None = None):
        self.llm = llm or LLMClient()
        # Provenance of the most recent extract() call (read by TaskAgent).
        self.generation_source: str | None = None
        self.generation_model: str | None = None
        # Human-visible notes of the most recent extract() call — the
        # scheduling safety net records every drop here (never silent).
        self.extraction_notes: list[str] = []

    # ------------------------------------------------------------------ API
    def extract(self, meeting: Meeting) -> list[ActionItem]:
        self.extraction_notes = []
        if self._llm_available():
            try:
                items, source = self._extract_llm(meeting)
                self.generation_source = source
                self.generation_model = getattr(self.llm, "model", None)
                for it in items:
                    it.generation_source = source
                return self._finish(items)
            except _RepairExhausted:
                pass  # _extract_llm already logged; fall through to rules
            except Exception as exc:  # defensive: LLM path must never crash callers
                logger.warning("LLM task-extraction path failed unexpectedly: %s", exc)
            items = self._extract_rules(meeting)
            self.generation_source = "rule_fallback"
            self.generation_model = getattr(self.llm, "model", None)
            for it in items:
                it.generation_source = "rule_fallback"
            return self._finish(items)
        items = self._extract_rules(meeting)
        self.generation_source = "rules"
        self.generation_model = None
        for it in items:
            it.generation_source = "rules"
        return self._finish(items)

    def _finish(self, items: list[ActionItem]) -> list[ActionItem]:
        """Scheduling safety net, applied to every extraction path."""
        kept, dropped = filter_scheduling_items(items)
        for it in dropped:
            note = f"已过滤会议安排类条目（属日程 Agent 职责，不进任务列表）：「{it.title}」"
            self.extraction_notes.append(note)
            logger.info("extraction safety net dropped %r: %s", it.id, note)
        return kept

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

    def _extract_llm(self, meeting: Meeting) -> tuple[list[ActionItem], str]:
        valid = meeting.valid_segment_ids()
        messages = build_extractor_messages(
            meeting.title,
            meeting.base_date(),
            [p.name for p in meeting.participants],
            meeting.transcript,
            summary=meeting.executive_summary or meeting.summary or "",
            decisions=meeting.decisions,
        )
        previous = ""
        try:
            data = self._call_json(messages)
            previous = json.dumps(data, ensure_ascii=False) if isinstance(data, (dict, list)) else str(data)
            return self._validate_and_clean(data, meeting, valid), "llm"
        except Exception as exc:
            first_error = exc
            if isinstance(exc, LLMError) and exc.raw:
                previous = exc.raw
            logger.warning("LLM task extraction failed (%s); trying one repair retry", exc)
        # --- one repair retry, feeding the error back to the model ---
        try:
            repair = build_repair_messages(
                str(first_error), previous, meeting.title, meeting.transcript, valid)
            data = self._call_json(repair)
            return self._validate_and_clean(data, meeting, valid), "llm_repaired"
        except Exception as exc:
            logger.warning("LLM task-extraction repair failed (%s); caller falls back to rules", exc)
            raise _RepairExhausted(str(exc)) from exc

    def _validate_and_clean(self, data, meeting: Meeting,
                            valid: set[int]) -> list[ActionItem]:
        if isinstance(data, list):  # tolerate a bare list of items
            raw_items = data
        elif isinstance(data, dict):
            raw_items = None
            for key in ("action_items", "tasks", "items"):
                if isinstance(data.get(key), list):
                    raw_items = data[key]
                    break
            if raw_items is None:
                raise LLMError("LLM JSON has no action_items list")
        else:
            raise LLMError(f"LLM JSON is not an object: {type(data).__name__}")

        base = meeting.base_date()
        seg_text = {s.id: s.text for s in meeting.transcript}
        items: list[ActionItem] = []
        used_ids: set[str] = set()
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            # --- evidence: coerce, then ALL ids must be real, else drop ---
            ids = _coerce_ids(raw.get("source_segment_ids", raw.get("segment_ids")))
            ids = list(dict.fromkeys(ids))  # dedup, keep order
            if not ids or not all(i in valid for i in ids):
                continue
            title = str(raw.get("title") or "").strip().strip("。")
            if len(title) < 2:
                continue
            owner_raw = str(raw.get("owner_raw") or "").strip()
            # --- due: parser is the source of truth; LLM due_date only
            #     as a fallback when the parser cannot parse due_raw ---
            due_raw = raw.get("due_raw")
            due_raw = str(due_raw).strip() if due_raw else None
            due_date = None
            if due_raw:
                due_date, _ = parse_due(due_raw, base)
            if not due_date:
                llm_due = raw.get("due_date")
                if isinstance(llm_due, str):
                    try:
                        due_date = date.fromisoformat(llm_due.strip()).isoformat()
                    except ValueError:
                        due_date = None
            # --- priority: P0/P1/P2 only, else rule priority ---
            priority = str(raw.get("priority") or "").strip().upper()
            if priority not in VALID_PRIORITIES:
                context = title + " " + " ".join(seg_text.get(i, "") for i in ids)
                priority = self._priority(context)
            first = ids[0]
            # Two different tasks may cite the same FIRST segment; a bare
            # meeting+segment id would collide — same id AND same
            # idempotency key made dispatch silently reuse the first
            # task's result for the second one — so a run-scoped -2/-3
            # suffix disambiguates while the first item keeps the stable
            # rule-path shape (switching paths never double-creates).
            item_id = f"task-{meeting.id}-{first}"
            occurrence = 1
            while item_id in used_ids:
                occurrence += 1
                item_id = f"task-{meeting.id}-{first}-{occurrence}"
            used_ids.add(item_id)
            item = ActionItem(
                # NOTE: raw.get("owner_id") is deliberately never read —
                # the Assigner is the only owner-resolution authority.
                id=item_id,
                title=title[:60], owner_raw=owner_raw, owner_confidence=0.0,
                due_date=due_date, due_raw=due_raw,
                priority=priority,
                source_segment_ids=ids,
                idempotency_key=(f"{meeting.id}:{first}" if occurrence == 1
                                 else f"{meeting.id}:{first}:{occurrence}"),
            )
            if self._merge_if_duplicate(item, items):
                continue
            items.append(item)
        if not items:
            raise LLMError("LLM returned no valid action items after evidence filtering")
        return items

    def _merge_if_duplicate(self, new: ActionItem, existing: list[ActionItem]) -> bool:
        """True when `new` was merged into an existing item (drop `new`).

        Exact same title + owner merges first (covers LLM repeats);
        otherwise the rule-path noun-overlap dedup decides.
        """
        for old in existing:
            if old.title == new.title and old.owner_raw == new.owner_raw:
                for sid in new.source_segment_ids:
                    if sid not in old.source_segment_ids:
                        old.source_segment_ids.append(sid)
                return True
        return self._is_duplicate(new, existing)

    # ------------------------------------------------------------ rule path
    def _extract_rules(self, meeting: Meeting) -> list[ActionItem]:
        names = [p.name for p in meeting.participants]
        base = meeting.base_date()
        items: list[ActionItem] = []
        for seg in meeting.transcript:
            text = re.sub(r"^(好的|好|嗯)[，,]\s*", "", seg.text.strip())
            owner_raw = self._detect_owner(text, seg.speaker, names)
            if not owner_raw:
                continue
            if not self._has_action_cue(text, seg.speaker, owner_raw):
                continue
            due_date, due_raw = parse_due(text, base)
            title = self._make_title(text, owner_raw)
            if len(title) < 4:
                continue
            item = ActionItem(
                id=f"task-{meeting.id}-{seg.id}",
                title=title, owner_raw=owner_raw, owner_confidence=0.0,
                due_date=due_date, due_raw=due_raw,
                priority=self._priority(text),
                source_segment_ids=[seg.id],
                idempotency_key=f"{meeting.id}:{seg.id}",
            )
            if self._is_duplicate(item, items):
                continue
            items.append(item)
        return items

    # ------------------------------------------------------- rule helpers
    @staticmethod
    def _detect_owner(text: str, speaker: str, names: list[str]) -> str | None:
        for name in names:
            if text.startswith(name) or f"{name}负责" in text or f"{name}，" in text or f"{name}你" in text:
                return name
        if text.startswith("我") or "我来" in text or "我负责" in text:
            return speaker
        return None

    @staticmethod
    def _has_action_cue(text: str, speaker: str, owner_raw: str) -> bool:
        if any(c in text for c in ASSIGN_CUES):
            return True
        # self-commitment: speaker == owner and a delivery verb + due/object
        if speaker == owner_raw and text.startswith("我") and any(
            v in text for v in ("完成", "提交", "负责", "跟进")
        ):
            return True
        return False

    @staticmethod
    def _make_title(text: str, owner_raw: str) -> str:
        t = text
        t = re.sub(r"^(好的|好|嗯)[，,]\s*", "", t)
        if t.startswith("我"):
            t = t[1:]
        t = t.replace(f"{owner_raw}，", "").replace(f"{owner_raw}", "")
        t = t.replace("你来负责", "").replace("我来负责", "").replace("来负责", "")
        t = t.replace("负责", "").replace("你来", "").replace("我来", "")
        # drop due-date phrases from the title body
        from src.agents.calendar_agent.date_parser import extract_due_raw

        raw = extract_due_raw(t)
        if raw:
            t = t.replace(raw, "")
        t = re.sub(r"(之前|以前|前)(完成|提交)", r"\2", t)
        t = t.strip(" ，,。")
        t = re.sub(r"\s+", "", t)
        return t[:60].strip("，,。") or text[:60]

    @staticmethod
    def _priority(text: str) -> str:
        if any(k in text for k in ("紧急", "尽快", "过期", "阻塞")):
            return "P1"
        if "证书" in text:
            return "P1"
        return "P2"

    @staticmethod
    def _is_duplicate(new: ActionItem, existing: list[ActionItem]) -> bool:
        new_nouns = {n for n in CORE_NOUNS if n in new.title}
        for old in existing:
            if old.owner_raw != new.owner_raw:
                continue
            old_nouns = {n for n in CORE_NOUNS if n in old.title}
            if new_nouns and old_nouns and (new_nouns & old_nouns):
                if old.due_date == new.due_date or not new.due_date or not old.due_date:
                    # merge evidence into the kept item
                    for sid in new.source_segment_ids:
                        if sid not in old.source_segment_ids:
                            old.source_segment_ids.append(sid)
                    return True
        return False


class _RepairExhausted(Exception):
    """Internal signal: LLM attempt + repair both failed -> rules."""
