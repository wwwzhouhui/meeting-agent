"""Relative-date parsing by RULES (LLM only extracts, code computes).

Supported: 今天/明天/后天, N天后, 周X / 本周X / 下周X (含 之前/前 后缀),
M月D日, 月底. Base date defaults to today; callers pass the meeting date.
"""
from __future__ import annotations

import re
from datetime import date, timedelta

WEEKDAY = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}

_DUE_RAW_RE = re.compile(
    r"(?:\d{1,2}月\d{1,2}[日号](?:之前|以前|前)?"
    r"|下周[一二三四五六日天](?:之前|以前|前)?"
    r"|本周[一二三四五六日天](?:之前|以前|前)?"
    r"|这周[一二三四五六日天](?:之前|以前|前)?"
    r"|周[一二三四五六日天](?:之前|以前|前)?"
    r"|明天|后天|今天|月底|\d+天后)"
)


def extract_due_raw(text: str) -> str | None:
    m = _DUE_RAW_RE.search(text or "")
    return m.group(0) if m else None


def _next_week_date(base: date, target_wd: int) -> date:
    """Date of weekday `target_wd` in the NEXT calendar week (Mon-Sun)."""
    monday_next = base + timedelta(days=(7 - base.weekday()))
    return monday_next + timedelta(days=target_wd)


def _this_week_date(base: date, target_wd: int) -> date:
    delta = (target_wd - base.weekday()) % 7
    return base + timedelta(days=delta)


def parse_relative_date(text: str, base_date: date | None = None) -> date | None:
    if not text:
        return None
    base = base_date or date.today()
    text = text.strip()

    m = re.search(r"(\d{1,2})月(\d{1,2})[日号]", text)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        try:
            d = date(base.year, month, day)
        except ValueError:
            return None
        if d < base:  # already passed this year -> next year
            d = date(base.year + 1, month, day)
        return d

    m = re.search(r"下周([一二三四五六日天])", text)
    if m:
        return _next_week_date(base, WEEKDAY[m.group(1)])

    m = re.search(r"(?:本周|这周|周)([一二三四五六日天])", text)
    if m:
        return _this_week_date(base, WEEKDAY[m.group(1)])

    if "明天" in text:
        return base + timedelta(days=1)
    if "后天" in text:
        return base + timedelta(days=2)
    if "今天" in text:
        return base
    m = re.search(r"(\d+)天后", text)
    if m:
        return base + timedelta(days=int(m.group(1)))
    if "月底" in text:
        if base.month == 12:
            return date(base.year, 12, 31)
        return date(base.year, base.month + 1, 1) - timedelta(days=1)
    return None


def parse_due(text: str, base_date: date | None = None) -> tuple[str | None, str | None]:
    """Return (due_date ISO str, due_raw)."""
    raw = extract_due_raw(text)
    if not raw:
        return None, None
    d = parse_relative_date(raw, base_date)
    return (d.isoformat() if d else None), raw
