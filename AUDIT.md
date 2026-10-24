# 完整性审计记录（2026-10-23）

标准：新开发者按 README 操作，每条路径不许崩、不许被文档骗。
对照物：落地方案第 5 节结构 + README 全部承诺。
严重程度：**崩溃**（路径会抛错/500）/ **误导**（行为与文档不符或静默假装成功）/
**缺失**（承诺或结构里有、代码里没有）/ **小瑕**（不影响运行的卫生问题）。

验证基线：审计前 pytest 32 passed；审计后 **63 passed**（新增 31 个测试：
`tests/test_api.py`、`tests/test_calendar_agent.py`、`tests/test_dispatch_resilience.py`）。

---

## 一、初扫疑点逐项核实

### 1. Dockerfile 漏装 httpx — 属实，已修复
- 发现：`Dockerfile` 的 pip 清单只有 pydantic/fastapi/uvicorn/python-dotenv，
与 `pyproject.toml` 的 dependencies 不一致；镜像内 LLM 客户端（httpx 直调）
和 FastAPI TestClient 都会因缺 httpx 坏掉（本地有 httpx 所以一直没暴露）。
- 修复：Dockerfile 补齐 httpx，并加注释要求与 pyproject 保持一致；
CMD/端口核对无误（uvicorn 8000，与 compose 的 8000:8000 一致）。

### 2. docker-compose 的 env_file:.env 不存在时直接报错 — 属实，已修复
- 发现：`env_file:.env` 短语法在.env 缺失时 `docker compose up` 直接失败，
而 README 又把 Docker 写成可选步骤，新人第一步就卡住。
- 修复：改为 Compose v2.24+ 长语法 `path:.env + required: false`；
README Docker 一节同步改为「.env 可选，没有也能起」。
- 本机无 docker，未能实跑 `docker compose config`；已用 PyYAML 静态解析
核对结构（services/env_file/command/ports 均正确），实跑验证留待有 docker 的环境。

### 3. 配置了 provider 后 create_task 抛 NotImplementedError 会打崩流程 — 属实且更深，已修复
- 发现（两层）：
a. `TaskTracker.dispatch()` 对 `create_task` 没有任何异常隔离，工具一抛错，
异常穿透 TaskAgent → flow → API，整场会议 500。
b. 更深一层：`TASK_PROVIDER` / `CALENDAR_PROVIDER` 此前**只读进 config、
从未接线**——全仓没有任何地方按 provider 构造工具，Tracker/Scheduler
永远用内置 mock。也就是说用户按 README 配了 provider 也毫无效果（静默无效）。
- 修复：
- 新增 `src/tools/factory.py`：按 Settings 构造 task/calendar/notify 工具
（mock/未知 → None，保持原 mock 行为；已知 provider → 对应 stub 工具）。
- `TaskAgent` / `CalendarAgent` 默认即通过 factory 拿工具，env 配置真正生效。
- `Tracker.dispatch()` 改为逐条隔离：单条失败 → 该条 `status="dispatch_failed"`
+ 新增字段 `dispatch_error` 记录原因，其余任务继续；工具返回无 id 也算失败。
- `flow.step_dispatch()` 据此把整场状态置为 `dispatched` /
`dispatched_with_failures` / `dispatch_failed` 并写 `meeting.warnings`。
- `Scheduler.create_follow_up()` 同步加隔离：失败不抛，`calendar_event_id`
保持 None 并 append warning。
- 测试锁定：`tests/test_dispatch_resilience.py`（含 env 接线后整场不崩的用例）。

### 4. 方案里的 tools/calendar/outlook.py 缺失 — 属实，已补齐
- 修复：新增 `src/tools/calendar/outlook.py`，与 google/feishu 同模式的
Microsoft Graph stub（未配置 → mock+warning；配置了 → NotImplementedError
并注明 Graph 的 events/getSchedule 路径）；factory 已支持
`CALENDAR_PROVIDER=outlook`；config 新增 `OUTLOOK_ACCESS_TOKEN`，
`.env.example` 同步补行。

### 5. 只有 audio_url 的会议静默产出空纪要 — 属实，已修复
- 发现：Transcriber 对 audio_url 返回 []（仅日志），MinutesAgent 照常调
Analyst 生成一份空 MinutesResult，状态还写 `minutes_draft`，下游会
抽出 0 条任务并"成功"分发——看起来像开了一场没结论的会，实际是没转写。
- 修复：MinutesAgent 增加空逐字稿契约——不调用 Analyst，
`status="needs_transcript"`、`minutes=None`、`meeting.warnings` 写明原因
（新增 Meeting.warnings 字段）；flow 在该状态直接停住，不进闸门/任务；
API 补传逐字稿后状态回到 `scheduled` 可重跑。顺带把 `ASR_ENGINE` 真正
接进 Transcriber（此前 config 里有、构造时被写死 "mock"）。
- 测试锁定：resilience 文件的 needs_transcript 两条 + test_api 的补传重跑全程。

### 6. webhooks 只有占位却返回 200 — 属实，已修复
- 发现：三个 webhook 此前返回 `200 {"ok": true, "mock": true}`，调用方
（如飞书卡片回调）会以为处理成功；且畸形 JSON 会让 `request.json()` 抛错变 500。
- 修复：全部改为 **HTTP 501** + `{"ok": false, "error": "not_implemented",
"webhook":..., "detail":...}`，明确写清"没有处理任何东西"；
请求体改成防御性解析，畸形 JSON 也稳定返回 501。
- 测试锁定：test_api.test_webhooks_are_explicitly_not_implemented（含畸形体）。

---

## 二、系统性自查新发现

### 7. API 没有状态机保护 — 已修复
- 发现：未 process 就 confirm-minutes 会凭空抽任务并置 awaiting_tasks；
confirm-tasks 可以在 gate1 之前调用并直接"分发"（0 条任务也 dispatched）；
重复 confirm-minutes 会重新抽任务、冲掉已分发状态；重复建会静默覆盖；
已分发的会议可被 process 重跑（真实工具下有重复建任务风险）。
- 修复（`src/api/main.py`）：重复建会 → 409；confirm 顺序/前置不满足 → 409；
重复 confirm → 幂等 200 no-op；已分发后 process/transcript → 409。
- 测试锁定：test_api 全套（happy path + 409/404/422 + 幂等）。

### 8. 日程 Agent：prepare_brief 未接线 — 明确保留为待办
- 核实：`CalendarAgent.prepare_brief()` 函数本身真实实现（上次决策 + 未完成
任务结转 + 建议议程），且有测试覆盖；但 flow 和 API 都没有调用点，
属于方案 Phase 2（会前 Brief）。README 路线图本来就把它列在 Phase 2，
本次在 README「如何切真实服务」后补了一段未接线清单明确点名，不改代码行为。

### 9. Tracker 的 overdue/reminder/weekly 已实现但无人调用 — 明确保留为待办
- 核实：`check_overdue / reminders_due / weekly_summary` 逻辑真实
（到期判定、T-1 提醒、周汇总，本次已补行为测试锁定，见
test_dispatch_resilience.py 的 tracker 用例）；真正缺的是 APScheduler
定时驱动（tracker 文件头已标 Phase 2 TODO），全仓没有任何调用点。
保留待办，理由：没有调度进程时硬接调用点也是死代码，先保证被调用路径全绿。

### 10. config 里定义了却没人用的配置项 — 已分别处置
- `LLM_SMALL_MODEL`：从未被读取 → 在 config、.env.example、README 三处
明确标注「预留，尚未接线，填了不生效」。
- `NOTIFY_PROVIDER`：notify 整步未进 flow → config 标 RESERVED，
factory 提供唯一构造点 `build_notify_tool()` 备用。
- `DATABASE_URL`：db 的 Postgres 实现待办 → config 标 RESERVED 注释。
- `REDIS_URL`：.env.example 有、config 没有 → 已给 Settings 补 `redis_url`
字段（RESERVED，保持两边对齐）。
- `ASR_ENGINE` / `*_PROVIDER`：此前没人用 → 已接线（见 #3、#5）。
- `.env.example` 与 config 字段逐项对过：现在双向都有对应（OUTLOOK 也已补齐）。

### 11. db.py 接口一致性 — 核实无误，保留
- `save/get/list_all/delete` 接口与 db 文件头承诺一致；API 只用 save/get，
list_all/delete 是为 Postgres 替换预留的接口面（文件头已说明），不是死代码。

### 12. gates 的拒绝/超时路径不存在 — 明确保留为待办
- 核实：ConfirmationGate 只有 confirm，没有 reject、没有超时；
`docs/confirmation-card.md` 里的「驳回重跑/编辑/改派」是交互稿（文档标题
即"交互稿"），后端没有对应端点。本次不 invent 语义（驳回后流程何去何从
是产品决策），在 README 未接线清单和此处明确标注。
- 另注意（既有语义，非 bug）：gate 的批准按 (kind, meeting_id) 粘性保存，
同一 meeting 在同一进程内批准过就不会再拦——API 的 confirm 端点正是靠它工作。

### 13. 死代码与小瑕 — 已清理
- `calendar_agent/agent.py` 未用的 `import re` 删除；
`shared/llm.py` 未用的 `Optional` 删除；
`extractor.py` 未被引用的常量 `SELF_COMMIT_CUES` 删除；
`availability.py` 里的死变量、intent 里易误读的冗余分支已一并清理（语义不变，有用例锁定）。

> 注：第一轮写入的 AUDIT.md 在此处曾被截断（文件尾部一度残留截断标记），第二轮补齐时已发现并修复断尾；#13 之后的内容以本节及「三、第二轮补齐」为准。

---

## 三、第二轮补齐（2026-10-23，同日）

验证基线：补齐前 pytest 63 passed；补齐后 **79 passed**（新增 16 个测试：
`tests/test_brief.py` 6、`tests/test_reminders.py` 5、`tests/test_rejection.py` 5）。
mock demo 仍 `Demo OK`（generation_source=rules）。

### 14. 会前 Brief 接进 flow + API — 对应第一轮 #8，已补齐
- 复用 `CalendarAgent.prepare_brief()`（未重写），并给它补了标题行之外的
时间 / 参会人头信息。
- schema：新增 `BriefResult`，`Meeting.brief` 存生成结果（含 `generated_at`
与 `source_meeting_ids`）；重复生成覆盖旧 brief 并刷新时间戳。
- flow：新增 `MeetingFlow.prepare_brief(meeting, history=None)`（会前阶段，
与会后 `run()` 分开）；history 缺省从共享 store 取，经 `related_history()`
按「共享参会人（user_id）或共享标题主题（双字片段）」过滤——查不到就是
空列表，brief 只剩议程，不编造历史。
- API：`POST /meetings/{id}/brief` 生成并落库（任何状态可调）、
`GET /meetings/{id}/brief` 读取，未生成返回 404 并写明先调 POST。

### 15. 定时提醒：APScheduler 驱动 Tracker — 对应第一轮 #9，已补齐
- 新增 `src/orchestrator/reminders.py::scan_and_notify(db, notify_tool, now,
lead_hours=24)` 纯函数：到期窗口内且未提醒 → 发「即将到期」；已逾期且
未通知 → 发「已逾期」并把 item 状态翻为 `overdue`（与 TaskTracker 一致）；
done 不发。标记字段 `ActionItem.reminder_sent_at / overdue_notified_at`
保证重复扫描幂等（第二次扫描发送数为 0，已测）。
- 通知走 `tools/factory.build_notify_tool()`；新增 `tools/notify/mock.py`
的 MockNotifyTool —— provider=mock 时 factory 不再返回 None，扫描结果里
mock 发送单独计入 `mocked`；工具抛错逐项计 `failed`、不打断扫描、不落标记
（下轮会重试）。
- 配置：`REMINDERS_ENABLED`（默认 false）/ `REMINDER_SCAN_INTERVAL_MIN`
（默认 30）/ `REMINDER_LEAD_HOURS`（默认 24），config 与 .env.example 同步；
pyproject 主依赖与 Dockerfile pip 清单同步加 `APScheduler>=3.10`。
- 接线：FastAPI lifespan 仅在 `REMINDERS_ENABLED=true` 时启动
BackgroundScheduler 注册 interval job；apscheduler 导入/启动失败只打
warning 降级，API 照常起（已用模拟 ImportError 实测）。默认关闭，
现有测试/demo 行为不变（已实测开启时调度器能启动并干净关闭）。
- API：`POST /reminders/run` 手动触发一次扫描，返回
`{due_soon_sent, overdue_sent, mocked, failed}`，不依赖调度器即可验收。

### 16. 驳回路径：gates reject + 改派 + 可重跑 — 对应第一轮 #12，已补齐
- gates：新增 `reject()` 及 `reject_minutes / reject_tasks`（与 confirm 对称：
撤销批准、清 pending、记录 reason；后续 confirm 会清掉驳回记录），
另有 `is_rejected / rejection_reason`。
- schema：新增 `Rejection{gate, reason, at}`，`Meeting.rejections` 追加式
审计轨迹，驳回不删已生成内容。
- API：`POST /meetings/{id}/reject-minutes` / `reject-tasks`（body
`{reason}`，仅对应 awaiting_* 状态可驳回，否则 409）→ 状态置
`minutes_rejected` / `tasks_rejected`；
`minutes_rejected` 下 `POST /process` 放行重跑纪要（上一轮 process 护栏
仅对该状态放行，dispatched 仍 409），回到 `awaiting_minutes_confirmation`；
`tasks_rejected` 下新增 `POST /meetings/{id}/reextract-tasks`，基于已确认
纪要重抽任务回到 `awaiting_tasks_confirmation`。
- 改派：`PATCH /meetings/{id}/tasks/{item_id}`，仅
`awaiting_tasks_confirmation` 可改（否则 409），可改 owner_id / due_date /
priority（非法日期/优先级 422，item 不存在 404）；改 owner 后
`assignment_source="manual"`、置信度 1.0、owner_raw 同步为参会人姓名，
最终 dispatch 使用改后的值（已测全程：驳回 → 重抽 → 改派 → 分发用的
是新负责人）。
- `docs/confirmation-card.md` 已同步：卡片按钮与真实端点一一对应。

### 17. 第二轮后仍保留的诚实待办
- gates **超时**路径仍未实现（第一轮 #12 的另一半）：没有「待确认超时
自动升级/关闭」的语义定义，未 invent。（历史注记：此项已于第六轮闭环，
语义与证据见文末第六轮——超时默认只升级通知、可选自动驳回进既有
rejected 状态，绝不自动确认。）
- 真实外部服务仍为 stub：ASR、飞书/Google 日历与任务、真实通知通道
（配了 provider 的 stub 会抛 NotImplementedError，被各调用点降级处理，
不假装成功）；Postgres 持久化仍是内存 db（接口不变）。
- `LLM_SMALL_MODEL` 仍为预留未接线（标注不变）。
- 提醒只覆盖「单次扫描」语义：周汇总（weekly_summary）仍无调度入口；
调度器是 API 进程内 BackgroundScheduler，多实例部署会重复扫描——
靠 reminder_sent_at 标记在单进程内幂等，跨进程需先上 Postgres 再谈。
（历史注记：周汇总调度入口已于第六轮补齐；多实例重复扫描这一限制对
第六轮的闸门超时扫描与周报同样适用——闸门超时 notify 靠
*_gate_timeout_notified_at 标记在单进程内 exactly-once，跨进程仍需
行锁，与第四轮已知限制同源。）

---

## 四、第三轮：飞书真实 API（2026-10-23，同日）

验证基线：本轮前 pytest 79 passed；本轮后 **87 passed**（新增
`tests/test_feishu_tools.py` 8 个用例）。mock demo 仍 `Demo OK`。

### 18. 飞书三件套从 stub 换成真实 OpenAPI — 已实现（请求级验证，未做真机验证）

- **共享客户端**：新增 `src/tools/feishu_client.py::FeishuClient`。
  tenant token 调 `/auth/v3/tenant_access_token/internal` 获取并在内存
  缓存，按 `expire` 提前 5 分钟刷新；`request()` 统一组装
  Authorization、解析 `{code,msg,data}`，`code != 0` 抛
  `FeishuAPIError(code,msg,path)`。httpx 代理处理与 `shared/llm.py`
  一致（localhost 直连、远程手动读环境代理、注入 transport 时不用
  代理）。user token 支持 `FEISHU_USER_ACCESS_TOKEN`，并实现
  `/authen/v1/oidc/refresh_access_token`（`grant_type=refresh_token` +
  Basic app 凭据）；飞书明确返回 user token 失效类 code 时刷新并重试
  一次，刷新失败不静默改走 tenant。
- **日历**：`FeishuCalendarTool` 真实实现 create/list/get/update/delete、
  freebusy 和单独添加参会人。时间戳为 Unix **秒**字符串并带 timezone；
  `FEISHU_CALENDAR_AUTH=tenant|user` 决定 token 类型，tenant 只能写应用
  日历、user 写已授权用户日历；未配 `FEISHU_CALENDAR_ID` 时明确报错
  说明要先建应用日历或填用户日历 id。参会人只接受 `FEISHU_USER_MAP`
  映射结果或本身为 `ou_...` 的 ID，其余跳过并在结果 `warnings` /
  `skipped_attendees` 中列明；参会人添加失败不抹掉已成功创建的日程。
- **任务**：`FeishuTaskTool` 真实实现 Task v2 创建、PATCH 更新/完成与
  评论。due 时间戳为 Unix **毫秒**字符串（与日历秒级形成测试对照）；
  tenant token 可创建并以 `members[{id,type:"user",role:"assignee"}]`
  指派映射后的 open_id，无有效 assignee 时不带 members 并给 warning；
  返回 `data.task.guid` 作为外部 ID。创建继续保留本地 idempotency key
  缓存，并向飞书传稳定的 `client_token`。
- **通知**：`FeishuNotifyTool` 按 `FEISHU_WEBHOOK_URL` 向自定义机器人
  发送 interactive 卡片（标题 header + lark_md 正文），返回值带飞书
  `code/msg`，非 0 抛 `FeishuAPIError`；配 `FEISHU_WEBHOOK_SECRET` 时
  按飞书规范用 `timestamp + "\n" + secret` 做 HMAC-SHA256 并 base64，
  发送 `timestamp/sign` 字段。
- **配置与 factory**：config 新增 `FEISHU_CALENDAR_ID`、
  `FEISHU_CALENDAR_AUTH`、`FEISHU_USER_ACCESS_TOKEN`、
  `FEISHU_REFRESH_TOKEN`、`FEISHU_WEBHOOK_URL`、`FEISHU_WEBHOOK_SECRET`、
  `FEISHU_USER_MAP`（JSON 解析失败 log warning 并按空映射）和
  `DEFAULT_TIMEZONE`；旧 `FEISHU_WEBHOOK` 保持兼容读取。factory 在
  provider=feishu 时构造带共享客户端的真实工具；凭据不全时工具仍按
  既有诚实语义返回 mock 结果，不假装已调用。`.env.example` 已补齐每个
  键的来源说明。
- **文档与联调脚本**：新增 `docs/feishu-setup.md`（建应用、权限、
  tenant/user 日历取舍、取 calendar_id/open_id、填映射、webhook、
  联调顺序）和 `examples/feishu_smoke.py`（tenant token → 测试任务 →
  webhook → 可选测试日程）。脚本会产生带 `[smoke]` 前缀的真实数据，
  **本轮没有运行它**。
- **离线验证范围**：MockTransport 已验到请求级别——token 只取一次被
  多次调用复用、日历 body 秒级时间戳 + timezone、任务 due 毫秒级、
  参会人/负责人非法 ID 跳过与 warning、freebusy/get/update/delete
  路径、user token 刷新请求与失效后一次重试、webhook 卡片与签名可
  复算、飞书 `code != 0` 经 TaskTracker 落为单条 `dispatch_failed`。
- **未做真机验证（重要）**：本环境没有真实飞书租户凭据，尚未验证
  真实租户的权限审核状态、应用可用范围、日历模式、通讯录数据和
  webhook 群设置。只有用户填好自己的 `.env` 后运行
  `examples/feishu_smoke.py` 并逐项看到 OK，才算完成真机验证。
- **本轮范围外**：没有接飞书应用消息/卡片回调（通知仍是群自定义
  机器人 webhook）；没有接妙记转写（无公开 API）；user 日历仍是
  `.env` 中一组 token 的单用户模式，多用户 token 托管/凭据库未做；
  Google/Linear/Trello/Jira/Slack 等其他 provider 仍是原有 stub。

### 第三轮补充：真机验证结果（2026-10-23，用户提供自建应用凭据后实测，凭据未落盘）

- tenant_access_token：真实租户获取成功，客户端/鉴权/网络链路确认可用。
- 日历：列出应用主日历（calendar_id 已确认）；用本项目代码真实创建 [smoke] 测试日程并读回标题一致（mock=False，真实 event_id）→ **真机通过**。
- 任务：权限开通+版本审核通过后，真实创建 [smoke] 测试任务并读回（mock=False，真实 guid；due 为 13 位毫秒时间戳，status=todo）→ **真机通过**。
- 通知（webhook）：仍待用户提供群自定义机器人 webhook 地址后验证；在此之前仅请求级验证。
- 人员指派：真实指派需要 FEISHU_USER_MAP 填入各成员 open_id（需通讯录权限查询），demo 数据中的内部 id 不会自动匹配。
- 端到端真机跑（2026-10-23）：示例会议经 Brief → 纪要（规则模式）→ 双闸门自动确认 → 分发，3 条任务真实落入飞书任务（guid 逐条读回校验，status=todo，因未配 FEISHU_USER_MAP 而未指派到人）；跟进会候选第一项（2026-10-27 10:00）已真实建到应用日历并读回。本轮验证的是飞书分发链路，LLM 链路的真实验证见此前火山方舟实测记录。

---

## 第四轮：Postgres 持久化（2026-10-23）

### 设计

- 存储模型：一张表 `meetings(id TEXT PRIMARY KEY, status TEXT NOT NULL, data JSONB NOT NULL, updated_at TIMESTAMPTZ NOT NULL)`。整个 Meeting（Pydantic）序列化进 `data`，`status` 单独抽列供按状态查询。理由：当前查询模式只有按 id 取、列全部、按状态筛，JSONB 文档式最贴合；把会议拆成多张关系表只会带来迁移负担而无收益，待出现按负责人查任务、纪要全文检索等需求再拆列/拆表。
- `PostgresDB` 实现与 `InMemoryDB` 完全相同的接口（save/get/list_all/delete），psycopg v3，每次操作开一个连接（简单优先，未上连接池）；初始化时 `CREATE TABLE IF NOT EXISTS`。每次读都重新 `model_validate`，与内存版 deepcopy 的隔离语义一致（调用方改返回值不污染存储）。
- 工厂 `make_store(settings)`：`DATABASE_URL` 为 `postgresql://`/`postgres://` → PostgresDB；为空 → InMemoryDB（测试/demo 行为不变）；其他 scheme → ValueError 明确报错。连接失败抛 RuntimeError，消息含目标 host/port/db 与「DATABASE_URL is set/EMPTY」，且不带凭据，不吞成空结果。
- 调用点全部走工厂：进程级单例仍是 `src.shared.db.store`（导入时由 `make_store()` 构建一次），API、flow 的 Brief 历史、reminders 扫描都用这一个实例，没有每请求新建。
- 依赖：pyproject 主依赖 + Dockerfile pip 清单同步加 `psycopg[binary]>=3.1`；docker-compose 给 api 服务加 `environment.DATABASE_URL` 指向 compose 的 postgres 服务（凭据与服务定义一致）；`.env.example` 改为留空默认 + 注释示例（密码掩码），config 中 `database_url` 的 RESERVED 标注改为正式说明。

### 环境阶梯（怎么拿到真 Postgres 的）

本机无 psql/postgres/docker 二进制，apt 无可用 postgresql 包 → 走 pip 路线：驱动 `psycopg[binary]` 3.3.6 装入用户 site；服务器用 pip 包 `embedded-postgres` 18.6.3 自带的 PostgreSQL 18.6 真二进制（其 Python 封装在本机权限模型下起不来，改为把其 bundled pginstall 复制到 /tmp 后以专用 unprivileged 用户直接 initdb/pg_ctl 拉起，监听 127.0.0.1:15433，建测试库 meeting_agent_test）。是真服务器、真线协议，不是替身。

### 真实验收输出（摘录）

- 契约测试（同一套断言参数化跑两种实现）：`TEST_DATABASE_URL=... pytest tests/test_db_postgres.py` → **14 passed**；其中跨实例持久化用例：store A 写入含 action_items（external_task_id）、rejections、brief 的会议，**新建 store B（新连接）读回 `model_dump()` 逐字段相等**。
- 全量：设 TEST_DATABASE_URL 时 **101 passed**；未设时 **94 passed + 7 skipped**（Postgres 用例按设计 skip，默认套件不变红、不变慢）；mock demo 仍 Demo OK。
- API 重启不丢（uvicorn + DATABASE_URL 实跑）：POST 建会 → POST /process 后状态 `awaiting_minutes_confirmation`，且直接查库可见行 `(m-20261023-001, awaiting_minutes_confirmation)` → **杀进程** → 重启 uvicorn → GET 同一会议：状态不变、minutes 在、逐字稿 10 段在 → 还能继续 confirm-minutes 推进到 `awaiting_tasks_confirmation`（3 条任务），证明读回的对象可继续走流程而非只能看。
- 失败路径：DATABASE_URL 指向不存在的端口 → RuntimeError 原文含 `host=127.0.0.1 port=59999 db=meeting_agent; DATABASE_URL is set`，不静默。

### 已知限制

- 提醒防重复标记在 Meeting 的 JSON 文档内：单进程内幂等，多 API 实例并发扫描仍可能重复发（两边同时读到未标记）。根治需行锁（SELECT ... FOR UPDATE）或独立提醒标记表，列为下一期。
- PostgresDB 每次操作新建连接：当前负载（会议量级）足够；高频场景再换连接池，接口不变。
- `list_all` 按 updated_at 排序，与内存版的插入顺序近似但不严格等价（覆盖保存会改变次序）；当前调用方（Brief 历史、提醒扫描）都不依赖次序。

---

## 第五轮：真实 ASR（2026-10-23）

### 实现

- **引擎协议**：`src/agents/minutes/transcriber.py` 重写。`Transcriber` 内部按协议注入 engine（`transcribe(audio_path) -> list[TranscriptSegment]`）；默认引擎 `FasterWhisperEngine` 懒加载 `faster_whisper.WhisperModel`——首次转写才 import + 加载，模块 import 绝不触发下载（有子进程测试锁死）。参数来自 config：`ASR_MODEL`（默认 small）、`ASR_DEVICE`（cpu）、`ASR_COMPUTE_TYPE`（int8）、`ASR_LANGUAGE`（默认 zh，`auto` 时不传 language）、`vad_filter=True`。转写段映射到 `TranscriptSegment`：顺序 id、start/end 秒、文本 strip、空段跳过。
- **音频来源**：`audio_url` 支持 http(s)（httpx 下载到临时文件，代理处理复用 `llm.py` 的 `_proxy_kwargs`，注入 transport 时不加代理；转写后删临时文件）与本地路径。失败契约不变且更具体：下载失败/文件不存在/引擎未装/模型加载失败一律不抛崩 MinutesAgent，走既有 `needs_transcript`，具体原因经 `Transcriber.last_error` 并进 meeting 的 warning（如未装包时明确给出 `pip install -e ".[asr]"`）。
- **说话人分离未做**：pyannote 本轮不接（已从 pyproject 的 asr extra 移除并在注释说明）。ASR 段的 speaker 是统一占位「发言人」，`Transcriber.last_note` 由 MinutesAgent 在转写成功时写入 `meeting.warnings`，明示未做分离。
- **上传端点**：`POST /meetings/{id}/audio`（multipart）。存到 `ASR_UPLOAD_DIR`（默认 `data/uploads`，新增 config + `.env.example` 整段注释），文件名只由会议 id + 白名单扩展名（mp3/wav/m4a/flac/ogg/webm/mp4）生成，绝不拼用户文件名（有穿越文件名测试）；会议不存在 404、空文件 422、扩展名不合 422；存好后 `audio_url` 指向本地路径并 save，`needs_transcript` 状态自动回 `scheduled` 可重跑。主依赖因此加 `python-multipart`（FastAPI 解析 multipart 必需），Dockerfile pip 清单同步。
- **测试**：新增 `tests/test_asr.py` 16 个，全离线（注入假 engine / stub 模型 / MockTransport 下载 / monkeypatch 模拟包缺失），默认套件不触发任何模型下载。全量：**110 passed + 7 skipped**（原 94 + 7，只增不减）。

### 真实冒烟（examples/asr_smoke.py，本轮真跑）

环境条件（写明以便复现）：本机 2 核 CPU；系统 python 是 PEP 668 外部管理环境，faster-whisper 1.2.1 / ctranslate2 4.8.2 装在 `~/asr-venv`，冒烟以 `PYTHONPATH` 指过去用系统 python 运行；模型 `Systran/faster-whisper-small` 已缓存于 `~/.cache/huggingface`（464 MB，包外，不入打包）。加载时踩到本机已知坑：`NO_PROXY` 含 `[::1]` 使 huggingface_hub 的 httpx 客户端报 `Invalid port`，把 `NO_PROXY` 改成不带方括号形式后加载成功（与 `llm.py` 记录的环境问题同源，正常环境无此问题）；JFK 音频经代理用 httpx 直下成功，`HF_ENDPOINT=hf-mirror` 未用到。

- **英文 JFK**（openai/whisper 仓库 `tests/jfk.flac`，11.0 s）：模型加载 9.8 s，转写 8.5 s，2 段，全文 “And so my fellow Americans, ask not what your country can do for you. Ask what you can do for your country.” —— 断言 “fellow Americans” **PASS**。
- **中文**：edge-tts 7.3.1 已装，但其 WSS 连接被本机出口代理的自签证书拦截（`SSLCertVerificationError: self-signed certificate in certificate chain`），是本机环境限制；脚本已内置兜底顺序（edge-tts → gTTS → 跳过并说明），本次经 gTTS 合成同一句话（74,880 字节，9.4 s）。真实引擎转写 12.8 s，2 段，全文「我们决定将上线时间推迟到11月8号，李四负责支付渠道证书续期。」—— 断言含「上线」「证书」**PASS**。
- 样本与转写结果在 `examples/asr_samples/`（jfk.flac / jfk.txt / zh_meeting.mp3 / zh_meeting.txt）。速度预期：small/int8 在本机约 0.8–1.4 倍实时（不含首次加载约 10 s），1 小时录音约需 45–75 分钟，README 已按此写明，`large-v3` 只建议 GPU。

### 已知限制

- 无说话人分离：纪要中基于 speaker 的归因对 ASR 逐字稿不可靠，需人工复核（warnings 已标注）。
- 转写是同步阻塞调用：长音频会长时间占住一次 `/process` 请求，未做任务队列/异步化。
- 模型首次下载依赖 HF hub 可达（或预置 `~/.cache/huggingface` 缓存）；上传端点未设文件大小上限（骨架阶段）。

### 第五轮增补：豆包云端 ASR 引擎（volcano，2026-10-23）

第二个真实引擎 `VolcanoASREngine`（`src/agents/minutes/transcriber.py`，`ASR_ENGINE=volcano`），与本地 faster-whisper 并列、按配置二选一。协议已由真实服务实测验通，本轮按实测参数实现，未改任何参数：

- **协议要点**：提交 `POST https://openspeech.bytedance.com/api/v3/auc/bigmodel/submit`，查询 `POST .../api/v3/auc/bigmodel/query`（body 为空 `{}`）。请求头 `X-Api-App-Key`（App ID）、`X-Api-Access-Key`（Access Token）、`X-Api-Resource-Id`（`volc.seedasr.auc`，SeedASR 2.0）、`X-Api-Request-Id`（每任务一个 uuid，提交与历次查询共用同一个）、`X-Api-Sequence: -1`。提交 body：`audio: {url, format}` + `request: {model_name: "bigmodel", enable_itn, enable_punc, show_utterances, enable_speaker_info}`（均 true）。状态不在 body 里，在响应头 `X-Api-Status-Code`：20000000 成功 / 20000001 处理中 / 20000002 排队中 / 20000003 静音无语音 / 45000xxx 参数错 / 550xxxxx 服务错或任务超时；错误信息拼上同响应头的 `X-Api-Message`。成功 body 的 `result.utterances[]` 给分句（`start_time`/`end_time` 为**毫秒**，映射时除以 1000 转秒；`additions.speaker` 映射为「说话人N」，原始值保留可辨）；无 utterances 时用 `result.text` + `audio_info.duration`（毫秒）生成单段。轮询间隔 `VOLCANO_ASR_POLL_INTERVAL_SEC`（默认 5）、总超时 `VOLCANO_ASR_TIMEOUT_SEC`（默认 1800），超时报错里带排查指引。凭据缺失时报错点名缺哪个环境变量（`VOLCANO_ASR_APP_ID` / `VOLCANO_ASR_ACCESS_TOKEN`）。
- **task id 落库**：`Meeting` 新增 `asr_task_id`。提交成功的那一刻（轮询开始之前），引擎经回调 `Transcriber.on_task_submitted` → `MinutesAgent._persist_asr_task_id` 把 id 写到 meeting 并立刻 `store.save`（仅当该会议已在 store 中，即 API 路径；内存调用只设属性、由调用方原有 save 兜底）。这是第一单实测的教训：曾有任务提交后 id 没留住、进程一停就再也追不回结果。现在进程中途挂掉，凭存储里的 id 仍可去查询。有测试从第一次 query 的处理函数内部断言「此刻 store 里已经有这个 id」，锁死提交时落库而非结束时落库。
- **两条硬约束（均来自实测，代码里客户端先拦）**：
  1. **只接受公网 URL**：该 API 只收 URL 不收文件。`audio_url` 是本地路径时 volcano 引擎直接报错走 `needs_transcript`，warning 写清「volcano 引擎需要公网可访问的音频 URL，本地文件请用 faster-whisper 或先把文件放到对象存储/可公网访问的地址」，绝不尝试提交本地路径（Transcriber 对该引擎跳过本地文件解析这一步）。
  2. **格式白名单** `wav/mp3/ogg/spx/amr/aac/m4a`（按 URL 后缀判断；无后缀默认 wav 并在 warning 注明是猜的）：不在列表内的后缀在发出任何 HTTP 请求之前就拦下。原因是实测教训：**flac 不在文档支持列表内，提交 flac 任务不会被拒，而是静默卡死直到服务端超时失败（55001010）**——客户端不拦就会白等一场超时。
- **音频托管可达性（实测说明，避免误判）**：同一凭据、同一协议下，音频放在慢速海外文件托管时任务长时间挂起；换到国内可达的 CDN 后，3 秒 wav 约 10 秒内出结果。这是**音频托管的可达性问题，不是协议问题**——排查云端转写「卡住」时先换音频 URL 的托管位置，不要先怀疑参数。
- **说话人分离**：云端引擎开 `enable_speaker_info` 返回说话人，段的 speaker 是真实区分（说话人1/说话人2…），因此 volcano 转写成功时**不**附加 faster-whisper 那条「未做说话人分离」的 warning。代价是逐字稿归因依赖服务商的分离质量。
- **Secret Key 声明**：`VOLCANO_ASR_SECRET_KEY` 已进 config 与 `.env.example`，但**本接口未用到**——录音文件识别接口的鉴权只用 App ID + Access Token 两项（请求头），引擎从不发送 secret。它是旧版/其他火山语音接口的凭据，保留在配置里只为让一整套凭据同处一份 `.env`，文档与代码注释均已注明，不许误以为配了它才生效。
- **测试**：`tests/test_asr_volcano.py` 12 个，全离线（MockTransport 脚本化响应头状态机）：五头齐全与两次请求 Request-Id 一致、处理中→处理中→成功的轮询序列、毫秒→秒换算与 speaker 映射、flac 零请求被拦、本地路径被拒含指引、静音码落 `needs_transcript` 且 warning 含「音频中未检测到人声」、错误码 45000001 的 code/msg 透传、task id 提交时落库、凭据缺失点名、无 utterances 的全文兜底、无后缀默认 wav。全量：**122 passed + 7 skipped**（第五轮 110 + 本轮 12，只增不减）。

---

## 第六轮：闸门超时 + 周报调度入口（2026-10-23）

验证基线：本轮前 pytest 122 passed + 7 skipped；本轮后 **142 passed + 7 skipped**（新增 `tests/test_gate_timeouts.py` 13、`tests/test_weekly_summary.py` 7，共 20 个，全离线）。mock demo 仍 `Demo OK`。至此 #9 与 #12/#17 保留的两项诚实待办全部闭环（#17 原文已加历史注记）。

### 19. 确认闸门超时语义 — 对应第一轮 #12 / 第二轮 #17，已闭环

- **产品红线**：超时**绝不自动确认**。超时只有两种处置，由 `GATE_TIMEOUT_ACTION` 选择：`notify`（默认）发一条升级提醒后会议继续等确认；`reject` 把会议转入既有 rejected 状态等人工重跑。不存在第三种「超时自动放行」。
- **计时**：`Meeting` 新增 `minutes_gate_entered_at` / `tasks_gate_entered_at`。flow 的 `run()` 停在任一闸门时经 `flow.stamp_gate_entry()` 打戳；API 侧 confirm-minutes 落 `awaiting_tasks_confirmation`、reextract-tasks 重回闸门时同样打戳。重进闸门（驳回后重跑/重抽）覆盖时间戳并清掉对应 notified 标记——时钟与「只发一次」都按「这一次进入」重新算。两闸门各自独立计时（有测试用 tasks 会议上残留的旧 minutes 时间戳证明扫描读的是当前闸门的字段，不串门）。
- **存量会议**：时间戳为 None 的老会议，扫描器第一次见到只补记当前时间（动作记录 `backfilled`，可查），绝不回溯补记——否则所有历史会议第一次扫描就会集体「超时」。有测试以 0.01h 的发夹阈值验证：首次扫描只补记、补记后 1 小时才按新时钟触发。
- **扫描**：新增 `src/orchestrator/gate_timeouts.py::scan_gate_timeouts(db, notify_tool, now, settings)`，与第二轮 `scan_and_notify` 同范式（纯函数、标记防重、单条失败隔离、投递如实回报）。notify 模式经通知工具发升级提醒（注明会议标题、卡在哪个闸门、已等待时长、进入时间；收件人取 organizer 参会人），`*_gate_timeout_notified_at` 置位后同一次进入只发一次（二次扫描 0 条，已测）；mock 工具发送 `delivered=False` 并在 `delivery_note` 写明，工具抛错则不置位、记 `notify_failed`、下轮重试（已测）。reject 模式置 `minutes_rejected` / `tasks_rejected`（不新造状态名）、清对应 confirmed 标记、`Rejection` 留痕 reason 为 `confirmation timeout: …（waited Xh）`；之后 `/process` 重跑纪要、`/reextract-tasks` 重抽任务的既有语义直接生效（有 API 全程测试：超时驳回 → process 重跑回 awaiting 且时间戳刷新）。
- **配置与接线**：`GATE_TIMEOUT_HOURS`（float，默认 48）、`GATE_TIMEOUT_ACTION`（notify|reject，默认 notify，非法值回退 notify 并打 warning）、`GATE_TIMEOUTS_ENABLED`（bool，默认 false）、`GATE_TIMEOUT_SCAN_INTERVAL_MIN`（默认 60）。调度注册抽成 `api/main.py::build_scheduler(settings)`（与 reminders 共用一个 BackgroundScheduler），仅在 enabled 时注册 interval job；`POST /gate-timeouts/run` 手动触发返回 `{"actions": [...]}`（会议 id、闸门、动作、delivered、delivery_note、waited_hours），手动路径还把 reject 结果同步进进程内 gate 对象，与 `/reject-*` 端点行为一致。

### 20. 周报调度入口 — 对应第一轮 #9，已闭环

- **汇总构建**：新增 `src/orchestrator/weekly_summary.py::build_weekly_summary(db, now, window_days=7, notify_tool=None)`——这是 `TaskTracker.weekly_summary()` 的第一个调用点。跨全部会议收集 ActionItem，窗口为截至 `now.date()` 的 trailing 窗口 `[period_start, period_end]`（含两端）。分组（互斥）：本周到期未完成（未完成且 due 在窗口内）/ 逾期未完成（未完成且 due 早于窗口）/ 未定日期仍开放（未完成且无可解析 due）/ 本周已完成（done 且 due 在窗口内）；未来才到期的开放项与窗口外完成的项不入报。口径说明（诚实边界）：ActionItem 没有 completed_at 字段，「本周已完成」只能以 due_date 落在窗口内为据。正文沿用 `weekly_summary` 既有格式（标题行 + `- [负责人] 标题（截止 …，状态）`），条目在收集层以副本前缀 `《会议标题》` 补齐会议上下文（不回写存储，已测存储不被污染）；已完成项单列一节，行格式与未完成明细一致。头部带周期与四组计数。
- **投递与留痕**：经 `build_notify_tool()` 发送，返回 `{period_start, period_end, summary, delivered, delivery_note}`。未配工具 / mock 工具 / 工具抛错时 `delivered=False` 且 note 写明原因，summary 全文照常产出——手动端点永远能拿到全文，周报不因发送失败而丢失（三种情形均已测）。
- **调度与端点**：`WEEKLY_SUMMARY_ENABLED`（默认 false）；开启时同一 BackgroundScheduler 上注册 CronTrigger 每周一 09:00（时区取 `DEFAULT_TIMEZONE`，默认 Asia/Shanghai）；`POST /weekly-summary/run?window_days=7` 手动触发返回上述结构（已测默认 7 天窗口与 window_days=3 的周期跨度）。

### 本轮环境备注

- 本机用户 site-packages 一度缺 `python-multipart`（pyproject 主依赖、第五轮上传端点所需），表现为导入 `src.api.main` 时 FastAPI 在 `/audio` 路由注册处抛 `RuntimeError: Form data requires "python-multipart"`、7 个测试文件 collection 失败；已按 pyproject 声明 `pip install --user python-multipart`（0.0.32）补齐后全量恢复并达 142 passed。与代码无关，记此备查。
- Postgres 契约：`tests/test_db_postgres.py` 的富字段 fixture 已补四个新闸门时间戳字段，model_dump 逐字段相等断言覆盖 JSONB 往返；本轮未设 TEST_DATABASE_URL，Postgres 参数仍按设计 skip（与前几轮相同，未假装跑过）。

---

## 第七轮：LLM 质量修复 — 决策/待办边界 + 任务抽取排除会议安排（2026-10-23）

验证基线：本轮前 pytest 142 passed + 7 skipped；本轮后 **153 passed + 7 skipped**（新增 `tests/test_llm_quality_fixes.py` 11 个，全离线、假 LLM）。**验证边界（重要）**：本轮只有离线验证。真机复验——同一示例会议在火山方舟 lite（doubao-seed-2-0-lite-260428）上重跑、断言 decisions 无分派句且任务恰为 3 条——需要用户再次提供方舟 key 后补做，本轮**没有**真机验过，不许按已验通汇报。

**真机复验补记（2026-10-23 晚，用户再次临时提供方舟 key 后补做，key 未写入任何文件）**：示例会议全流程实跑（61 秒，纪要与任务 generation_source 均为 llm、一遍过未触发 repair）。结果：decisions 恰 2 条——「上线推迟至11月8日」（真决策）与「下周二下午召开进度跟进会，确认证书和联调结果」（集体约定，属合法决策，且已被日程链正确接住：follow_up_candidate_times 给出 2026-10-27 的 10:00/14:00/16:00 三个候选）；原病历的 3 条分派句（李四负责/王五负责…）全部消失，suspect 检测器对实跑 decisions 复检为零命中。任务恰 3 条且全对：证书续期（李四，conf 0.95，2026-10-28）、灰度方案（王五，0.95，2026-10-24）、联调报告（王五，0.95，2026-11-01）；「召开进度跟进会」不再出现在任务列表。risks 恰 1 条真风险（证书过期，high）。两个瑕疵真机闭环。

### 病历（2026-10-23 方舟真机实测发现，一直挂账的两处）

1. **纪要 decisions 与任务列表重叠**：LLM 纪要把 3 条分派句（「李四负责…」「王五负责…」类）写进了 decisions，与随后抽出的任务列表重复。根因：纪要 prompt 没有决策/待办的边界定义。
2. **任务多抽会议安排**：任务抽取多出 1 条「召开进度跟进会确认证书和联调结果」——开下一次会是日程 Agent 的职责，不该进任务列表。根因：任务 prompt 写了「高召回优先」但没排除会议安排类。

### 21. 修复一：纪要 decisions 边界（prompt + suspect 检测走 repair，绝不静默删）

- **prompt**：`src/agents/minutes/prompts.py` 的 SYSTEM_PROMPT 新增「决策与待办的边界」一节：决策 = 集体拍板的结果/结论/规则（示例「上线推迟至11月8日」「灰度范围定为5%用户」「证书到期前必须完成续期」）；带「某人 + 动作 + 期限」的分派/承诺句不是决策，是待办，系统另行抽取，不要写进 decisions；附一句话判定法——删掉人名仍是成立的结论 → 决策，必须有人去执行才有意义 → 待办。
- **suspect 检测**：`src/agents/minutes/analyst.py` 新增 `find_suspect_decisions(decisions, names)`，在 `_validate_and_clean` 之后、首次尝试成功路径上运行。判定规则（两个条件同时命中才算）：① decision **文本**含参会人姓名（名单 = participants 参数 ∪ 逐字稿说话人；只看文本，不看 decided_by）；② 文本命中分派标记——`负责`，或 `(在|于)…前(完成|提交|交付)` 的期限分派式。保守性的实证（均有单测）：「由李四牵头成立专项组」有人名但牵头无负责 → 不报；「上线推迟至11月8日」带日期但无人名 → 不报；「证书到期前必须完成续期」是集体规则、无人名 → 不报。
- **处置（红线：不静默删）**：suspect 不删除、不改写，而是包装成 `_SuspectDecisionsError`（错误信息逐条写明第几条、命中哪个人名与哪类标记、应如何移出/改写）喂进**既有的那 1 次 repair**，由模型复判；repair 模板同步补了一条 suspect 修正指引。repair 后仍有 suspect 的，结果照常接受（模型可能确有依据），suspect 原文保留，并在新增字段 `MinutesResult.generation_notes` 留痕（`src/shared/schema.py`），`MinutesAgent` 同步把 notes 追加到 `meeting.warnings` 人工可见通道。为什么不硬删：硬规则删必误杀真决策（见上三例），且静默删改纪要内容违背本项目一贯的「留痕」原则。
- **范围**：suspect 检测只作用于 LLM 链；规则路径的 decisions 来自确定性 cue 抽取，不在本轮范围。

### 22. 修复二：任务抽取排除会议安排（prompt + 确定性安全网）

- **prompt**：`src/agents/task_agent/prompts.py` 的 SYSTEM_PROMPT 新增排除条款：召开/组织/安排下一次会议、跟进会、评审会、例会等「会议安排类事项」属于日程 Agent 职责，即使已定时间与参会人也不抽为 action_item；但跟进会要讨论的实质交付物（续期证书、提交联调报告、准备评审材料）照常高召回。EXTRACTOR_PROMPT_TEMPLATE 在决策列表后补注：「决策列表仅供参考，其中若混有分派句，以逐字稿为准照常抽取，不要因此漏抽」——防止纪要侧修好之前，抽取侧被混入的分派句带偏漏抽。
- **安全网**：`src/agents/task_agent/extractor.py` 新增 `filter_scheduling_items` / `is_scheduling_title`，在 `extract()` 的统一收尾 `_finish` 中执行，覆盖 LLM / llm_repaired / rule_fallback / rules **全部路径**。模式（保守）：标题中出现会议动词 `召开|组织|主持|安排`，且其后 4 字内（不跨标点）紧跟会议名词 `跟进会|评审会|例会|同步会|启动会|碰头会|会议(?!室)` 才丢弃（名词集在任务书清单外加了启动会/碰头会两个无歧义的会议名词）。keep-case 五条全部有单测且必须保留：「准备评审会演示材料」（动词是准备）、「提交会议纪要」（无会议动词）、「预订会议室」（无会议动词 + `会议(?!室)` 双保险）、「安排联调环境」（安排 + 非会议对象）、「组织联调测试」（组织 + 测试非会议）。
- **规则路径实证**：规则版在示例会议原逐字稿上不会产出会议安排条目（第 9/10 段无负责人命中）；但合成段「张三负责组织下周评审会，确认联调结果。」会被规则版抽成「组织下周评审会，确认联调结果」——安全网实测拦下，同路径的「准备评审会演示材料」保留（有端到端单测）。
- **留痕**：每丢一条，`ActionItemExtractor.extraction_notes` 记一条（含标题原文），`TaskAgent.run` 同步追加到 `meeting.warnings`。绝不静默消失。若某次抽取过滤后为空列表，如实为空（会议只产生了开会安排时这是正确结果），不触发规则回退。

### 测试证据（tests/test_llm_quality_fixes.py，11 个）

- prompt 断言 3 个：两份 SYSTEM_PROMPT 含边界/排除条款关键表述；extractor 模板含「仅供参考…不要因此漏抽」旁注。
- suspect 检测器单测 1 个：3 条分派句命中、5 条真决策/边界句不误报。
- 纪要链 3 个（MockTransport 假 LLM）：首答含 2 suspect + 1 真决策 → 恰好触发 1 次 repair、repair 请求含「疑似待办」与第 2/3 条定位 → 次答干净后 source=llm_repaired、真决策保留、notes 为空；repair 后仍 suspect → 原文保留 + generation_notes 留痕；真决策集（含牵头成立专项组等）首答即过、只调用 1 次、source=llm。
- 安全网单测 2 个：5 条 convening 标题全丢、5 条 keep-case 全留。
- 任务链 2 个：假 LLM 返回 3 真任务 + 1 条「召开进度跟进会…」→ 留 3 丢 1 且 extractor notes 与 meeting.warnings 双留痕；规则路径合成逐字稿 → 会议安排条目被拦、评审会演示材料保留。

## 第八轮：前端 + 消息总线 + 独立编排配置（2026-10-23）

背景：魔乐社区海光专场第二期验收要求「前后端完整代码、Agent 编排配置文件」，评审维度原文问「Agent 之间是否通过消息总线有效通信」。对照评估（~/workspace/meeting-agent-plan/第二期验收标准对照评估.md）确认三处交付形态缺口：项目无前端、无消息总线、无独立编排配置。本轮三项全部补齐，均真接线、有测试。

### 23. 消息总线（进程内事件总线）— 已闭环

- **实现**：新增 `src/orchestrator/eventbus.py`。同步 `EventBus`：`subscribe(event_type, handler)`（支持装饰器与 `"*"` 通配）、`publish(event)`、`emit(meeting, type, payload)`。事件为 Pydantic `Event{type, meeting_id, at, payload}`。订阅者异常隔离：抛错被记入 `bus.errors` 并打日志，不阻断其余订阅者与发布方（有单测）。
- **总线真实驱动范围（如实说明）**：跨 Agent 的两段衔接是真订阅驱动——`MeetingFlow.__init__` 注册 `minutes.confirmed → _on_minutes_confirmed → TaskAgent 抽取` 与 `tasks.confirmed → _on_tasks_confirmed → CalendarAgent 提议 + TaskAgent 分发`。`flow.run()` 的双闸门通过后只 publish 确认事件、下游由处理函数接力；API 的 confirm-minutes/confirm-tasks 端点已改为调用 `flow.confirm_minutes/confirm_tasks`（同样只发布事件），端点自身不再直接调抽取/分发。**其余阶段仍是直接调用**：纪要生成是 run 内的顺序步（gate1 之前没有可订阅的上游事件）、prepare_brief 由 API/调用方直接触发、reextract-tasks 是同 Agent 重跑——这些节点只发布审计事件，不经总线接力。理由：总线承载的是管线生命周期与跨 Agent 边界，Agent 内务（转写、负责人消解）与单 Agent 步骤强行事件化只会增加间接层，不增加解耦收益。
- **事件目录**（只发已存在节点）：meeting.created、brief.generated、transcript.ready、minutes.generated、gate.minutes.entered、gate.tasks.entered、minutes.confirmed、minutes.rejected、tasks.extracted、tasks.confirmed、tasks.rejected、schedule.proposed、tasks.dispatched、dispatch.failed、reminders.sent、gate.timeout、weekly_summary.generated（全进程事件，只发布不落单会日志）。提醒/超时两类由 API 扫描包装层发布：reminders.sent 按扫描前后邮戳差分识别实际发送的会议（不是按计数瞎发），gate.timeout 按扫描返回的动作记录逐条发。
- **审计落库**：`Meeting` 新增 `events: list[MeetingEvent{type, at, payload}]`。`emit()` 先 append 到 meeting.events 再 publish，日志与总线不可能漂移；随会议经 store 持久化（Postgres JSONB 自动兼容，有 InMemoryDB 往返单测；Postgres 参数化契约测试随全量套件在本轮未设 TEST_DATABASE_URL 时按设计 skip）。payload 纪律：只放计数/id/状态摘要，逐字稿全文永不入 payload（有单测断言示例会议逐字稿原句不出现在任何 payload 里）；emit 另对形似凭据的键名（secret/token/password/api_key 等）打码。
- **测试**（tests/test_eventbus.py，8 个）：发布顺序与通配、异常隔离与 errors 记录、emit 落审计 + 凭据打码、全流程事件序列顺序（created < minutes.generated < confirmed < tasks.extracted < tasks.confirmed < schedule.proposed < dispatched）且时间戳单调、停在闸门 1 时无 confirmed/extracted 事件、事件日志 store 往返不丢、API 确认/驳回路径事件齐全且确认后任务确由总线处理函数抽出。

### 24. 独立 Agent 编排配置文件 — 已闭环

- **文件**：`config/orchestration.yaml`（项目根 config/）：pipeline.steps 六步开关（brief/transcribe/minutes/tasks/schedule/dispatch）、gates 两闸门 enabled + timeout_hours、scheduler 三作业 enabled + interval_min/cron、providers（calendar/task/notify）、engines（asr/llm_mode）。取值与 tools/factory.py、Settings 的现有口径对齐，默认内容与改造前行为逐项一致。
- **加载器**：`src/shared/orchestration.py`（pyproject 新增 pyyaml 依赖，Dockerfile 同步）。路径解析：参数 > `ORCHESTRATION_CONFIG` 环境变量 > 仓库默认路径。文件缺失 → 内置默认（INFO 日志）；文件写坏（YAML 语法/类型错误）→ `OrchestrationError`（ValueError）点名文件与问题，绝不静默忽略。
- **与环境变量的优先级（代码注释与 README 均明示）**：环境变量 > 编排文件 > 内置默认。落地方式分三路：① steps/gates 开关由 flow 直接读加载结果，另有 `ORCH_STEP_<NAME>_ENABLED`/`ORCH_GATE_<NAME>_ENABLED` 覆盖文件；② providers/engines 经 `Settings` 字段工厂兜底读取（env 未设置时取编排默认值），所以 agents/factory 的全部现有路径自动生效；③ scheduler 三作业由 `build_scheduler(settings, orchestration)` 注册，`*_ENABLED` 环境变量一旦设置以 Settings（env 派生）为准，未设置时听文件的 enabled/interval/cron；闸门 timeout_hours 经 API 路径填入 Settings 的 per-gate 字段（`MINUTES_/TASKS_GATE_TIMEOUT_HOURS` 与共享 `GATE_TIMEOUT_HOURS` 优先于文件），扫描器按闸门取阈值（向后兼容：per-gate 为 None 时用共享值，第一~七轮的扫描测试不受影响）。
- **开关真实生效（验收点，全部有单测）**：关 brief → prepare_brief 不生成、warning 留痕、无 brief.generated 事件；关 dispatch → 全流程停在新状态 `tasks_confirmed`、任务未外发、warning 留痕；关 transcribe → 无逐字稿会议停在 needs_transcript 并注明是配置关闭；关 tasks → 纪要确认后不抽取（run 与 API 两路都守）；关 schedule → 不产候选时间、分发照常；关 minutes（步骤）→ 不生成纪要并留痕。
- **闸门关闭语义的选择与理由**：选「自动通过 + 留痕」，不选「加载时报错禁止关闭」。理由：评审可能现场改配置验收开关生效，报错等于功能不存在；而风险（闸门被关后像人工确认过一样无声通过）用留痕封堵——每次自动通过都在 meeting.warnings 写明是编排配置关闭的、确认事件 payload 带 `via=config_gate_disabled`，与人工确认（via=api/approved）在审计日志里可区分。默认两闸门均为开启，行为与从前一致。

### 25. 前端（零构建 Web UI）— 已闭环

- **实现**：`web/index.html + app.js + style.css`，原生 JS，无构建链、无外部 CDN，离线可开。FastAPI 在 `src/api/main.py` 末尾挂载（注册顺序在全部 API 路由与 webhook 之后，`GET /` 返回 index.html，`/` 静态挂载只兜底 web/ 文件，API 路由优先，有单测断言 /health 与 404 行为不被吞）。新增 `GET /meetings` 列表端点（UI 列表的数据来源，此前 API 只有按 id 取单个）。
- **功能**：会议列表（标题/状态徽章/时间）与新建（表单建会、内置小样一键载入、完整 JSON 导入）；会议详情（基本信息、Brief 生成与展示、逐字稿带说话人/时间且可被纪要证据徽章点击高亮定位、纪要摘要/decisions/risks/open_questions 并标注证据 segment、任务表含负责人/置信度/到期/状态/外部 id、跟进会候选、事件时间线读 meeting.events 展示总线日志）；双闸门操作（确认/驳回带原因、驳回后重新抽取、任务改派表单含负责人/到期/优先级，全部调现有端点，409 时把后端 detail 原文弹给用户）；工具区（提醒扫描、闸门超时扫描、周报生成并渲染 Markdown）；录音上传（调现有 /audio 端点）与逐字稿补录。
- **防漂移测试**（tests/test_web_ui.py，5 个）：GET / 返回 200 且含关键 DOM 标记；/app.js、/style.css 可达；把 app.js 里所有 api()/fetch() 的字面路径抽出并归一化（`${id}` 与 `{meeting_id}` 统一），与 app 的 APIRoute 路由表逐一比对，有一个对不上就红。
- **Docker 同步**：Dockerfile 新增 `COPY web ./web`、`COPY config ./config` 与 pip 依赖 pyyaml；docker-compose 无代码挂载卷，构建上下文自动包含新目录，无需改动。

### 本轮验证基线

全量 pytest：**179 passed + 7 skipped**（基线 153+7，本轮新增 26：test_eventbus 8 + test_orchestration 13 + test_web_ui 5，只增不减）。examples/demo.py 实跑通过（规则模式全流程 dispatched）。至此第二期验收对照评估的五项交付形态差距中，前端/消息总线/编排配置三项闭环；剩余未做：源码仓库建仓推送（需用户决定托管平台）、Docker 真机构建验证（本环境无 Docker，仅静态核对，照旧如实标注）。

## 第九轮：第八轮代码审查五项修复（2026-10-24）

背景：对第八轮新增代码（eventbus / flow / orchestration / api 调度 / web 前端）做了一次专项审查，确认 5 个问题（2 中、2 中低、1 低）。本轮逐项修复，每项均有针对性新测试（tests/test_review_fixes.py，20 个）。

### 26. 承重衔接被总线异常隔离吞错 — 已修复

- **问题**：`EventBus.publish()` 吞掉全部订阅者异常记 `bus.errors`。两段跨 Agent 衔接（minutes.confirmed→任务抽取、tasks.confirmed→日程提议+分发）是承重的也在其中：抽取抛错时 confirm_minutes 照样盖戳返回成功，会议带 0 条任务进任务闸门，失败只在日志里。各 Agent 有「从不抛错」的回退设计，触发概率低，但失败模式是静默的状态不一致。
- **修法**：① `EventBus.subscribe(..., critical=)` 双轨语义——非 critical 照旧隔离；critical 先记 `bus.errors`（记录新增 `critical` 字段）再向 publish/emit 调用方**传播**，投递在该处停止。flow 的两个 handoff 以 critical 注册。② handoff handler 包 try/except：失败先往 `meeting.warnings` 追加一条注明哪段衔接失败与异常摘要，再 re-raise——失败在会议数据上可见。③ 置位顺序反转：`confirm_minutes`/`confirm_tasks`/`run()` 中 `minutes_confirmed`/`tasks_confirmed` 一律移到对应 `_emit` **成功之后**（此前先置位后 emit，失败后标志已 True，端点幂等早退会堵死重试）。④ API 两确认端点捕获 handoff 异常时先 `store.save(m)` 再 re-raise（FastAPI 默认 500）：会议保持未确认可重试，warning 随记录可见。注：emit 先写审计再 publish 的顺序不变——critical 抛错时 `minutes.confirmed` 事件已在 meeting.events 中，这是正确的（事件确实发过，失败的是下游衔接），eventbus/flow docstring 已写明双轨语义。
- **测试**：critical 传播 + errors 记录 + 投递停止、非 critical 与 critical 混编时隔离不误伤、装饰器 critical 形式；flow 级注入抛错 TaskAgent 替身：confirm_minutes 抛错、标志仍 False、warning 在、换回正常替身重试成功；API 级同场景：500 → GET 核对未确认 + warning 已落库 → 重试 200 且任务抽出。

### 27. tasks 步关闭时确认后状态与标志矛盾 — 已修复

- **问题**：编排关 tasks 步时，`confirm_minutes` 置 `minutes_confirmed=True` 却不改 status（仍 `awaiting_minutes_confirmation`）——前端按 status 渲染纪要闸门，与已确认状态矛盾；`run()` 同分支同样问题。这正是「评审现场关步验收」会踩到的路径。
- **修法**：`confirm_minutes` 与 `run()` 的 tasks-disabled 分支统一落到新状态 **`minutes_confirmed`** 并在 warning 注明任务抽取被编排配置关闭。对齐全部 status 消费方：API confirm-tasks 对该状态单独 409 并说明「tasks 步已在编排配置中关闭，没有待确认的任务」；reject-minutes 的 status 守卫天然拒绝（已确认纪要不可再驳回——核查确认原守卫即以 status 为准，修复后 flag 与 status 同步移动，守卫成立）；闸门超时扫描的 `_GATES` 只认两个 awaiting 状态，新状态天然不被扫（有单测断言 100 小时前进入也不被动）；前端 `statusClass` 给它 `st-info` 徽章、`gatePanel` 显示说明条「纪要已确认；任务抽取已在编排配置中关闭」。
- **测试**：run() 路径与 API 路径各断言 status/标志/warning 三者一致；API 后续 confirm-tasks → 409 且文案含原因、reject-minutes → 409；超时扫描对 `minutes_confirmed` 零动作。

### 28. _known 注册表无界增长 — 已修复

- **问题**：`MeetingFlow._known` 只进不出，API 的 flow 是模块级单例、活整个进程——每场 emit 过的会议（含完整逐字稿）被永久引用，长期运行内存无界增长。
- **修法**：`_known` 改 `OrderedDict` LRU，上限 512（`_KNOWN_LIMIT`），`_emit` 登记时 `move_to_end` 并淘汰最旧；另在 `_on_tasks_confirmed` 的分发支路结束时（`tasks.dispatched` 已 emit）显式 `pop` 该会议——终态后 flow 不再需要活对象，之后任何 emit 都会重新登记，淘汰安全。驳回会议不淘汰（还要重跑，且重跑本就会重新登记，此处只保持简单）。
- **测试**：示例会议跑到 dispatched 后断言 id 不在 `_known`；连发 600 场轻量会议的 meeting.created 后断言 `len(_known) ≤ 512` 且最新在内、最早已被淘汰。

### 29. 前端 URL 未编码 + 后端 ID 无字符校验 — 已修复

- **问题**：`web/app.js` 全文无 `encodeURIComponent`，会议 ID 是用户自由输入（新建表单 / JSON 导入），含 `/`、`?`、`#`、空格的 ID 建得出来但所有按 id 的请求路径都是坏的；且含 `/` 时后端路由本身也匹配不上。
- **修法（两端）**：① 前端引入 `const enc = encodeURIComponent`，全部 11 处把 meeting id / item id 拼进 URL 的调用点（详情、process、brief、双确认、双驳回、reextract、transcript、audio、PATCH 改派）统一过 `enc()`；JSON body 里传的 meeting_id 不编码。② 后端在模型边界校验：`Meeting.id` 加 `field_validator`，字符集 `^[A-Za-z0-9._-]+$`（`MEETING_ID_PATTERN`），违规在建会/导入/按 JSON 提议等全部入口统一 422 并说明允许字符——id 还要进音频上传文件名，字符集收紧同时封住路径拼接面。既有全部测试与示例 id 均在字符集内，无误伤。
- **测试**：POST id ∈ {a/b, a b, a?b, a#b, 空, 中文} → 422；`ok-id_1.2` → 200；前端断言 app.js 中每个 `/meetings/${...}` 与 `/tasks/${...}` 插值都以 `enc(`/`encodeURIComponent(` 开头（并钉住 `enc` 别名定义本身）。

### 30. 编排数值无范围校验 + 调度单作业失败牵连全部 — 已修复

- **问题**：`interval_min` 负数、`cron` 错串要到 `build_scheduler` 才炸，而 lifespan 是整体 try——一个作业的坏配置让三个后台作业全灭，只留一行 warning；`timeout_hours` 负数不报错，扫描时把所有待确认会议判超时，`GATE_TIMEOUT_ACTION=reject` 时是真驳回（破坏性）。
- **修法**：① `orchestration._parse` 加载时范围校验并以 `OrchestrationError` 点名键：`interval_min` ∈ 1..1440、`gates.*.timeout_hours` ∈ (0, 720]、`scheduler.*.cron` 非空时须能被 `CronTrigger.from_crontab` 解析（校验函数内延迟 import APScheduler，失败带原始错误信息）。② `scan_gate_timeouts` 入口第二道防御：生效阈值（共享或任一 per-gate）≤ 0 直接 `raise ValueError` 点名字段——环境变量可绕过文件校验从 Settings 侧灌入负值，手动端点与定时作业都会因此大声失败而非误动作。③ `build_scheduler` 改逐作业隔离构建：每作业的 trigger 构造与 add_job 各自 try，失败只跳过该作业并 `logger.error` 点名，其余照常注册；全部失败时返回 None；lifespan 的整体 try 保留作最后防线。
- **测试**：坏 YAML 三例（interval_min: 0、timeout_hours: -1 及 721、cron: 'not a cron'）各断言 OrchestrationError 且信息含键名；Settings 灌负共享阈值/per-gate 0 → scan 抛 ValueError 且会议零改动（reject 模式下也不驳回）；编排注入坏 weekly cron → build_scheduler 仍注册 reminder-scan、跳过 weekly-summary。

### 本轮验证基线

全量 pytest：**199 passed + 7 skipped**（基线 179+7，本轮新增 20：tests/test_review_fixes.py，只增不减）。examples/demo.py 实跑通过。本轮为纯修复轮，无新增功能；第八轮遗留的两项未做（源码仓库建仓推送、Docker 真机构建验证）维持原状、照旧如实标注。
