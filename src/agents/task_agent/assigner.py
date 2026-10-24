"""Owner resolution: exact name > alias > module-owner rule > human.
Never guess: unknown owner returns (None, <0.7) forcing human assignment.
"""
from __future__ import annotations

from src.shared.schema import ActionItem, Participant

ALIASES: dict[str, str] = {
    "老张": "张三", "张总": "张三",
    "小李": "李四", "李工": "李四",
    "小王": "王五", "王工": "王五",
    "产品那边": "王五", "产品": "王五",
}

# keyword in task title -> owner name (module ownership table, edit per team)
MODULE_OWNERS: dict[str, str] = {
    "证书": "李四", "支付": "李四", "联调": "李四",
    "灰度": "王五", "方案": "王五",
}


class Assigner:
    def resolve_owner(self, owner_raw: str, participants: list[Participant],
                      title: str = "") -> tuple[str | None, float]:
        by_name = {p.name: p.user_id for p in participants}
        raw = (owner_raw or "").strip()
        if raw in by_name:
            return by_name[raw], 0.95
        if raw in ALIASES and ALIASES[raw] in by_name:
            return by_name[ALIASES[raw]], 0.85
        for kw, owner_name in MODULE_OWNERS.items():
            if kw in (title or "") and owner_name in by_name:
                return by_name[owner_name], 0.75
        return None, 0.3

    def assign(self, items: list[ActionItem], participants: list[Participant]) -> list[ActionItem]:
        name_by_id = {p.user_id: p.name for p in participants}
        for item in items:
            owner_id, conf = self.resolve_owner(item.owner_raw, participants, item.title)
            item.owner_id = owner_id
            item.owner_confidence = conf
            item.assignment_source = "auto" if owner_id else None
            if owner_id and conf >= 0.7:
                # normalise owner_raw to the canonical name for display
                item.owner_raw = name_by_id.get(owner_id, item.owner_raw)
        return items
