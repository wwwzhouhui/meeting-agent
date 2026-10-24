"""Intent classification by rules. TODO(llm): small-model fallback for
ambiguous sentences; rule path must stay first (cheap + deterministic).
"""
from __future__ import annotations

INTENTS = ("schedule", "reschedule", "cancel", "query_availability", "follow_up", "unknown")


def classify_intent(text: str) -> str:
    t = text or ""
    if any(k in t for k in ("取消会议", "取消这场", "不用开了", "取消吧")):
        return "cancel"
    # NOTE: the two branches used to overlap with an operator-precedence
    # trap; merged without behaviour change: 改期/改到/换个时间 always
    # mean reschedule, 延期到/推迟到 only together with 会议.
    if "改期" in t or any(k in t for k in ("改到", "换个时间")) or (
        any(k in t for k in ("延期到", "推迟到")) and "会议" in t
    ):
        return "reschedule"
    if any(k in t for k in ("有没有空", "有空吗", "空闲", "查一下时间", "什么时候有空")):
        return "query_availability"
    if any(k in t for k in ("下次开会", "下次会议", "跟进会", "再对一次", "再开一次", "后续会议")):
        return "follow_up"
    if any(k in t for k in ("安排会议", "开个会", "开会", "约个会", "建会", "创建会议")):
        return "schedule"
    return "unknown"
