# 飞书接入指南（日历 + 任务 + 群机器人通知）

本文对应已经落地的真实实现：

- `src/tools/feishu_client.py`：tenant token 缓存、user token 刷新、统一错误 `FeishuAPIError`
- `src/tools/calendar/feishu.py`：Calendar v4
- `src/tools/tasks/feishu_task.py`：Task v2
- `src/tools/notify/feishu.py`：自定义机器人 webhook（interactive 卡片 + 可选签名）

先说清两个最容易踩的差异：

1. **飞书日历时间戳是秒（字符串），飞书任务 due 时间戳是毫秒（字符串）。** 代码已分别处理，测试也分别锁死。
2. **日历写入看 token 类型。** tenant token 只能写应用自己的日历；写某位用户的日历要用该用户的 user token。

妙记没有本项目可调用的公开转写 API，本指南不处理转写；纪要仍从外部转写结果/已有逐字稿进入系统。

---

## 1. 创建企业自建应用

1. 打开飞书开放平台开发者后台，创建「企业自建应用」。
2. 在「凭证与基础信息」取得：
   - `FEISHU_APP_ID`（App ID，通常以 `cli_` 开头）
   - `FEISHU_APP_SECRET`（App Secret）
3. 按团队管理要求填写应用名称、图标和可用范围。权限或可用范围变更后，通常还要创建版本并经企业管理员审核发布，改完权限不要只保存草稿就开始联调。
4. 把值写入项目根目录的本地 `.env`（从 `.env.example` 复制），不要写进代码、文档、工单或聊天记录。`.env` 已被 `.gitignore` 忽略。

```dotenv
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=你的_app_secret
```

---

## 2. 开通权限

在应用的「权限管理」里按下面三组开通。控制台在不同租户/版本上可能把大权限拆成更细的 scope；原则是只开本项目实际调用的读写范围，不要为了省事开通讯录全量写权限。

### 日历

开通 `calendar:calendar` 对应的日历读写能力；若控制台显示细粒度项，至少覆盖：

- 日历/日程读取
- 日程创建、更新、删除
- 日程参会人管理
- 忙闲（freebusy）查询

代码会调用 `/open-apis/calendar/v4/calendars...` 与 `/open-apis/calendar/v4/freebusy/list`。

### 任务

开通 `task:task` 对应的任务读写能力，覆盖：

- 创建任务
- 更新任务（含完成状态）
- 任务评论
- 按 open_id 指派成员

代码会调用 `/open-apis/task/v2/tasks` 及其 `/{guid}`、`/{guid}/comments` 路径。Task v2 可以用 tenant token 创建并指派 `assignee`，不要求每位被指派人先给应用做日历 OAuth。

### 通讯录（只读，仅用于查 open_id）

开通 contact 相关的**只读**权限即可，例如控制台中与下列能力对应的项：

- 读取用户基本信息
- 按手机号/邮箱批量查询用户 ID
- 按员工 ID 查询用户（如团队内部 ID 就是工号时）

只在建映射表时需要；运行时代码不会拿通讯录权限去改成员资料。实际 scope 名称以应用权限页搜索「通讯录」「用户基本信息」「员工 ID」后显示为准。

---

## 3. 先选日历模式：tenant 还是 user

| 模式 | 配置 | 能写哪里 | 取舍 |
|---|---|---|---|
| tenant（默认） | `FEISHU_CALENDAR_AUTH=tenant` + App ID/Secret + 应用日历的 `FEISHU_CALENDAR_ID` | **应用自己的日历** | 服务端可无人值守运行；但它不是任何员工的个人日历，日程出现在应用日历上，参会邀请/个人日历体验受应用日历模式限制。适合先跑通、团队公告型日程、机器人代建日程。 |
| user | `FEISHU_CALENDAR_AUTH=user` + 每位用户的 OAuth 结果 + 目标用户日历的 `FEISHU_CALENDAR_ID` | **已授权用户的日历** | 最符合“替某位组织者建会”的体验；代价是每位要代建的用户至少要 OAuth 授权一次，并保存/刷新其 token。当前配置是一组 user token，因此一个进程实例主要服务一位日历所有者；多用户托管要再做按用户存 token 的凭据层。 |

`.env` 示例（user 模式）：

```dotenv
FEISHU_CALENDAR_AUTH=user
FEISHU_USER_ACCESS_TOKEN=用户OAuth得到的_access_token
FEISHU_REFRESH_TOKEN=同次OAuth得到的_refresh_token
FEISHU_CALENDAR_ID=目标用户日历ID
```

`FEISHU_USER_ACCESS_TOKEN` 可以单独使用；同时提供 `FEISHU_REFRESH_TOKEN` 时，客户端会在 token 被飞书判定失效时调用：

```text
POST /open-apis/authen/v1/oidc/refresh_access_token
```

自动刷新一次并重试原请求。刷新失败会抛出带飞书 `code/msg` 的 `FeishuAPIError`，不会静默改走 tenant token（那会把日程写到错误的日历）。

### OAuth 大致流程

1. 在应用「安全设置」里配置与你登录回调服务完全一致的重定向 URL。
2. 让用户打开飞书 OAuth 授权页并同意应用所需的日历权限。
3. 回调服务拿授权码换取 `user_access_token`、`refresh_token` 和该用户的 `open_id`。
4. 把 token 放入该用户对应的安全配置/密钥系统；本骨架先从 `.env` 的两个 `FEISHU_*TOKEN` 读取，适合单用户联调，不适合把很多人的 token 塞进同一个 `.env` 长期生产运行。

---

## 4. 获取 calendar_id

调用日历列表接口（把下面的 token 换成当前模式对应的访问令牌）：

```http
GET https://open.feishu.cn/open-apis/calendar/v4/calendars?page_size=50
Authorization: Bearer <tenant_or_user_access_token>
```

在返回的日历列表中按名称/主日历标记找到目标项，复制其 `calendar_id` 到：

```dotenv
FEISHU_CALENDAR_ID=这里填真实calendar_id
```

注意：

- tenant 模式要找**应用日历**。没有现成应用日历时，可先在飞书端按企业应用日历的创建方式建立，或用已授权的 Calendar API 创建日历后再查列表取得 ID。
- user 模式要找**该已授权用户**的日历 ID，通常优先选其主日历。
- 不要填 `primary`：那是 Google Calendar 的约定，不是飞书 Calendar v4 的日历 ID。

---

## 5. 获取 open_id 并填写 FEISHU_USER_MAP

系统内部负责人 ID（如示例数据里的 `u-lisi`）不是飞书 open_id。日历参会人和任务 assignee 只接受映射后的 `ou_...`，映射写成一行 JSON：

```dotenv
FEISHU_USER_MAP={"u-lisi":"ou_xxx","u-wangwu":"ou_yyy"}
```

取得 open_id 的常用方法：

1. **按手机号/邮箱批量查**（适合已开通讯录只读权限时）
   ```http
   POST https://open.feishu.cn/open-apis/contact/v3/users/batch_get_id?user_id_type=open_id
   Authorization: Bearer <tenant_access_token>
   Content-Type: application/json

   {"emails":["lisi@example.com"]}
   ```
   也可以传 `mobiles`。从返回的 `user_list` 里取对应 `user_id`（在 `user_id_type=open_id` 下就是 open_id）。

2. **从 OAuth 登录结果拿**：用户授权登录后，换 token 的响应/用户信息里有该用户 open_id。适合 user 日历模式顺手建映射。

3. **从企业管理/开放平台调试工具查**：在目标用户知情并符合企业数据规范的前提下，用通讯录查询接口或开放平台 API 调试台按员工 ID/邮箱查。不要用抓包、通讯录导出文件或第三方脚本绕过权限。

映射解析失败时代码会 log warning 并按空映射处理；未映射的内部 ID 会被跳过：日历建会仍会成功但返回 `warnings/skipped_attendees`，任务会不带 assignee 创建并在结果里给 warning。两者都不会因为一个坏 ID 整场失败。

---

## 6. 配置群机器人 webhook

通知不走应用消息 API，而是每个群单独添加的自定义机器人：

1. 在目标飞书群的群设置中添加「自定义机器人」。
2. 复制完整 webhook 地址到：
   ```dotenv
   FEISHU_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/你的hook标识
   ```
3. 如果机器人安全设置开启了「签名校验」，把机器人页面给出的密钥填到：
   ```dotenv
   FEISHU_WEBHOOK_SECRET=机器人签名密钥
   ```
   代码会按飞书规范用当前时间戳与密钥生成 HMAC-SHA256 签名。不要把“关键词/IP 白名单”误当成签名密钥；若机器人同时启用了 IP 白名单，还要把运行本服务的出口 IP 加进去。
4. `.env` 里设置 `NOTIFY_PROVIDER=feishu` 后，提醒扫描才会通过 factory 选到这个工具。Webhook 机器人发到固定群，不能按 `to` 私聊某个人；`to` 只保留在发送结果中做审计。

旧配置名 `FEISHU_WEBHOOK` 仍兼容读取，但新部署请用 `FEISHU_WEBHOOK_URL`。

---

## 7. 推荐联调顺序

不要一上来跑整场会议。按下面顺序把外部依赖逐个排掉：

1. **webhook**：先发一条测试通知，确认群、签名/IP 设置都对。最容易验证，也不需要日历/通讯录权限。
2. **任务**：建一条标题带 `[smoke]` 的测试任务，确认 tenant token、任务权限、due 毫秒时间戳和 assignee open_id 都对。
3. **日历**：最后建测试日程。日历比前两步多一个变量——token 模式。先明确你要写应用日历还是用户日历，再排 `calendar_id` 和参会人。

项目提供了脚本（**会产生真实数据，本项目交付时没有替你运行**）：

```bash
cp .env.example .env
# 填好飞书配置后：
python3 examples/feishu_smoke.py
```

脚本依次验证 tenant token → 测试任务 → webhook 通知 →（只有配了 `FEISHU_CALENDAR_ID` 才）测试日程；每步打印成功标识或飞书 `code/msg`。测试日程标题带 `[smoke]` 前缀，联调后请在飞书里删除测试任务和日程。

整链路配置时，`.env` 里还需要把 provider 切过去：

```dotenv
CALENDAR_PROVIDER=feishu
TASK_PROVIDER=feishu
NOTIFY_PROVIDER=feishu
```

外部 API 失败不会打崩会议流程：任务会在对应条目上落 `dispatch_failed` + `dispatch_error`，日历建会失败会进入 `meeting.warnings`，提醒扫描会计入 `failed`。先看这些字段里的飞书 code/msg，再对照权限、token 类型和 ID 类型排查。
