# 进度日志

本文件只追加，不改写历史。（项目 2026-10-23 至 2026-10-24 的九轮审计
记录见 AUDIT.md；本文件自 2026-10-25 起承接后续轮次。）

## 2026-10-25 - Task: 修复独立审查发现的四个高优先级问题（鉴权缺失 / 任务 ID 撞号 / 音频无上限 / 云 ASR 阻塞请求）

### What was done

1. **任务 ID 撞号修复**：LLM 抽取的两条不同任务引用同一段逐字稿时，
   旧逻辑让它们共享同一 id 和同一幂等键，分发到飞书时第二条会静默
   复用第一条的外部任务（真实丢任务）。现在同一次抽取内第二个起加
   `-2`/`-3` 后缀，第一条保持原稳定 ID（与规则路径同形，切换路径不
   重复建外部任务）。
2. **音频大小上限**：上传端点改为分块流式写盘，超过 100MB（
   `MAX_AUDIO_UPLOAD_BYTES`）即 413 并删除半成品；远程音频下载同样
   加 100MB 上限（`MAX_AUDIO_DOWNLOAD_BYTES`），超限报错并清理临时
   文件。上传不再把整个请求体读进内存。
3. **上传替换语义**：同一会议重新上传录音时删除旧扩展名的孤儿文件，
   并清空 `meeting.asr_task_id`（新录音绝不续查旧录音的云端任务，
   也是云端任务永久失败后的逃生门）。
4. **HTTP Basic 访问认证**：`BASIC_AUTH_USERNAME`/`BASIC_AUTH_PASSWORD`
   两项都设置即对整个服务（API + 网页 UI）启用 Basic 认证，凭据用
   `secrets.compare_digest` 比较，仅 `/health` 豁免供探活；任一为空
   = 关闭（本机 demo/测试零配置不变），未启用时启动日志有醒目
   warning。浏览器原生弹窗同时覆盖页面与同源 fetch，前端零改动。
5. **云 ASR（volcano）有界等待 + 续查**：单次 `/process` 最多同步等待
   `VOLCANO_ASR_SYNC_WAIT_SEC`（默认 60 秒，硬上限取
   min(SYNC_WAIT, TIMEOUT)）；超预算仍 pending 就诚实返回「仍在处理
   中」的 warning，不再把请求线程挂住最长 30 分钟。任务 ID 本就在
   提交时落库，再次 `/process` 按该 ID 续查、不重复提交；长录音靠
   多次 /process 收敛。
6. 文档同步：README 增加访问认证章节并改写 volcano 轮询语义；
   `.env.example` 增补三个新变量说明；新增 docs/security.md 汇总
   以上行为。

### Testing

- 新增 `tests/test_high_priority_fixes.py`（8 个用例）：同首段双任务
  的 ID/幂等键唯一性断言；上传 413 + 半成品清理断言；上传替换孤儿
  清理 + 任务 ID 失效断言；下载上限断言；鉴权启用（401/健康豁免/
  错误密码/畸形头/正确凭据放行）与默认关闭断言；volcano 续查不发
  submit、会议级续查流程出纪要断言。
- `tests/test_asr_volcano.py` 中原「轮询超时抛错」用例按新契约改写
  为「超预算诚实返回 pending + 保留任务 ID」。
- 全量回归：`pytest -q` → **207 passed, 7 skipped**（修复前基线
  199 passed, 7 skipped；新增 8 例全过，无回归）。
- 验证环境：Python 3.12.10 + requirements.txt 依赖的临时 venv。

### Notes

改动文件清单（回滚：整体回退到提交 `8575aec`；逐项回滚见下）：

- `src/agents/task_agent/extractor.py` —— 抽取条目 ID/幂等键去重后缀。
  回滚单项：还原该文件即可，其余修复不受影响。
- `src/api/main.py` —— 上传分块写盘 + 大小上限 + 孤儿清理 +
  asr_task_id 失效 + Basic 认证中间件与启动日志。
- `src/agents/minutes/transcriber.py` —— 下载上限；volcano 有界轮询
  （pending 超预算返回而非挂起）+ `transcribe_url(resume_task_id=)`
  续查入口。
- `src/agents/minutes/agent.py` —— 转写调用带上
  `resume_task_id=meeting.asr_task_id`。
- `src/shared/config.py` —— 新增 `volcano_asr_sync_wait_sec` 与
  `basic_auth_username/password` 三个设置项。
- `tests/test_high_priority_fixes.py`（新增）、
  `tests/test_asr_volcano.py` —— 回归测试与契约更新。
- `README.md` / `.env.example` / `docs/security.md`（新增）—— 文档
  同步。回滚文档不影响运行行为。

行为变化提醒：volcano 云 ASR 默认不再长阻塞（等待上限 60 秒）；鉴权
默认关闭（部署前按 docs/security.md 启用）。其余行为不变。

## 2026-10-25 - Task: 修复 security-audit 聚焦审查的四个确认发现（fail-closed 启动 / CSRF / SSRF / compose 端口）

### What was done

按 security-audit skill 的聚焦审查结论（F1-F4 confirmed），逐项修复：

1. **F1 fail-closed 启动**：新增 `REQUIRE_AUTH` 环境变量。设为 true 且
   `BASIC_AUTH_USERNAME/PASSWORD` 缺任一时，应用启动直接失败（不再
   静默以无鉴权状态运行）——杜绝"忘了配凭据就暴露"的配置事故。
2. **F2 CSRF 防护 + 防嵌入**：新增 `csrf_and_frame_guard` 中间件。对
   POST/PUT/PATCH/DELETE：`Origin` 与（转发后）Host 不匹配 → 403；
   `Sec-Fetch-Site: cross-site`（无 Origin 也拦）→ 403。无这些头的
   请求（curl/脚本/服务端 webhook）与同源浏览器请求不受影响。全部
   响应带 `X-Frame-Options: DENY` + `Content-Security-Policy:
   frame-ancestors 'none'`（防 clickjacking）。此前本地实证的"跨站
   表单 POST 可驱动 confirm-* 直至外部分发"路径已被阻断。
3. **F3 SSRF 防护**：服务端音频下载改为手动重定向循环（最多 5 跳），
   每一跳发请求前解析并校验目标地址——环回/私网/link-local/保留/
   多播一律拒绝（覆盖字面 IP 与域名解析结果，含 DNS rebinding 形态）；
   域名解析失败按 fail-closed 拒绝。所有下载错误信息不再包含目标
   URL/主机名（warning 可读，内网拓扑不经此泄露）。
4. **F4 compose 端口收敛**：postgres/redis 不再发布宿主端口（原
   5432/6379），仅 compose 内网互通；本地调试注释了绑回环的写法。
5. 文档同步：`.env.example` 增 `REQUIRE_AUTH`；README 认证章节补
   fail-closed 与两个新防护；docs/security.md 重写为两轮加固汇总。

F5（依赖未锁版本）/F6（auto_approve 直达路径）为加固级观察、不构成
边界跨越，本轮不改代码，已在 docs/security.md 与审查记录中说明。

### Testing

- 新增 `tests/test_security_audit_fixes.py`（14 个用例）：REQUIRE_AUTH
  缺凭据启动抛错/配齐正常启动；跨站 Origin 与 Sec-Fetch-Site 被 403
  且会议状态不变；同源与无头请求放行（走通到 dispatched）；响应帧
  防护头断言；5 类非公网目标 + DNS rebinding + 重定向跳私网全部在
  发请求前拦截（transport 命中即失败），公共地址下载正常，错误信息
  无拓扑泄露；compose 端口移除断言。
- `tests/test_asr.py` 两个 URL 下载用例与
  `test_high_priority_fixes.py` 的下载上限用例补确定性 DNS 桩
  （SSRF 防护引入域名解析后，保持离线确定性；本机真实 DNS 曾把
  example 域名解析进保留段被新防护正确拦截）。
- 全量回归：`pytest -q` → **222 passed, 7 skipped**（上轮基线
  207 passed；新增 15 例全过，无回归）。
- CSRF 修复的直接验证：上轮实证脚本同一形态（跨站表单 POST + 环境
  凭据）现返回 403，会议状态不变。

### Notes

改动文件清单（回滚：整体回退到提交 `8575aec`；逐项回滚见下）：

- `src/shared/config.py` —— 新增 `require_auth` 设置项。单项回滚：
  还原该文件即可关闭 fail-closed，其余不受影响。
- `src/api/main.py` —— REQUIRE_AUTH 启动检查 + csrf_and_frame_guard
  中间件。单项回滚：还原后 CSRF 防护与帧头消失，其余行为不变。
- `src/agents/minutes/transcriber.py` —— `_assert_public_http_host`
  防护 + `_download` 手动重定向循环与错误去拓扑化。单项回滚：还原
  后回到旧的自动重定向行为（SSRF 面重新打开）。
- `docker-compose.yml` —— 移除 postgres/redis 宿主端口映射。单项
  回滚：还原后两服务重新暴露宿主端口。
- `tests/test_security_audit_fixes.py`（新增）、
  `tests/test_asr.py`（2 用例补 DNS 桩）、
  `tests/test_high_priority_fixes.py`（1 用例补 DNS 桩）—— 测试。
- `.env.example` / `README.md` / `docs/security.md` —— 文档同步，
  回滚不影响运行行为。

行为变化提醒：① 反向代理部署若不转发 Host/X-Forwarded-Host，同源
写请求会被 403——代理需按 docs/security.md 传递 Host；② 音频下载
不再自动跟随重定向到非公网地址；③ REQUIRE_AUTH=true 时配置缺失
将无法启动（这是设计意图）。
