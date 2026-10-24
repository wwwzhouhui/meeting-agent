# meeting-agent — 纪要 + 日程 + 任务 三 Agent 会议助手

落地方案的实现骨架（方案原文：`../三Agent会议助手落地方案.md`）。
**设计目标：没有任何 API Key、没有数据库时也能完整跑通全流程。**
日程 Agent 的核心逻辑先用规则实现（关键词/正则/日期解析/别名表）；
纪要 Agent 和任务抽取已接真实 LLM 主路径（失败自动 repair 一次、再失败回退规则，
无 key 时纯规则照常跑，详见「接真实 LLM」一节）。飞书日历、任务和
群机器人通知已接真实 OpenAPI（详见「飞书真实 API」一节）；其他外部
系统仍留接口和 TODO，切真实服务不需要改 Agent 业务代码。

> 📖 **操作手册**：面向使用者的图文手册（界面总览、全流程操作、功能详解、状态说明与常见问题）见 **[docs/操作手册.md](docs/操作手册.md)**；在线演示：https://meeting-agent-swart.vercel.app/

## 架构与方案的对应关系

| 方案概念 | 代码位置 |
|---|---|
| 共享契约 `Meeting` 对象（方案第 4 节） | `src/shared/schema.py`（Pydantic v2，含出处 validator） |
| 编排器（CrewAI Flows 风格，纯 Python 模拟） | `src/orchestrator/flow.py`（`MeetingFlow.run`） |
| 消息总线（进程内事件总线，驱动跨 Agent 衔接 + 事件审计日志） | `src/orchestrator/eventbus.py` + `Meeting.events`（见「消息总线」一节） |
| Agent 编排配置文件（步骤/闸门/调度/provider/引擎开关） | `config/orchestration.yaml` + `src/shared/orchestration.py`（见「编排配置」一节） |
| 两次人工确认闸门（确认 + 驳回） | `src/orchestrator/gates.py` + API 的 confirm/reject 端点 |
| 前端（零构建 Web UI，API 同源托管） | `web/`（index.html / app.js / style.css，见「浏览器使用」一节） |
| 会前 Brief / 到期提醒扫描 | `MeetingFlow.prepare_brief`、`src/orchestrator/reminders.py`（APScheduler 可选驱动） |
| ① 纪要 Agent（Transcriber + NoteAnalyst） | `src/agents/minutes/` |
| ② 日程 Agent（Intent/Date/Availability/Scheduler） | `src/agents/calendar_agent/` |
| ③ 任务 Agent（Extractor/Assigner/Tracker） | `src/agents/task_agent/` |
| LLM 统一层 + Mock fallback | `src/shared/llm.py`（OpenAI 兼容协议 / httpx 直调，无 key 走规则 mock；纪要与任务抽取已接真实 LLM 主路径，见「接真实 LLM」一节） |
| 存储（内存实现，Postgres TODO） | `src/shared/db.py` |
| 工具层（日历/任务/通知，各自独立接口；飞书共用客户端） | `src/tools/calendar|tasks|notify/`、`src/tools/feishu_client.py` |
| FastAPI 服务 | `src/api/main.py` + `src/api/webhooks.py` |

流程：`start → 纪要 → Gate1 确认纪要 → 抽任务+消解负责人 → Gate2 确认任务 → 提后续会议候选 → 分发（幂等）`

三条在代码里真执行的红线：
1. 决策/风险/任务必须带真实 `segment_ids` 出处，无出处被 validator / `validate_evidence` 丢弃；
2. 负责人置信度 < 0.7 不自动派（Assigner 返回 `None`，交人工）；
3. 分发幂等：`meeting_id:segment_id` 做 idempotency key，重跑不重复建任务。

## 快速开始

### 1) Mock 模式 Demo（不需要任何 key）

```bash
cd meeting-agent
python3 examples/demo.py
```

预期输出：示例会议（支付联调/上线推迟到 11 月 8 日/证书续期）的
执行摘要、决策、风险、任务清单（含负责人、截止日期、外部 mock ID）
和后续会议候选时间（2026-10-27 起）。

### 2) 测试

```bash
pip install --user pytest pydantic fastapi httpx python-dotenv
python3 -m pytest -q
```

### 3) API 模式

```bash
pip install --user uvicorn
python3 -m uvicorn src.api.main:app --port 8000
# 建会 -> 传逐字稿 -> 处理 -> 确认纪要 -> 确认任务（分发）
curl -X POST localhost:8000/meetings -H 'content-type: application/json' -d @examples/sample_meeting.json
curl -X POST localhost:8000/meetings/m-20261023-001/process -H 'content-type: application/json' -d '{}'
curl -X POST localhost:8000/meetings/m-20261023-001/confirm-minutes
curl -X POST localhost:8000/meetings/m-20261023-001/confirm-tasks
curl -X POST localhost:8000/calendar/propose -H 'content-type: application/json' -d '{"meeting_id":"m-20261023-001"}'
```

端点：`POST /meetings`、`POST /meetings/{id}/transcript`、
`POST /meetings/{id}/audio`（multipart 上传录音，存 `ASR_UPLOAD_DIR`
下以会议 id 命名、扩展名白名单校验，`audio_url` 自动指向它，供
/process 时 ASR 转写）、
`POST /meetings/{id}/process`、`GET /meetings/{id}`、
`POST /meetings/{id}/confirm-minutes`、`POST /meetings/{id}/confirm-tasks`、
`POST /meetings/{id}/reject-minutes`、`POST /meetings/{id}/reject-tasks`、
`POST /meetings/{id}/reextract-tasks`、`PATCH /meetings/{id}/tasks/{item_id}`、
`POST /meetings/{id}/brief`、`GET /meetings/{id}/brief`、
`POST /calendar/propose`、`POST /reminders/run`、
`POST /gate-timeouts/run`、`POST /weekly-summary/run`、`GET /health`。
`/webhooks/*` **尚未实现**：
统一返回 HTTP 501 + `{"error": "not_implemented"}`，不会假装成功。

状态机保护（API 层）：重复建会 → 409；未 process 就 confirm → 409；
先 confirm-tasks 后 confirm-minutes → 409；已分发的会议再 process → 409；
重复 confirm 是幂等 no-op（200，状态不变）。只有录音、无逐字稿且 ASR
未配置的会议，process 后状态为 `needs_transcript`、`minutes` 为 null、
`warnings` 里有原因——不会生成假纪要；补传逐字稿后可重新 process。
任务分发到外部系统失败时，对应任务 `status="dispatch_failed"` 并带
`dispatch_error`，整场状态为 `dispatch_failed` / `dispatched_with_failures`，
其余任务照常分发，不会整场崩溃。

**驳回与改派**：纪要在 `awaiting_minutes_confirmation` 时可
`POST /meetings/{id}/reject-minutes {"reason": "..."}` 驳回（状态变
`minutes_rejected`，驳回记录进 `meeting.rejections`），之后
`POST /process` 重新生成纪要再走确认；任务同理用 `reject-tasks` 驳回
（状态 `tasks_rejected`），再 `POST /meetings/{id}/reextract-tasks`
基于已确认纪要重抽。错状态驳回/重抽一律 409。任务待确认时可
`PATCH /meetings/{id}/tasks/{item_id}` 改派（owner_id / due_date /
priority），改后的负责人标记 `assignment_source="manual"`，分发以改后值
为准。确认卡按钮与端点的对应见 `docs/confirmation-card.md`。

**会前 Brief**：`POST /meetings/{id}/brief` 生成会前简报（标题/时间/
参会人 + 相关历史会议的上次决策与未完成任务 + 建议议程），存
`meeting.brief`，任何状态可生成、重复生成覆盖刷新；
`GET /meetings/{id}/brief` 读取，未生成返回 404。相关历史按共享参会人
或共享标题主题从存储里筛，查不到就是空，不编造。

**到期提醒**：`POST /reminders/run` 手动触发一次扫描，返回
`{due_soon_sent, overdue_sent, mocked, failed}`；即将到期/已逾期的任务
各只提醒一次（重复扫描幂等）。自动扫描默认**关闭**：在 `.env` 里设
`REMINDERS_ENABLED=true` 后，API 进程会用 APScheduler 每
`REMINDER_SCAN_INTERVAL_MIN`（默认 30）分钟扫一次，提前量
`REMINDER_LEAD_HOURS`（默认 24）；调度器启动失败只降级打 warning，
不影响 API 本身。通知通道由 `NOTIFY_PROVIDER` 决定，mock 时发送结果
计入 `mocked`，不假装真送达。

**确认闸门超时**：会议卡在待确认状态超过 `GATE_TIMEOUT_HOURS`
（默认 48）小时，按 `GATE_TIMEOUT_ACTION` 处理——`notify`（默认）
发一条升级提醒（注明会议、卡在哪个闸门、已等待时长），同一次进入
闸门只发一次；`reject` 则转入既有的 `minutes_rejected` /
`tasks_rejected` 并在驳回记录里写明 confirmation timeout，之后走
既有重跑/重抽路径。**超时绝不自动确认**。手动触发：
`POST /gate-timeouts/run`，返回本次动作清单（会议 id、闸门、动作、
是否真实送达）；自动扫描默认关闭，设 `GATE_TIMEOUTS_ENABLED=true`
后每小时（`GATE_TIMEOUT_SCAN_INTERVAL_MIN`）扫一次。

**任务周报**：`POST /weekly-summary/run`（可带 `?window_days=7`）
汇总全部会议近 7 天的任务（本周到期未完成 / 逾期未完成 / 未定日期
仍开放 / 本周已完成，正文沿用 `TaskTracker.weekly_summary` 的格式，
条目带会议标题），经通知通道发送，并返回
`{period_start, period_end, summary, delivered, delivery_note}`——
未真送达时 `delivered=false` 并写明原因，周报全文照常返回不丢失。
自动发送默认关闭，设 `WEEKLY_SUMMARY_ENABLED=true` 后每周一 09:00
（`DEFAULT_TIMEZONE`）自动生成发送。

### 4) Docker（可选，骨架模式不需要）

```bash
docker compose up   # api 默认连 compose 内的 postgres（持久化模式）
# .env 可选：compose 里 env_file 已设 required: false，
# 没有 .env 也能起；要配 key 时再 cp .env.example .env
```

## 持久化（内存 / Postgres 两种模式）

会议数据的存储由 `DATABASE_URL` 一个开关决定（`src/shared/db.py`
的 `make_store` 工厂，进程内只有一个 store 实例）：

| 模式 | 怎么开 | 行为 |
|---|---|---|
| 内存（默认） | `DATABASE_URL` 留空 | `InMemoryDB`：零依赖，demo/测试行为不变；**进程重启数据全丢** |
| Postgres | `DATABASE_URL=postgresql://user:pass@host:5432/db` | `PostgresDB`：重启不丢；连接失败直接抛带目标 host/db 的 RuntimeError，绝不静默回退内存 |

其他 scheme（如 sqlite://）会明确报错，不会被吞成内存模式。

表结构一句话：一张 `meetings(id TEXT 主键, status TEXT, data JSONB,
updated_at TIMESTAMPTZ)` 表，整个 Meeting 对象序列化进 `data`，
`status` 抽列方便按状态筛；表在首次连接时自动建（`CREATE TABLE IF
NOT EXISTS`），不需要迁移脚本。当前查询只有按 id 取、列全部、按
状态筛，JSONB 文档式最贴合；将来出现按负责人查任务等需求再拆列。

docker compose 部署时不用自己填：api 服务的 `environment` 已默认把
`DATABASE_URL` 指向 compose 的 postgres 服务（用户/库名/密码与该
服务定义一致）。本地直跑 API 时把 `.env.example` 里的示例 URL
填进 `.env` 即可。

接口契约测试在 `tests/test_db_postgres.py`：同一套断言参数化跑
两种实现；设了 `TEST_DATABASE_URL` 才真连 Postgres，未设时
Postgres 用例 skip（默认 pytest 不受影响）。

已知限制：提醒扫描的防重复标记（`reminder_sent_at` 等）存在
Meeting 的 JSON 文档内，单进程内幂等；多个 API 实例同时扫描时，
两个进程可能都读到「未发送」状态而重复发一次。根治需要行锁或
独立的提醒标记表，列为下一期。

## 接真实 LLM（纪要 + 任务抽取已接通）

纪要 Agent 的主路径已经是真实 LLM：`NoteAnalyst` 在 LLM 可用时先走
`prompt → chat_json → Pydantic 校验 → 证据过滤（编造的 segment_id 直接丢弃）`，
首次输出不合格会把校验错误回喂模型 **repair 重试 1 次**，再失败才回退规则，
全程不会抛崩。每份纪要都带溯源字段，demo 和 API 返回里能直接看到：

- `generation_source`：`llm`（一次成功）/ `llm_repaired`（repair 后成功）/ `rule_fallback`（LLM 试过但失败，回退规则）/ `rules`（没配 LLM，纯规则）
- `generation_model`：实际使用的模型名

任务抽取也走同一套 LLM 配置（同一个 `LLMClient`、同一组 `.env` 配置项），
模式与纪要完全镜像：`ActionItemExtractor` 在 LLM 可用时先走 LLM 抽取，同样带
1 次 repair、失败回退规则，溯源字段是 `tasks_generation_source` /
`tasks_generation_model`（demo 里和纪要那行并列打印）。分工红线不变：LLM 只抽
`title / owner_raw / due_raw / priority / source_segment_ids`，它返回的
`owner_id` 一律丢弃（负责人消解只走 Assigner），`due_date` 不信 LLM 的换算、
优先由 `date_parser.parse_due(due_raw, 会议日期)` 计算，LLM 的日期只作兜底。

质量边界（第七轮，源于 2026-10-23 方舟真机实测的两个瑕疵）：纪要 prompt 明确
「决策 ≠ 待办」边界，校验链会把疑似分派句的 decisions 喂进 repair 复判（绝不
静默删除，复判后仍保留的会记入 `generation_notes` 并同步到会议 `warnings`）；
任务抽取 prompt 排除「会议安排类事项」（归日程 Agent），且所有抽取路径都过一
道确定性安全网过滤这类标题，被过滤的条目逐条记入会议 `warnings`，不静默消失。

客户端是 OpenAI 兼容协议（`POST {LLM_BASE_URL}/chat/completions`，httpx 直调，
**不需要装 litellm / openai SDK**），OpenAI、DeepSeek、通义千问 DashScope 兼容模式、
火山方舟、Moonshot 和本地 Ollama 都用同一套配置切。

### 1) 先用假服务离线验证链路（不需要任何真 key）

```bash
cd meeting-agent
python3 examples/fake_llm_server.py --port 8765 &   # 假 OpenAI 兼容服务
LLM_MODE=llm LLM_BASE_URL=http://127.0.0.1:8765/v1 LLM_API_KEY=test LLM_MODEL=fake-model \
  python3 examples/demo.py --mode llm
kill %1
```

预期：输出里 `Generation: source=llm` 且 `Tasks Generation: source=llm`，
执行摘要是假服务返回的干净版（风险只有「支付渠道证书过期」一条，不会像
规则版把开场白当风险），任务标题也是假服务的干净版（如「完成支付渠道证书
续期并提交联调」）。假服务按 prompt 内容自动区分纪要 / 任务请求。

### 2) 切真实供应商（改本地 .env 三行）

```bash
cp .env.example .env
# 编辑 .env：从文件头 6 个预设里选一个（OpenAI / DeepSeek / 通义千问 /
# 火山方舟 / Moonshot / Ollama 本地），只改这三类行：
#   LLM_MODEL=<预设里的模型名>
#   LLM_BASE_URL=<预设里的端点>
#   LLM_API_KEY=<你自己的 key>     # Ollama 本地免 key，并把 LLM_MODE 改成 llm
python3 examples/demo.py --mode llm   # 或不带 --mode，LLM_MODE=auto 有 key 也会走真实
```

配置项（`src/shared/config.py`）：`LLM_MODE`（auto|mock|llm）、
`LLM_BASE_URL`、`LLM_TIMEOUT`、`LLM_TEMPERATURE`。`LLM_SMALL_MODEL` 是
**预留项，尚未接线**（config 里有 RESERVED 注释），填了暂不生效。

> ⚠️ **key 只放本地 `.env`，不要提交 git、不要写进代码或发到群里。**
> `.env` 已在 `.gitignore` 里（可打开 `.gitignore` 确认有 `.env` 一行）；
> 提交前用 `git status` 确认 `.env` 不在待提交列表里。不慎提交过 key 的，
> 第一时间去供应商控制台吊销重发，光删提交记录不算处理完。

相关测试：`tests/test_llm_minutes.py`（注入假 transport：合法 JSON 被采纳、
围栏 JSON 解析、编造 segment_id=999 被过滤、repair 后 `source=llm_repaired`、
彻底失败回退规则），全离线、不发真实网络。任务抽取的对应测试在
`tests/test_llm_tasks.py`：同样全离线注入假 transport，另外锁死两条红线——
LLM 夹带的 `owner_id` 被丢弃、负责人仍由 Assigner 解析；LLM 给的
`due_date=2099-01-01` 会输给 `date_parser` 按 `due_raw`（如「明天」）算出的日期。

## 接真实 ASR（录音转逐字稿已接通，双引擎）

`src/agents/minutes/transcriber.py` 已接两个真实引擎，按
`ASR_ENGINE` 二选一：本地 `faster-whisper` 与云端 `volcano`（豆包
录音文件识别）。会议只有 `audio_url`、没有逐字稿时，/process 会先
真实转写再生成纪要。默认仍是 `ASR_ENGINE=mock`（不转写，会议停在
`needs_transcript` 且 warning 写明原因）。

### 本地引擎：faster-whisper

开启只需两步：

```bash
pip install -e ".[asr]"     # faster-whisper 是可选依赖，主安装保持轻量
# .env 里改一行：
ASR_ENGINE=faster-whisper
```

配置项（`src/shared/config.py`，均有 `.env.example` 注释）：`ASR_MODEL`
（默认 `small`）、`ASR_DEVICE`（默认 `cpu`）、`ASR_COMPUTE_TYPE`
（默认 `int8`）、`ASR_LANGUAGE`（默认 `zh`，`auto` 为自动检测）、
`ASR_UPLOAD_DIR`（默认 `data/uploads`）。模型在**第一次转写时**才
下载/加载（small 约 0.5 GB，缓存到 `~/.cache/huggingface`），import
代码不会触发下载。转写失败（包未装、模型加载失败、音频下载不到、
文件不存在）不会打崩流程：仍走 `needs_transcript` 契约，且 warning
里带具体原因和修法。

录音进来有三种方式：建会时给 `audio_url`（URL 会自动下载、本地
路径直接读）、调 `POST /meetings/{id}/transcript` 传逐字稿、或调
`POST /meetings/{id}/audio` 上传录音文件（mp3/wav/m4a/flac/ogg/
webm/mp4，文件名只由会议 id + 扩展名生成，不拼用户文件名）。

**速度预期（本机实测，2 核 CPU、small/int8）**：11 秒英文音频转写
约 8–11 秒，9 秒中文音频约 13 秒，大致 1–1.4 倍实时；模型首次加载
另约 10 秒（含校验缓存）。长会议（如 1 小时录音）按这个比例预估，
或改用 `ASR_MODEL=base` 提速、GPU + `float16` 提速提质；`large-v3`
只建议在有 GPU 时用。

**说话人分离未做（仅本地引擎）**：pyannote 没有集成，faster-whisper
段落的 `speaker` 是统一占位「发言人」，会议 `warnings` 里会明确标注
这一点——纪要里凡是依赖“谁说的”的归因，对本地转写逐字稿要人工复核。

真实验收：`python3 examples/asr_smoke.py`（JFK 英文样本断言
"fellow Americans"、edge-tts/gTTS 合成的中文样本断言「上线」「证书」，
样本与转写结果在 `examples/asr_samples/`）；离线测试在
`tests/test_asr.py`（注入假 engine，不触发模型下载）。

### 云端引擎：豆包录音文件识别（volcano）

```bash
# .env 里配（凭据来源与每个键的说明见 .env.example 的 VOLCANO_ASR 段）：
ASR_ENGINE=volcano
VOLCANO_ASR_APP_ID=...
VOLCANO_ASR_ACCESS_TOKEN=...
```

异步提交/查询协议：提交后拿任务 id 轮询（间隔
`VOLCANO_ASR_POLL_INTERVAL_SEC` 默认 5 秒，总超时
`VOLCANO_ASR_TIMEOUT_SEC` 默认 1800 秒）。**任务 id 在提交成功的
那一刻就写入 `meeting.asr_task_id` 并落库**，进程中途挂掉也能凭 id
续查，不会丢任务。状态码在响应头里，静音音频会以「音频中未检测到
人声」落到 `needs_transcript` 的 warning，不会假装转写成功。

两条硬约束（实测换来的，客户端先拦，不等服务端报错）：

1. **只接受公网可访问的音频 URL，不收文件**。`audio_url` 是本地
   路径时直接报错并指引改用 faster-whisper 或先把文件放到对象
   存储等公网地址；上传端点存下来的本地录音因此要用云端引擎，
   需先自行托管出 URL。
2. **格式只支持 wav/mp3/ogg/spx/amr/aac/m4a**（按 URL 后缀判断，
   无后缀默认 wav 并在 warning 注明）。flac 不在支持列表内——提交
   flac 不会被拒，而是服务端挂起直到超时，客户端先行拦下。

另注意音频托管位置的可达性：音频放在慢速海外托管时任务会长时
间挂起，换到国内可达的 CDN 后 3 秒音频约 10 秒出结果——这是托管
可达性问题，不是协议问题。

`VOLCANO_ASR_SECRET_KEY` 本接口**未用到**（鉴权只用 App ID +
Access Token），它属于旧版/其他火山语音接口，留在配置里只是让一
整套凭据同放一份 `.env`。

### 双引擎怎么选

| | faster-whisper（本地） | volcano（云端豆包） |
|---|---|---|
| 运行位置 | 本机进程内，离线可用 | 火山云端，需联网调 API |
| 音频来源 | 本地文件、http(s) 下载、上传端点 | **仅公网 URL** |
| 说话人分离 | 无（统一占位 + warning 标注） | 有（说话人1/说话人2…） |
| 速度（本机实测/实测） | 约 0.8–1.4 倍实时（small/int8，2 核 CPU） | 异步排队+轮询；短音频约 10 秒级，长音频不用占本地 CPU |
| 成本/依赖 | 装 `.[asr]` extra + 首次下载模型 | 按量调用云端 API，需 VOLCANO_ASR 凭据 |
| 适合 | 录音已在本地/内网、不能出网 | 录音已有公网地址、要说话人区分、长音频批量 |

离线测试在 `tests/test_asr_volcano.py`（MockTransport 脚本化
提交/查询状态机，含两条硬约束与 task id 落库断言）。

## 飞书真实 API（日历 + 任务 + 通知已实现）

飞书三件套已经从 stub 换成真实 OpenAPI 调用，共用
`src/tools/feishu_client.py`（tenant token 内存缓存并提前刷新、统一
`FeishuAPIError`、httpx 代理处理与 LLM 客户端一致）：

- **日历 Calendar v4**：创建/查询/更新/删除日程、freebusy、单独添加参会人；
  时间戳是**秒**。`FEISHU_CALENDAR_AUTH=tenant`（默认）只能写应用日历，
  `=user` 用 `FEISHU_USER_ACCESS_TOKEN` / `FEISHU_REFRESH_TOKEN` 写已授权
  用户的日历。必须配 `FEISHU_CALENDAR_ID`，飞书没有 `primary` 这个约定。
- **任务 Task v2**：tenant token 创建并按 open_id 指派、PATCH 更新/完成、评论；
  due 时间戳是**毫秒**（与日历不同，代码和测试已分别锁死）。
- **通知**：自定义机器人 webhook 发 interactive 卡片；配
  `FEISHU_WEBHOOK_SECRET` 时按飞书规范加 `timestamp/sign` 签名。
- **用户映射**：`FEISHU_USER_MAP={"u-lisi":"ou_xxx"}` 把内部负责人 ID
  映射到 open_id；未映射/非 `ou_` 的参会人会被跳过并在结果 `warnings`
  中列明，不会让整场建会失败。

`.env` 需要的核心键：`FEISHU_APP_ID`、`FEISHU_APP_SECRET`、
`FEISHU_CALENDAR_ID`、`FEISHU_CALENDAR_AUTH`、`FEISHU_USER_ACCESS_TOKEN`、
`FEISHU_REFRESH_TOKEN`、`FEISHU_WEBHOOK_URL`、`FEISHU_WEBHOOK_SECRET`、
`FEISHU_USER_MAP`，并把 `CALENDAR_PROVIDER` / `TASK_PROVIDER` /
`NOTIFY_PROVIDER` 切为 `feishu`。逐步开通权限、获取 calendar_id/open_id、
两种日历模式取舍和联调顺序见 **[docs/feishu-setup.md](docs/feishu-setup.md)**；
填好真实凭据后可运行 `python3 examples/feishu_smoke.py` 做真机联调
（会产生带 `[smoke]` 前缀的真实任务/日程，本仓库交付时**未运行**该脚本）。

> 本环境的验证级别：已用 httpx MockTransport 验到请求级（路径、请求体、
> 秒/毫秒时间戳、签名、token 复用与刷新、错误码降级）；**尚未用真实
> 飞书租户凭据做真机验证**，真机结论以 `feishu_smoke.py` 的输出为准。

## 如何切真实服务

| 想切什么 | 改哪里 | 做什么 |
|---|---|---|
| 真实 LLM | `.env` | ✅ 纪要 + 任务抽取均已接通，见上一节「接真实 LLM」：填 `LLM_MODEL` / `LLM_BASE_URL` / `LLM_API_KEY` 三行即可（httpx 直调 OpenAI 兼容协议，不用装 litellm），两个 Agent 共用同一套配置，抽取结果同样必须过出处校验 |
| 真实 ASR | `src/agents/minutes/transcriber.py` | ✅ 双引擎，见上一节「接真实 ASR」：本地 faster-whisper（装 `.[asr]` extra + `ASR_ENGINE=faster-whisper`；无说话人分离，speaker 为占位并在 warnings 标注）或云端豆包 `ASR_ENGINE=volcano`（仅公网 URL，自带说话人分离，task id 提交时落库） |
| 飞书（国内队一站式） | `src/tools/feishu_client.py`、`src/tools/calendar/feishu.py`、`src/tools/tasks/feishu_task.py`、`src/tools/notify/feishu.py` | ✅ 已实现真实 OpenAPI；按 [docs/feishu-setup.md](docs/feishu-setup.md) 填 App 凭据、calendar_id、user map 和 webhook 后切三个 provider 为 `feishu`，再用 `examples/feishu_smoke.py` 真机联调 |
| Google（海外队） | `src/tools/calendar/google.py` + `linear.py` / `trello.py` / `jira.py` + `notify/slack.py` | 填对应 token，按各文件头 API 路径实现 TODO 方法 |
| CrewAI Flows | `src/orchestrator/flow.py` | 文件头有可选导入注释；Agents 本身与框架无关，只需把 `step_*` 映射到 Flow 的 `@start/@listen` |
| Postgres | `src/shared/db.py` | ✅ 已实现（psycopg + JSONB 单表，见上文「持久化」一节）：填 `DATABASE_URL=postgresql://...` 即切换，留空为内存模式 |

未配置任何外部 token 时，工具层一律返回带 `"mock": true` 的结果并打
warning 日志，**不会假装调用成功**（通知 mock 的 `delivered` 为 `false`）。

`TASK_PROVIDER` / `CALENDAR_PROVIDER` / `NOTIFY_PROVIDER` 已通过
`src/tools/factory.py` 接线：provider=mock 时保持内置 mock 行为；
飞书 provider 是真实调用，凭据不全时对应工具维持 mock 并打 warning；
其他选了真实 provider 但方法还没实现的 stub，在配了凭据时对应任务会
变成 `dispatch_failed`（带错误信息）、日历建会会写入 `meeting.warnings`，
都不会把流程打崩。会前 Brief、Tracker 到期提醒（APScheduler 可选
调度 + `POST /reminders/run` 手动触发）、确认闸门驳回/改派均已接线
（见上文 API 一节）。仍未实现（不吹已实现）：
说话人分离（pyannote，volcano 云端引擎自带分离除外）、
Google/Linear/Trello/Jira/Slack 等外部 API
的真实调用（stub 配了凭据会明确报错并被降级）、
`LLM_SMALL_MODEL`（预留）。飞书真实调用除请求级离线
验证外，已用用户自建应用凭据完成真机验证（token/日历/任务，见
AUDIT 第三轮补充）；通知 webhook 仍待群机器人地址做真机验证。
细节见根目录 `AUDIT.md`。

## 消息总线（事件驱动协同）

Agent 之间不只是顺序调用：管线关键节点发布到进程内事件总线
（`src/orchestrator/eventbus.py`），跨 Agent 的两段衔接由订阅驱动——
纪要确认 → 任务抽取、任务确认 → 日程提议 + 分发，都走总线。
每个事件在发布前先追加到 `meeting.events`（审计日志，随会议一同
持久化，Postgres JSONB 兼容），前端详情页有事件时间线可视化。

| 事件 | 发布点 | 订阅方 |
|---|---|---|
| `meeting.created` | API 建会 / flow.run 首次运行 | —（审计） |
| `brief.generated` | `MeetingFlow.prepare_brief` | —（审计） |
| `transcript.ready` | ASR 转写完成 / API 提交逐字稿 | —（审计） |
| `minutes.generated` | `MeetingFlow.step_minutes` | —（审计） |
| `gate.minutes.entered` / `gate.tasks.entered` | flow 停在闸门时 | —（审计，闸门超时扫描另行计） |
| `minutes.confirmed` | flow.run 闸门通过 / `flow.confirm_minutes` | **MeetingFlow 处理函数 → TaskAgent 抽取任务** |
| `minutes.rejected` / `tasks.rejected` | `flow.reject_*`（API 驳回端点） | —（审计） |
| `tasks.extracted` | `MeetingFlow.step_tasks` | —（审计） |
| `tasks.confirmed` | flow.run 闸门通过 / `flow.confirm_tasks` | **MeetingFlow 处理函数 → 日程提议 + 分发** |
| `schedule.proposed` | `MeetingFlow.step_calendar` | —（审计） |
| `tasks.dispatched` / `dispatch.failed` | `MeetingFlow.step_dispatch` | —（审计） |
| `reminders.sent` | API 提醒扫描包装层（按邮戳差分识别实际发送的会议） | —（审计） |
| `gate.timeout` | API 闸门超时扫描包装层 | —（审计） |
| `weekly_summary.generated` | API 周报包装层（全进程事件，不属于单个会议，只发布不落单会日志） | —（审计） |

设计取舍：为什么是进程内同步总线而不是外部消息队列——三个 Agent
同进程、共享一个 Meeting 状态对象，引入 broker 只增加部署依赖与
失败模式；总线在这里买到的是**解耦**（发布方不直接调用下游
Agent）与**审计**（每会一条事件时间线）。失败语义是双轨的：审计
类订阅者抛错被**隔离**（记录在 `bus.errors`，不阻断其余订阅者与
发布方）；但两段跨 Agent 衔接是 **critical 订阅**——下游抛错在
记入 `bus.errors` 与 `meeting.warnings` 后**向确认方传播**（API
返回 500），且确认标志只在衔接成功后才置位，失败后可直接重试，
不会出现「闸门已确认、任务却没抽出来」的静默不一致。payload 只放
计数/id/状态摘要，逐字稿全文与凭据永不入 payload（emit 还会把
形似凭据的键名打码）。Agent 内部的步骤（转写、负责人消解）仍是
直接调用——总线承载管线生命周期，不承载 Agent 内务。

## 编排配置（config/orchestration.yaml）

`config/orchestration.yaml` 是独立的 Agent 编排配置文件，管**结构
开关**；凭据与数值覆盖仍走环境变量。加载器在
`src/shared/orchestration.py`，优先级（高 → 低）：

1. 环境变量（`CALENDAR_PROVIDER`、`ASR_ENGINE`、`LLM_MODE`、
   `REMINDERS_ENABLED`、`GATE_TIMEOUT_*`、`ORCH_STEP_<步骤>_ENABLED`、
   `ORCH_GATE_<闸门>_ENABLED` 等一旦设置即以它为准）
2. `config/orchestration.yaml`（或环境变量 `ORCHESTRATION_CONFIG`
   指向的文件）
3. 代码内置默认（与仓库内 yaml 的当前内容一致）

文件缺失不是错误（按内置默认启动，行为与无此文件前完全一致）；
文件写坏（YAML 语法/类型错误）启动时明确报错，不静默忽略。
数值同样在加载时校验并点名报错：`interval_min` 须在 1..1440、
`gates.*.timeout_hours` 须在 (0, 720] 小时、`scheduler.*.cron`
须能被 APScheduler 解析——非正的超时阈值会让所有待确认闸门被误判
超时（扫描入口另有第二道防御：生效阈值 ≤ 0 直接拒绝执行）。

| 配置段 | 内容 | 生效点 |
|---|---|---|
| `pipeline.steps` | brief / transcribe / minutes / tasks / schedule / dispatch 六步开关 | `MeetingFlow`：关掉的步被跳过且在 warnings 留痕（如 dispatch 关闭 → 停在 `tasks_confirmed` 不外发；tasks 关闭 → 纪要确认后停在专用状态 `minutes_confirmed`，没有待确认的任务） |
| `gates.minutes/tasks` | 两闸门 `enabled` 与 `timeout_hours` | flow 闸门判定 + 闸门超时扫描阈值（环境变量 `MINUTES_/TASKS_GATE_TIMEOUT_HOURS`、`GATE_TIMEOUT_HOURS` 可覆盖） |
| `scheduler` | reminders / gate_timeouts / weekly_summary 三作业的 enabled 与 interval/cron | API lifespan 的调度器注册（`*_ENABLED` 环境变量优先） |
| `providers` | calendar / task / notify 的 provider 名 | `Settings` 的默认值（对应环境变量未设置时）→ tools/factory |
| `engines` | asr（mock/faster-whisper/volcano）、llm_mode（auto/mock/llm） | `Settings` 的默认值（对应环境变量未设置时） |

⚠️ 闸门 `enabled: false` 的语义不是「静默自动确认」：流转会自动
通过，但每次都在 `meeting.warnings` 与确认事件
（`via=config_gate_disabled`）里留痕，表明这是配置选择而非人工
确认。评审/演示时现场改 yaml 即可验证开关真实生效（有单测覆盖：
关 brief 不生成、关 dispatch 停在 tasks_confirmed、关纪要闸门
自动通过并留痕、坏 yaml 报错）。

## 浏览器使用（前端）

零构建前端在 `web/`（原生 HTML/CSS/JS，无 CDN，离线可开），由
FastAPI 同源托管：

```bash
python3 -m uvicorn src.api.main:app --port 8000
# 浏览器打开 http://localhost:8000/
```

演示动线：左侧「载入示例（内置小样）」一键建会 → 详情页点
「处理会议」停在闸门 1 → 看纪要与证据段（点决策后的 `#id` 徽章
高亮对应逐字稿）→ 「确认纪要」后任务被抽取 → 在任务表改派一条
→ 「确认任务」完成分发 → 底部事件时间线核对总线日志；顶部工具区
可手动触发提醒扫描、闸门超时扫描与周报。所有按钮都直调后端
API，409 等错误会把后端原文弹出来。前端调用的每个路径都有单测
与后端路由表逐一比对（`tests/test_web_ui.py`），不会前后端漂移。
会议 id 只能用英文字母、数字与 `.` `_` `-`（它要进 URL 路径与
文件名）：后端建会时校验、违规返回 422，前端拼路径时一律过
`encodeURIComponent`，两端一起防「建得出来点不开」的 id。

## 路线图 Phase 对应文件

| Phase | 内容 | 主要文件 |
|---|---|---|
| Phase 0 骨架（已完成） | 契约、规则 Agent、Mock、Demo、测试 | 全部 `src/shared`、`src/agents`、`examples/`、`tests/` |
| Phase 1 MVP 纪要+任务（1-2 周） | 真实 ASR、真实 LLM 替换规则、接 1 个任务系统+1 个通知通道、确认清单 Web 页 | `minutes/transcriber.py`、`shared/llm.py`、`tools/tasks/*`、`tools/notify/*`、`api/main.py`（LLM、真实 ASR、飞书任务/通知与 Web 确认页均已接，见「浏览器使用」一节） |
| Phase 2 日程闭环（3-4 周） | 会前 Brief、接 1 个日历、IM 交互确认卡、Tracker 定时提醒 | `calendar_agent/*`、`tools/calendar/*`、`orchestrator/reminders.py`（Brief、提醒与飞书真实日历已接线，余下 IM 卡片回调/webhook）、`docs/confirmation-card.md` |
| Phase 3 记忆与主动性（第 2 月） | pgvector 跨会检索、系列会议任务结转、负责人学习、评估迁 LangGraph | `shared/db.py`（Postgres TODO）、`orchestrator/flow.py` |

## 目录

```
meeting-agent/
├── pyproject.toml  docker-compose.yml  .env.example
├── config/             orchestration.yaml（Agent 编排配置：步骤/闸门/调度/provider/引擎）
├── web/                index.html, app.js, style.css（零构建前端，API 同源托管）
├── src/
│   ├── orchestrator/   flow.py, eventbus.py, gates.py, reminders.py, gate_timeouts.py, weekly_summary.py
│   ├── agents/         minutes/  calendar_agent/  task_agent/
│   ├── shared/         schema.py, config.py, db.py, llm.py, orchestration.py
│   ├── tools/          calendar/  tasks/  notify/  feishu_client.py
│   └── api/            main.py, webhooks.py
├── examples/           sample_meeting.json, demo.py, fake_llm_server.py, feishu_smoke.py
├── tests/              test_date_parser / test_assigner / test_extractor / test_flow_e2e / test_llm_minutes / test_llm_tasks / test_api / test_calendar_agent / test_dispatch_resilience / test_brief / test_reminders / test_rejection / test_feishu_tools / test_db_postgres / test_asr / test_asr_volcano / test_gate_timeouts / test_weekly_summary / test_eventbus / test_orchestration / test_web_ui
├── AUDIT.md            完整性审计记录（发现 → 严重程度 → 处置）
└── docs/               confirmation-card.md, feishu-setup.md
```
