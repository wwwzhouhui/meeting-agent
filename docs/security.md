# 安全与部署加固说明

本文记录两轮加固（2026-10-25）：上午为审查修复的鉴权/上传上限/云
ASR 有界等待；下午为 security-audit 聚焦审查后修复的 CSRF、SSRF、
fail-closed 启动与 compose 端口收敛。对应回归测试在
`tests/test_high_priority_fixes.py` 与 `tests/test_security_audit_fixes.py`。

## 1. 访问认证（HTTP Basic）

**默认关闭**（与历史行为一致：本机 demo / 测试零配置）。但本服务能建
会议并触发真实分发（飞书建任务/建日程），公开暴露前必须启用。

启用方式：`.env` 里两项都填（任一为空 = 关闭）：

```bash
BASIC_AUTH_USERNAME=your-user
BASIC_AUTH_PASSWORD=your-password
# 推荐一并设置：fail-closed，凭据缺任一即拒绝启动
REQUIRE_AUTH=true
```

行为（`src/api/main.py` 的 `basic_auth_middleware`）：

- 覆盖**整个服务**：全部 API 路由 + 静态网页 UI；浏览器对页面和同源
  fetch 自动带上凭据，前端零改动。
- 只有 `/health` 豁免（负载均衡探活），它只返回 `{"ok": true}`。
- 凭据比较用 `secrets.compare_digest`（防时序侧信道）；畸形/非 Basic
  Authorization 头一律 401，不抛 500。
- 未启用时启动日志有醒目 warning；`REQUIRE_AUTH=true` 且凭据缺失时
  **启动直接失败**（fail-closed，config 事故不再静默放行）。
- curl 验证：

```bash
curl -u your-user:your-password http://localhost:8000/meetings
curl http://localhost:8000/meetings          # 无凭据 -> 401 + WWW-Authenticate: Basic
curl http://localhost:8000/health            # 200
```

更彻底的部署方式（反向代理做认证/限流/TLS）与本机制不冲突，可叠加。

## 2. CSRF 防护与防嵌入（`csrf_and_frame_guard` 中间件）

背景：浏览器会把缓存的 HTTP Basic 凭据自动附在跨站表单请求上，无
body 的状态变更端点（`/confirm-*` 等）此前可被第三方网页驱动。

行为（POST/PUT/PATCH/DELETE 生效，GET 不涉及状态变更）：

- 带 `Origin` 头且与（转发后）Host 不一致 → **403**；
- `Sec-Fetch-Site: cross-site`（无 Origin 也拦）→ **403**；
- 无 Origin / Sec-Fetch 头的请求（curl、脚本、服务端 webhook）放行
  ——CSRF 需要浏览器；同源浏览器请求的 Origin 与 Host 天然匹配；
- 反向代理场景用 `X-Forwarded-Host` 参与 Host 匹配；
- 所有响应带 `X-Frame-Options: DENY` 与
  `Content-Security-Policy: frame-ancestors 'none'`（防 clickjacking）。

残余边界（诚实声明）：同站不同子域（`Sec-Fetch-Site: same-site`）
仍放行——HTTP Basic 凭据按 host 隔离，该场景需按实际部署域名评估。

## 3. 音频大小上限

| 路径 | 上限 | 超限行为 |
|---|---|---|
| `POST /meetings/{id}/audio` 上传 | `MAX_AUDIO_UPLOAD_BYTES`（`src/api/main.py`，默认 100MB） | 边流式写盘边计数，超限即 413 并删除半成品文件；请求不再整体读进内存 |
| 远程音频 URL 下载（`Transcriber._download`） | `MAX_AUDIO_DOWNLOAD_BYTES`（`src/agents/minutes/transcriber.py`，默认 100MB） | 超限抛 ASRError，落到会议 warning，临时文件已清理 |

两个上限是模块常量（按需自行改代码调整），未做成环境变量。

## 4. SSRF 防护（服务端音频下载）

背景：`audio_url` 建会即可设置，服务端会亲自下载——此前无目标地址
过滤且自动跟随重定向，可被指向内网服务/云 metadata，且内部状态码与
完整内网 URL 会回显进会议 warning。

行为（`src/agents/minutes/transcriber.py` 的 `_assert_public_http_host`
+ `_download` 手动重定向循环）：

- 每一跳（含重定向目标）在**发请求前**解析并校验：环回/私网/
  link-local/保留/多播地址一律拒绝（含字面 IP 与域名解析结果，
  防御 DNS rebinding 的常见形态）；
- 重定向手动跟随，最多 5 跳，每跳重新校验；
- 域名解析失败按 fail-closed 处理（拒绝，不猜）；
- 所有下载错误信息**不含目标 URL/主机名**：warning 可读，内网拓扑
  不经此泄露。

## 5. 上传替换语义

对同一会议重新上传录音时（`src/api/main.py` 的 `upload_audio`）：

- 删除该会议旧录音文件（可能扩展名不同），不再留孤儿文件；
- 清空 `meeting.asr_task_id`：新录音绝不能续查旧录音的云端转写任务
  （这也是云端任务永久失败后的逃生门——重新上传即可重新提交）。

## 6. 云端 ASR（volcano）：有界等待 + 按任务 ID 续查

旧行为：`/process` 内同步轮询最长 `VOLCANO_ASR_TIMEOUT_SEC`（默认
1800 秒），长录音会把请求线程挂住几十分钟，并发几个即可耗尽线程池。

新行为（`src/agents/minutes/transcriber.py`）：

- 单次请求最多同步等待 `VOLCANO_ASR_SYNC_WAIT_SEC`（默认 60 秒，
  硬上限取 `min(SYNC_WAIT, TIMEOUT)`）；
- 超预算仍 pending 时**诚实返回**：`[]` + 「仍在处理中」的
  warning（含任务 ID 与「再次 /process 续查」指引），绝不假装转写
  成功，也绝不再挂住请求；
- 再次 `/process` 时按 `meeting.asr_task_id` 续查（提交成功那一刻就
  落库的老机制），**不重复提交**云端任务；
- 长录音的收敛方式 = 多次 `/process`，直到任务完成出纪要。

## 7. 部署端口收敛（docker-compose）

- postgres / redis **不再发布到宿主机**（原 `5432:5432`、`6379:6379`
  已移除）：二者均无鉴权/默认凭据，只在 compose 内网互通；本地调试
  可显式绑回环（`127.0.0.1:5432:5432`）。
- api 的 8000 端口仍发布；暴露到本机之外前按第 1 节启用认证
  （compose 文件头部有同样的提醒注释）。

## 回滚

两轮修复相互独立，可按文件粒度回滚（见仓库根 progress.md 的 Notes）。
整体回滚点：本轮全部修复前的提交
`8575aec docs: 新增操作手册（图文版，含 10 张界面截图）+ README 入口`。
