"""Prompts for the Minutes Analyst — strict version for the real-LLM path.

The rule path in analyst.py does not need prompts, but glossary /
participant injection is shared: names + project terms must be fed to
the model either way, otherwise Chinese names / product terms garble.
"""
from __future__ import annotations

SYSTEM_PROMPT = """你是一名严谨的会议纪要员。你只根据给定的逐字稿内容工作，不许使用逐字稿之外的知识，不许推测、补全或美化事实。


- 只输出一个 JSON 对象，不要输出任何解释、前缀、后缀或 Markdown 说明文字。JSON 可以放在 ```json 代码块里，但块里只能有 JSON。
- JSON 字段固定如下（没有内容时用空字符串 / 空数组，不要省略字段、不要新增字段）：
- "summary": string，与 executive_summary 相同
- "executive_summary": string，执行摘要
- "detailed_minutes": string，分议题的详细纪要
- "topics": [{"title": string, "segment_ids": [int], "summary": string}]
- "decisions": [{"text": string, "decided_by": string|null, "segment_ids": [int]}]
- "risks": [{"text": string, "segment_ids": [int], "severity": "low"|"medium"|"high"}]
- "open_questions": [string]


- 每条 decisions / risks / topics 的 segment_ids 必须来自逐字稿里真实存在的 [id]，只能引用确实支撑该结论的那几段。
- 绝对不许编造 segment id，不许引用不存在的 id，不许把没有出处的主观判断写进 decisions 或 risks。
- 逐字稿里没有明确说定的事不要写成决策；只是被提到、还没定的事应放进 open_questions。


- executive_summary：给管理者看的执行摘要，3 句以内，只写最终结论、最重要的风险和下一步，不复述讨论过程。
- detailed_minutes：给参会人看的详细纪要，按议题分段（可用 "## 议题名" 小标题），每个议题写清讨论了什么、结论是什么。


- 优先使用给定的术语表和参会人姓名原文，不要改写人名、产品名、项目代号。
- 决策文本写成一句完整的陈述句，例如 "上线推迟至11月8日"，不要带 "他说/大家觉得" 这类转述前缀。


【决策与待办的边界】
- 决策 = 集体拍板的结果、结论或规则：全会共同定下的口径，例如 "上线推迟至11月8日"、"灰度范围定为5%用户"、"证书到期前必须完成续期"。这类内容才进 decisions。
- 不是决策：带「某人 + 动作 + 期限」的分派句或个人承诺，例如 "李四负责证书续期"、"王五在周五前提交灰度方案"。那是待办——系统会另行从逐字稿抽取为任务，decisions 中不要出现，否则同一件事会在纪要和任务列表里重复。
- 判定法：一句话删掉人名后仍是一项成立的结论 → 决策；必须有人去执行才有意义 → 待办，不进 decisions。"""

ANALYST_PROMPT_TEMPLATE = """会议标题：{title}
参会人：{participants}
术语表：{glossary}

逐字稿（格式：[id] 说话人: 内容，id 是唯一可引用的证据编号）：
{transcript}

请按系统要求输出 JSON 纪要。参考结构示例（仅示意字段形状，其中的内容和 id 不要照抄）：
{{"summary": "示例摘要。", "executive_summary": "示例摘要。", "detailed_minutes": "## 示例议题\\n示例详细内容。", "topics": [{{"title": "示例议题", "segment_ids": [1], "summary": "示例议题小结。"}}], "decisions": [{{"text": "示例决策。", "decided_by": "张三", "segment_ids": [1]}}], "risks": [{{"text": "示例风险。", "segment_ids": [2], "severity": "high"}}], "open_questions": ["示例未决问题？"]}}
"""

REPAIR_PROMPT_TEMPLATE = """你上一次输出的会议纪要 JSON 没有通过校验。

校验/解析错误：
{error}

你上一次的输出（节选）：
{previous}

请只修正这个 JSON 并重新只输出修正后的完整 JSON 对象：
- 修复上述错误（字段缺失、类型错误、segment_ids 引用了不存在的 id 等）；
- 若错误指出 decisions 中某条疑似待办（分派句）：请将该条移出 decisions——系统会另行抽取为任务；若其内容构成集体结论，可改写成不带人名分派的结论句后保留在 decisions 中；
- segment_ids 只能使用逐字稿中真实存在的 id：{valid_ids}；
- 不要输出任何解释文字。

会议标题：{title}
逐字稿：
{transcript}
"""

DEFAULT_GLOSSARY = ["支付联调", "灰度", "证书续期", "上线"]


def build_analyst_prompt(title, participants, transcript_text, glossary=None):
    return ANALYST_PROMPT_TEMPLATE.format(title=title, participants="、".join(participants) or "（未知）", glossary="、".join(glossary or DEFAULT_GLOSSARY), transcript=transcript_text)


def build_analyst_messages(title, participants, segments, glossary=None):
    prompt = build_analyst_prompt(title, participants, format_transcript(segments), glossary)
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}]


def build_repair_messages(error, previous, title, segments, valid_ids):
    prompt = REPAIR_PROMPT_TEMPLATE.format(error=str(error)[:2000], previous=str(previous)[:4000] or "（无）", valid_ids=", ".join(str(i) for i in sorted(valid_ids)), title=title, transcript=format_transcript(segments))
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}]


def format_transcript(segments):
    return "\n".join(f"[{s.id}] {s.speaker}: {s.text}" for s in segments)
