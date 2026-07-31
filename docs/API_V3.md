# AIFlow Web Agent Service API V3

## 1. 基础约定

默认监听：`0.0.0.0:8880`。本机使用 `http://127.0.0.1:8880`，局域网客户端使用 `http://<服务端局域网 IP>:8880`。

同源测试客户端：`GET /client`。页面静态资源位于 `/client-assets/`，不需要单独启动前端服务或配置 CORS。

除服务发现和系统容量外，项目请求必须包含：

```http
X-AIFlow-Context-Token: ctx_secret_...
```

服务没有账号登录。初始化时客户端必须提交 `deviceId` 和 `clientId`。`deviceId` 是前后端统一项目主键，`clientId` 是资源上传所需的客户端标识；能力令牌用于隔离该设备项目的文件、任务和 Claude 历史。SQLite 只保存令牌 SHA-256。

默认公网入口是 `aiflow_server.gateway:app`。浏览器无需且不能持有 HMAC 密钥：网关签发匿名 HttpOnly 会话，检查修改请求的 Origin，按会话/IP 限流，再用进程内密钥通过 `AIFLOW-HMAC-SHA256-V1` 调用不可公网寻址的核心应用。`GET /api/v3/capabilities` 返回 `client_auth.mode=server_bff`。

不要把 `aiflow_server.app:app` 单独监听到公网。拆分网关与核心时，HMAC 才作为两服务之间的线协议；细节见 [CLIENT_SECURITY.md](CLIENT_SECURITY.md)。

错误格式：

```json
{
  "detail": {
    "code": "task_queue_full",
    "message": "global task queue is full",
    "system_status": {}
  }
}
```

### 第三方模型提供方配置

服务启动前通过环境变量配置 Claude Code 的 Anthropic 兼容提供方：

```dotenv
ANTHROPIC_BASE_URL="https://your-provider.example.com"
ANTHROPIC_AUTH_TOKEN="your-bearer-token"
AIFLOW_CLAUDE_MODEL="your-provider-model-id"
```

使用 `x-api-key` 的提供方将 `ANTHROPIC_AUTH_TOKEN` 替换为 `ANTHROPIC_API_KEY`。两种认证变量不要同时设置。`AIFLOW_CLAUDE_MODEL` 优先于 `server_config.json -> claude.model`；密钥不属于 V3 HTTP 请求，网页客户端也不能读取或修改它。

仓库根目录的 `manage.sh` 默认加载 `.env.local`，也可由 `AIFLOW_ENV_FILE` 指定其他文件。使用 `./manage.sh config` 做脱敏检查。第三方端必须兼容 Anthropic Messages API，纯 OpenAI Chat Completions 代理不能直接作为此 URL。

## 2. 服务发现与容量

### `GET /health`

进程存活检查。

### `GET /ready`

检查数据库和默认 Skill 是否完整。`ready=false` 时不要提交任务。

### `GET /api/v3/capabilities`

返回模型、Skill、上传限制、多模态限制和队列配置，不返回令牌或设备信息。

### `GET /api/v3/system/status`

每次请求实时读取 SQLite 中的会话和任务状态：

```json
{
  "state": "busy",
  "sessions": {
    "limit": 100,
    "used": 12,
    "recently_active": 4,
    "activity_window_seconds": 60,
    "available": 88,
    "accepting_new": true
  },
  "tasks": {
    "concurrency_limit": 4,
    "running": 4,
    "queue_limit": 20,
    "queued": 3,
    "total_capacity": 24,
    "available": 17,
    "accepting_new": true
  }
}
```

`used` 是持久化设备项目占用，`recently_active` 是活动窗口内发送过鉴权请求的设备数。`state` 为 `available`、`busy`、`queue_full` 或 `session_full`。该接口不包含任何设备标识，前端可低频轮询。

## 3. deviceId + clientId 连接与项目

### `POST /api/v3/contexts`

配对完成后连接服务：

```json
{
  "label": "browser-tab",
  "device": {
    "device_id": "paired-platform-device-id",
    "client_id": "client-generated-upload-id",
    "product": "CoreS3",
    "firmware_version": "2.3.1",
    "capabilities": {"display": "320x240", "touch": true}
  }
}
```

`device_id` 和 `client_id` 都必填，也接受客户端常用的 `deviceId`、`clientId` 输入别名。旧字段 `push_client_id` 暂时作为 `client_id` 的兼容输入，但响应和持久化统一使用 `client_id`。服务端不需要客户端提交 MAC，也不维护 MAC 映射。

从 API `3.2` 开始，旧项目在再次 Coding 或部署前应使用同一 `deviceId` 携带真实 `clientId` 重连。服务端不会为旧数据生成或猜测 Client ID。

- 新 `deviceId`：创建项目并返回 `201`、`created=true`。
- 已存在 `deviceId`：复用同一 `context_id`、工作区和 Claude 历史，更新该项目保存的 `clientId`，返回 `200`、`created=false`。
- 重连会签发新能力令牌并立即使旧令牌失效。
- 新设备达到 `capacity.max_sessions` 时返回 `503 session_capacity_full`；已有设备仍可重连。

响应：

```json
{
  "context_id": "ctx_...",
  "device_id": "paired-platform-device-id",
  "client_id": "client-generated-upload-id",
  "access_token": "ctx_secret_...",
  "conversation_id": "conv_...",
  "label": "browser-tab",
  "device": {},
  "created_at": "2026-07-30T...+00:00",
  "model": "claude-code-default",
  "created": true,
  "system_status": {}
}
```

### `GET /api/v3/context`

返回当前设备项目、Claude 对话和 `active_task_id`。

### `GET /api/v3/project`

一次返回当前 `device_id`、`client_id`、项目文件、当前对话、Claude session 摘要和活跃任务，供前端恢复项目页面。

### `PATCH /api/v3/context/device`

更新产品、固件或能力信息。`device_id` 和 `client_id` 不允许通过 PATCH 修改；切换设备或更新 Client ID 应重新调用连接接口。

### `DELETE /api/v3/context?confirm=true`

删除该 `deviceId` 对应的项目、任务、事件和文件。存在活跃任务时返回 `409`。

当前版本不自动过期项目。生产部署应制定保留期和运维清理策略。

## 4. Coding 与图片/语音消息

### `POST /api/v3/tasks/coding`

纯文字：

```json
{
  "prompt": "编写触摸控制程序",
  "deploy_mode": "none"
}
```

图片和语音使用原始 Base64 字符串，不使用 Data URL：

```json
{
  "prompt": "根据截图和语音修改程序",
  "deploy_mode": "server",
  "attachments": [
    {
      "kind": "image",
      "mime_type": "image/png",
      "name": "screen.png",
      "data_base64": "iVBORw0KGgo..."
    },
    {
      "kind": "audio",
      "mime_type": "audio/wav",
      "name": "question.wav",
      "data_base64": "UklGRiQAAABXQVZF..."
    }
  ]
}
```

允许纯文字、纯附件或混合消息，不能三者都为空。支持 MIME：

- 图片：`image/png`、`image/jpeg`、`image/bmp`、`image/gif`、`image/webp`。
- 语音：`audio/wav`、`audio/x-wav`、`audio/mpeg`、`audio/mp4`、`audio/ogg`、`audio/amr`。

服务端先校验 Base64、MIME、数量和大小，再写入：

```text
inputs/<conversation_id>/<task_id>/image-01.png
inputs/<conversation_id>/<task_id>/audio-02.wav
```

Agent 收到相对路径并可用工作区工具读取。SQLite 任务请求只保存类型、MIME、路径、大小和原始名称，不保存 Base64。限制由 `messages.max_attachments`、`messages.max_attachment_bytes` 和 `messages.max_total_bytes` 配置。

Agent 仅处理 M5Stack UIFlow2/MicroPython 编程任务。编写或确认 UIFlow2 代码时优先建议使用 `uiflow2-coder`，但服务端不强制工具调用顺序；需要产品规格、屏幕、按键、引脚、电气特性、兼容性或排障依据时，可以直接先使用 `m5stack-assistant` 查询官方 MCP。确认官方资料或工具存在重大问题时，Agent 按 Skill 规则提交 `knowledge_feedback`，并以返回的 `feedback_id` 作为成功依据。

`deploy_mode`：

- `none`：只 Coding。
- `server`：Coding 成功后服务端直接推送。
- `agent`：明确授权 Agent 在代码验证完成后调用部署 Skill；先执行一次离线 `plan`，再执行一次最终推送。

非 `agent` 模式不会向 Agent 工作区暴露设备目标、推送 Skill 或推送网络权限。HTTP 推送成功只表示服务端已提交，不表示设备已经执行代码。

## 5. 有界任务队列

同一设备项目同时最多一个未结束任务。全局容量：

```text
max_concurrent_tasks + max_queued_tasks
```

执行槽满后任务保持 `queued`，按创建顺序等待；总容量满返回 `429 task_queue_full`。当前调度器支持单 Uvicorn 进程内并发，不支持通过增加 worker 横向扩展全局限流。

任务创建响应 `202`：

```json
{
  "task_id": "task_...",
  "device_id": "paired-platform-device-id",
  "kind": "coding",
  "status": "queued",
  "status_url": "/api/v3/tasks/task_...",
  "events_url": "/api/v3/tasks/task_.../events",
  "stream_token": "stream_...",
  "queue_position": 1,
  "system_status": {}
}
```

`queue_position` 仅在仍排队时为整数；进入运行或终态后为 `null`。

## 6. 直接重跑与部署计划

### `POST /api/v3/tasks/direct-run`

跳过 Agent，直接推送当前项目代码：

```json
{"code_path":"main.py","include_resources":true}
```

`include_resources=true` 会读取 `.aiflow/deploy.json` 中的非代码资源；如果清单误把本次 `code_path` 列为资源，服务端会自动排除该项，代码仍只发送到代码接口。

该任务使用同一并发槽和队列限制。

### `POST /api/v3/deployments/plan`

使用相同请求体，只校验上下文中的 `deviceId + clientId`、代码和资源清单，不访问网络。

设备部署只表示 MicroPython/UIFlow2 源码与资源提交，不是固件刷写。HTTP 成功也不证明设备已经执行。

## 7. 任务状态与卡死判断

### `GET /api/v3/tasks/{task_id}`

响应包含：

- `device_id`、`status`、`stage`、兼容字段 `progress`。
- `queue_position`。
- `heartbeat_age_seconds`、`agent_silence_seconds`、`possibly_stalled`。
- `result`、`error`、`last_event`。

状态机：

```text
queued -> running -> completed
                  -> failed
                  -> cancelled
```

只有 `running` 任务参与卡死判断；正常排队不会因为没有执行心跳而被误报。`possibly_stalled=true` 表示运行任务心跳超过配置值三倍，或 Coding 阶段 Agent 静默超过 `agent_stall_seconds`。前端应提示并提供取消，不要自动重复提交。

`progress` 不表示模型完成百分比：排队和运行阶段固定为 `0`，完成、失败或取消后为 `100`。客户端应显示 `status/stage` 和真实事件，不得制造 20%、50% 等估算值。

### `POST /api/v3/tasks/{task_id}/cancel`

排队任务立即取消并释放队列容量。正在执行的 Agent 会收到取消请求；已开始的设备 HTTP 推送只能尽力取消。

## 8. SSE 与事件历史

### `GET /api/v3/tasks/{task_id}/events`

鉴权任选：

- Header：`X-AIFlow-Context-Token`。
- Query：`stream_token=<TaskCreatedResponse.stream_token>`。

支持 `after=N` 和 `Last-Event-ID`。SSE 断开不停止后台任务。

事件写入 SQLite 后会立即唤醒对应任务的 SSE 订阅连接；空闲连接仅按 `tasks.heartbeat_seconds` 发送 heartbeat，不使用固定 500ms 轮询聚合事件。

服务端会持久化并透传可公开的 Agent/SDK 行为：

- 生命周期：`agent_connected`、`agent_system`、`agent_status`、`agent_warning`、`agent_rate_limit`、`agent_result`、`agent_result_error`、`agent_sdk_event`。
- 输出：`assistant_message_started`、`assistant_text_delta`、`assistant_message`、`assistant_message_finished`。
- 工具：`tool_started`、`tool_finished`、`server_tool_started`、`server_tool_finished`、`agent_user_message`、`agent_user_content`。
- 原始流状态：`agent_stream_event`；隐藏推理阶段只发送不含原文的 `agent_reasoning`。
- 服务任务：`task_queued`、`task_started`、`file_ready`、`deployment_started`、`deployment_finished`、`cancellation_requested`、`task_completed`、`task_failed`、`task_cancelled`、`heartbeat`。

`assistant_text_delta` 是实时增量并带 `finalized=false`；`assistant_message` 是 SDK 最终完整块并带 `finalized=true`。服务端从原始 `message_start.message.id` 与最终 `AssistantMessage.message_id` 生成统一 `response_id`，并保留内容块 `block_index`。客户端必须以 `response_id + block_index` 为键追加 delta，再用最终块覆盖校准；最终块到达后忽略同键晚到 delta，不能把完整块和增量各渲染一遍。SDK 外层 `message_uuid` 仅用于诊断，不能作为这两类事件的关联键。

`tool_started` 和 `tool_finished` 通过 `tool_use_id` 关联，分别携带已脱敏的真实完整 `input` 与 `content/result`。逐字符 `input_json_delta`、签名碎片和高频 `thinking_tokens` 不会单独持久化或下发，也不会转换成“正在组织参数”等推测状态。事件中的绝对工作区路径、`deviceId`、`clientId`、凭据和签名会统一脱敏；单个长文本也会截断。隐藏 chain-of-thought 不会对外发送，`agent_reasoning` 最多每个响应块每秒发送一次安全活动信号。

### `GET /api/v3/tasks/{task_id}/events/history?after=N&limit=200`

持久化事件补偿接口，需上下文令牌。

## 9. 文件与会话

- `GET /api/v3/files`：列出当前项目文件，包括已解码的 `inputs/`，隐藏 `.claude`、`.aiflow`、`.git`。
- `POST /api/v3/files`：multipart 文件上传，受 `uploads.max_bytes` 限制。
- `GET /api/v3/files/{path}`：下载项目文件。
- `POST /api/v3/conversation/reset`：生成新对话；`keep_files=false` 同时清空用户文件。
- `GET /api/v3/conversations`：返回 `device_id` 和该项目 Claude session 摘要。
- `GET /api/v3/conversations/{session_id}/messages`：读取当前设备项目内的 session 消息并替换内部绝对路径。

## 10. 关键状态码

| 状态 | code | 含义 |
| --- | --- | --- |
| `200` | - | 查询、重连或同步操作成功 |
| `201` | - | 新设备项目或文件已创建 |
| `202` | - | 后台任务已进入执行/等待容量 |
| `400` | `invalid_attachment_base64` 等 | 文件、路径或附件格式错误 |
| `401` | `invalid_context_token` | 能力令牌缺失、失效或已被重连轮换 |
| `409` | `context_busy` | 同一设备已有活跃任务 |
| `413` | `attachment_too_large` 等 | 上传或 Base64 附件超过限制 |
| `422` | `device_target_missing` 等 | 部署前验证失败 |
| `403` | `cross_site_request_rejected` | 匿名修改请求不是同源或允许的 Origin |
| `429` | `web_rate_limit_*` | 匿名会话或来源 IP 限额触发；响应包含 `Retry-After` 和 `retry_after_seconds` |
| `429` | `ai_task_limit_*` | 每客户端或全局 AI 任务费用保护触发 |
| `429` | `task_queue_full` | 全局执行槽和等待队列都已满 |
| `503` | `session_capacity_full` | 新设备项目达到总会话上限 |

## 11. 安全与部署边界

- 默认监听 `0.0.0.0` 的是匿名 Web BFF，核心应用仅存在于进程内。生产必须启用 HTTPS 和 Secure Cookie。
- 浏览器不持有 HMAC 密钥。启动脚本只运行 `aiflow_server.gateway:app`，不要单独暴露 `aiflow_server.app:app`。
- 创建连接时服务仍信任客户端提交的 `deviceId` 和 `clientId`；若要证明设备归属，还需由配对系统提供签名设备断言。
- Origin/CORS 只能防跨站调用，不能阻止脚本伪装浏览器；匿名服务需同时使用会话/IP 限额、WAF/验证码和全局预算。
- 能力令牌不要进入 URL、埋点、错误日志或跨站 Cookie。
- 使用单 Uvicorn worker。多进程/多实例需要 Redis 或外部队列提供全局容量、分布式锁和取消控制。
