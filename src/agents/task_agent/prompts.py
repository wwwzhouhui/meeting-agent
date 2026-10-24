"""Prompts for the Task Extractor — strict version for the real-LLM path.

Style mirrors src/agents/minutes/prompts.py. Division of labour (red
lines, enforced again in extractor.py):

- the LLM only EXTRACTS: title / owner_raw / due_raw / priority /
source_segment_ids;
- it must NOT output owner_id (owner resolution is the Assigner's job)
and must NOT convert due_raw into a date (date_parser computes it).
"""
from __future__ import annotations

from datetime import date

from src.agents.minutes.prompts import format_transcript

SYSTEM_PROMPT = """你是一名严谨的会议任务抽取员。你只根据给定的逐字稿和纪要内容工作，不许使用逐字稿之外的知识，不许推测或补全事实。


- 只输出一个 JSON 对象，不要输出任何解释、前缀、后缀或 Markdown 说明文字。JSON 可以放在 ```json 代码块里，但块里只能有 JSON。
- JSON 字段固定如下（不要省略顶层字段、不要新增字段）：
- "action_items": [{"title": string, "owner_raw": string, "due_raw": string|null, "priority": "P0"|"P1"|"P2", "source_segment_ids": [int]}]


- "title"：任务标题，写成动作短语（如「完成支付渠道证书续期并提交联调」「提交灰度方案供评审」）。标题里不要带负责人姓名，不要带日期词（如「明天」「下周三之前」），不要带「负责」「你来」这类分派词。
- "owner_raw"：负责人称呼，写逐字稿里出现的原文称呼（全名如「李四」或别名如「小李」「产品那边」都可以），不要改写、不要翻译、不要输出系统 ID。确实无法判断负责人时写空字符串 ""。
- "due_raw"：截止时间的原文表述（如「下周三之前」「明天」「11月1日前」），逐字稿里没有提到截止时间时写 null。只抄原文，不要换算成具体日期。
- "priority"：只能是 "P0" / "P1" / "P2" 三者之一。涉及过期、阻塞、上线等紧急事项用 "P1"，普通事项用 "P2"，只有逐字稿明确说「最高优先级/立刻」时才用 "P0"。
- "source_segment_ids"：支撑这条任务的逐字稿段落 id 数组。
- 不要输出 "owner_id"，不要输出 "due_date"——这两项由系统代码计算，你输出了也会被丢弃。


- 每条任务的 source_segment_ids 必须来自逐字稿里真实存在的 [id]，只能引用确实支撑该任务的那几段。
- 绝对不许编造 segment id，不许引用不存在的 id；没有出处支撑的事不要抽成任务。


- 高召回优先：逐字稿里凡是有人明确承诺要做、或被明确分派要做的事，都要抽成任务，宁可多抽一条也不要漏抽。
- 同一件事被重复提到（分派一次、确认一次）只抽一条，source_segment_ids 合并所有相关段落。
- 只是讨论、决策或闲聊、没有落到具体某人头上要做的事，不要抽成任务。
- 排除会议安排：召开、组织、安排下一次会议、跟进会、评审会、例会等「会议安排类事项」属于日程 Agent 的职责，即使会上已经定下时间与参会人，也不要抽成 action_item。但跟进会要讨论的实质交付物（如续期证书、提交联调报告、准备评审材料）照常按高召回抽取——排除的只是「开会」这个动作本身。"""

EXTRACTOR_PROMPT_TEMPLATE = """会议标题：{title}
会议日期：{base_date}（{weekday}）
参会人：{participants}

纪要摘要（供理解上下文，抽取仍以逐字稿为准）：
{summary}

纪要中的决策：
{decisions}
（注意：决策列表仅供参考，其中若混有分派句，以逐字稿为准照常抽取，不要因此漏抽。）

逐字稿（格式：[id] 说话人: 内容，id 是唯一可引用的证据编号）：
{transcript}

请按系统要求输出 JSON 任务清单。参考结构示例（仅示意字段形状，其中的内容和 id 不要照抄）：
{{"action_items": [{{"title": "完成示例模块联调并提交报告", "owner_raw": "李四", "due_raw": "下周三之前", "priority": "P1", "source_segment_ids": [5, 6]}}]}}
"""

REPAIR_PROMPT_TEMPLATE = """你上一次输出的任务清单 JSON 没有通过校验。

校验/解析错误：
{error}

你上一次的输出（节选）：
{previous}

请只修正这个 JSON 并重新只输出修正后的完整 JSON 对象：
- 修复上述错误（字段缺失、类型错误、source_segment_ids 引用了不存在的 id 等）；
- source_segment_ids 只能使用逐字稿中真实存在的 id：{valid_ids}；
- 不要输出 owner_id，不要输出 due_date；
- 不要输出任何解释文字。

会议标题：{title}
逐字稿：
{transcript}
"""

WEEKDAYS = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")


def _weekday_cn(d: date) -> str:
    return WEEKDAYS[d.weekday()]


def build_extractor_prompt(title, base_date, participants, segments,
                           summary="", decisions=None):
    if isinstance(base_date, date):
        base_str = base_date.isoformat()
        weekday = _weekday_cn(base_date)
    else:
        base_str = str(base_date or "")
        weekday = ""
    decision_lines = []
    for d in decisions or []:
        text = getattr(d, "text", None) or (d.get("text") if isinstance(d, dict) else None)
        if text:
            decision_lines.append(f"- {text}")
    return EXTRACTOR_PROMPT_TEMPLATE.format(
        title=title,
        base_date=base_str,
        weekday=weekday,
        participants="、".join(participants) or "（未知）",
        summary=(summary or "（无）").strip(),
        decisions="\n".join(decision_lines) or "（无）",
        transcript=format_transcript(segments),
    )


def build_extractor_messages(title, base_date, participants, segments,
                             summary="", decisions=None):
    prompt = build_extractor_prompt(
        title, base_date, participants, segments,
        summary=summary, decisions=decisions,
    )
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}]


def build_repair_messages(error, previous, title, segments, valid_ids):
    prompt = REPAIR_PROMPT_TEMPLATE.format(
        error=str(error)[:2000],
        previous=str(previous)[:4000] or "（无）",
        valid_ids=", ".join(str(i) for i in sorted(valid_ids)),
        title=title,
        transcript=format_transcript(segments),
    )
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}]
