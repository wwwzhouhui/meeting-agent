# 确认卡交互稿（两次确认，方案第 3 节）

原则：纪要确认（Gate1）和任务确认（Gate2）是两张分开的卡。
纪要没确认，绝不发任务卡；任何卡上都要有「修改 / 改派 / 删除」入口，
不能只有「确认」一个按钮。

## 按钮 → 后端端点对照（已实现）

| 卡片按钮 | 后端端点 | 说明 |
|---|---|---|
| 确认纪要 | `POST /meetings/{id}/confirm-minutes` | 仅 `awaiting_minutes_confirmation`；通过后自动抽任务并发任务卡 |
| 驳回重跑（纪要） | `POST /meetings/{id}/reject-minutes`，body `{"reason": "..."}` | 仅 `awaiting_minutes_confirmation`，否则 409；状态变 `minutes_rejected`，驳回记录进 `meeting.rejections`；之后 `POST /meetings/{id}/process` 重新生成纪要再确认 |
| 确认并分发（任务） | `POST /meetings/{id}/confirm-tasks` | 仅 `awaiting_tasks_confirmation`；通过后提日程候选并分发 |
| 驳回（任务） | `POST /meetings/{id}/reject-tasks`，body `{"reason": "..."}` | 仅 `awaiting_tasks_confirmation`，否则 409；状态变 `tasks_rejected`；之后 `POST /meetings/{id}/reextract-tasks` 基于已确认纪要重新抽取，再回到待确认 |
| 改派 / 改截止 / 改优先级 | `PATCH /meetings/{id}/tasks/{item_id}`，body 可含 `owner_id` / `due_date` / `priority` | 仅 `awaiting_tasks_confirmation`，否则 409；item 不存在 404；非法日期/优先级 422；改派后 `assignment_source="manual"`，分发以改后值为准 |

卡片回调入口 `/webhooks/*` 本身仍未实现（统一 501），IM 平台的回调
需要先落到上面这些端点上（见各卡下方的 TODO 说明）。

## Gate1 纪要确认卡

内容：执行摘要 + 决策列表（每条带出处片段）+ 风险列表。
操作：`确认纪要` / `编辑纪要` / `驳回重跑`。

### 飞书交互卡片（节选）

```json
{
  "config": {"wide_screen_mode": true},
  "header": {"title": {"tag": "plain_text", "content": "纪要待确认：支付联调与上线时间评审会"}, "template": "blue"},
  "elements": [
    {"tag": "markdown", "content": "**执行摘要**\n本次会议决定上线推迟至11月8日……"},
    {"tag": "markdown", "content": "**决策**\n1. 上线推迟至11月8日（出处 #3）\n**风险**\n- 支付渠道证书11月1日过期（出处 #2）"},
    {"tag": "action", "actions": [
      {"tag": "button", "text": {"tag": "plain_text", "content": "确认纪要"}, "type": "primary", "value": {"action": "confirm_minutes", "meeting_id": "m-20261023-001"}},
      {"tag": "button", "text": {"tag": "plain_text", "content": "编辑"}, "value": {"action": "edit_minutes", "meeting_id": "m-20261023-001"}}
    ]}
  ]
}
```

飞书卡片回调处理：`POST /webhooks/...`（TODO，见 src/api/webhooks.py 说明，
目前统一返回 501），收到 `confirm_minutes` 后调用
`POST /meetings/{id}/confirm-minutes`；「驳回重跑」按钮对应
`POST /meetings/{id}/reject-minutes`（body 带 reason），驳回后由调用方
再触发 `POST /meetings/{id}/process` 重跑。

## Gate2 任务确认卡

内容：任务清单逐条展示（标题 / 负责人+置信度 / 截止 / 出处），
低置信度负责人标黄并要求改派；确认后才真正建外部任务并发通知。

### Slack Block Kit（节选）

```json
{
  "blocks": [
    {"type": "header", "text": {"type": "plain_text", "text": "任务待确认（3 条）"}},
    {"type": "section", "text": {"type": "mrkdwn", "text": "*[P1] 支付渠道证书续期并提交联调*\n负责人：李四（置信度 0.95） · 截止：2026-10-28 · 出处 #5"}},
    {"type": "actions", "elements": [
      {"type": "button", "text": {"type": "plain_text", "text": "确认并分发"}, "action_id": "confirm_tasks", "value": "m-20261023-001"},
      {"type": "button", "text": {"type": "plain_text", "text": "改派"}, "action_id": "reassign", "value": "m-20261023-001"}
    ]}
  ]
}
```

Slack 回调处理 TODO：校验 signing secret 后调用
`POST /meetings/{id}/confirm-tasks`；「改派」按钮对应
`PATCH /meetings/{id}/tasks/{item_id}`（在卡片上收集新负责人后回填
`owner_id`），任务驳回对应 `POST /meetings/{id}/reject-tasks`。
