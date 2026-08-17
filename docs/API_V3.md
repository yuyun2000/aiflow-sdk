# AIFlow Web Agent Service API V3

## 1. 基础约定

默认监听：`0.0.0.0:8880`。本机使用 `http://127.0.0.1:8880`，局域网客户端使用 `http://<服务端局域网 IP>:8880`。

同源测试客户端：`GET /client`。页面静态资源位于 `/client-assets/`，不需要单独启动前端服务或配置 CORS。

除服务发现和系统容量外，项目请求必须包含：

```http
X-AIFlow-Context-Token: ctx_secret_...
```

服务没有账号登录。初始化时客户端必须提交 `deviceId` 和 `clientId`，可选提交 MAC。`deviceId` 是前后端统一项目主键，`clientId` 是资源上传所需的客户端标识；能力令牌用于隔离该设备项目的文件、任务和 Claude 历史。SQLite 只保存令牌 SHA-256。

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
AIFLOW_CLAUDE_CONTEXT_WINDOW_TOKENS="258000"
AIFLOW_CLAUDE_MAX_TURNS="30"
AIFLOW_CLAUDE_SUPPORTS_IMAGE_INPUT="false"
```

使用 `x-api-key` 的提供方将 `ANTHROPIC_AUTH_TOKEN` 替换为 `ANTHROPIC_API_KEY`。两种认证变量不要同时设置。`AIFLOW_CLAUDE_MODEL` 优先于 `server_config.json -> claude.model`；`AIFLOW_CLAUDE_CONTEXT_WINDOW_TOKENS` 和 `AIFLOW_CLAUDE_MAX_TURNS` 分别覆盖 `claude.context_window_tokens`（默认 258K）和 `claude.max_turns`（默认 30）。上下文值通过 Claude Code 的 `CLAUDE_CODE_MAX_CONTEXT_TOKENS` 环境变量控制自动压缩上限，实际可用值仍受第三方模型限制。DeepSeek 等不支持图片输入的模型应设置 `AIFLOW_CLAUDE_SUPPORTS_IMAGE_INPUT=false`，该变量优先于 `server_config.json -> claude.supports_image_input`。密钥不属于 V3 HTTP 请求，网页客户端也不能读取或修改它。

仓库根目录的 `manage.sh` 默认加载 `.env.local`，也可由 `AIFLOW_ENV_FILE` 指定其他文件。使用 `./manage.sh config` 做脱敏检查。第三方端必须兼容 Anthropic Messages API，纯 OpenAI Chat Completions 代理不能直接作为此 URL。

## 2. 服务发现与容量

### `GET /health`

进程存活检查。

### `GET /ready`

检查数据库和默认 Skill 是否完整。`ready=false` 时不要提交任务。

### `GET /api/v3/capabilities`

返回模型、Skill、上传限制、多模态限制、上下文上限、最大 Agent 轮次和队列配置，不返回令牌或设备信息。`context_window_tokens` 是传给 Claude Code 自动压缩的 token 上限，`max_turns` 是单次 Agent 任务的最大对话轮次。

启用火山引擎 SAUC 后还会返回脱敏状态：

```json
"asr": {
  "enabled": true,
  "configured": true,
  "resource_id": "volc.seedasr.sauc.duration",
  "url": "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_nostream"
}
```

`configured` 只表示服务端发现凭证，不返回 API Key。浏览器不直接连接火山引擎 WebSocket。

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
  },
  "conversation_logging": {
    "enabled": true,
    "pending_records": 0,
    "oldest_created_at": null,
    "max_attempts": 0,
    "worker_running": true
  }
}
```

`used` 是持久化设备项目占用，`recently_active` 是活动窗口内发送过鉴权请求的设备数。`state` 为 `available`、`busy`、`queue_full` 或 `session_full`。`conversation_logging.pending_records` 是尚未收到 TLS 成功响应的持久化记录数；持续增长表示日志上传异常，但不阻断任务执行。该接口不包含任何设备标识，前端可低频轮询。

## 3. deviceId + clientId 连接与项目

### `POST /api/v3/contexts`

配对完成后连接服务：

```json
{
  "label": "browser-tab",
  "device": {
    "device_id": "paired-platform-device-id",
    "client_id": "client-generated-upload-id",
    "mac_address": "AA:BB:CC:DD:EE:FF",
    "product": "CoreS3",
    "firmware_version": "2.3.1",
    "capabilities": {"display": "320x240", "touch": true}
  }
}
```

`device_id` 和 `client_id` 都必填，也接受客户端常用的 `deviceId`、`clientId` 输入别名。`device.mac_address` 是可选字段，同时接受 `device.macAddress` 和 `device.mac` 输入别名，响应和持久化统一使用 `mac_address`。为兼容已经把 MAC 放在请求体外层的客户端，`mac_address`、`macAddress`、`mac` 也可直接作为 `/contexts` 请求体字段；嵌套 `device` 字段优先。旧字段 `push_client_id` 暂时作为 `client_id` 的兼容输入，但响应和持久化统一使用 `client_id`。不传 MAC 的旧客户端请求仍然有效；已有设备重连时省略 MAC 会保留已绑定的 MAC。

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
  "mac_address": "AA:BB:CC:DD:EE:FF",
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

一次返回当前 `device_id`、`client_id`、可选 `mac_address`、项目文件、当前对话、Claude session 摘要和活跃任务，供前端恢复项目页面。

### `PATCH /api/v3/context/device`

更新产品、固件、能力或可选 MAC 信息。`device_id` 和 `client_id` 不允许通过 PATCH 修改；切换设备或更新 Client ID 应重新调用连接接口。

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

从 API `3.4` 开始，图片和语音由客户端提供必填文件名，并使用原始 Base64 字符串，不使用 Data URL：

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

允许纯文字、纯附件或混合消息，不能全部为空。附件字段：

- `kind`：必填，`image` 或 `audio`。
- `mime_type`：必填，附件实际媒体类型。
- `name`：必填，客户端希望下游 Agent 使用的文件名。只能是单个文件名，不能包含 `/`、`\\`，不能是 `.` 或 `..`，也不能带首尾空白或控制字符。
- `data_base64`：必填，文件内容的原始 Base64。

支持的 MIME 与文件扩展名：

- 图片：`image/png -> .png`、`image/jpeg -> .jpg/.jpeg`、`image/bmp -> .bmp`、`image/gif -> .gif`、`image/webp -> .webp`。
- 语音：`audio/wav` 或 `audio/x-wav -> .wav`、`audio/mpeg -> .mp3`、`audio/mp4 -> .m4a/.mp4`、`audio/ogg -> .ogg`、`audio/amr -> .amr`。

服务端先校验文件名、Base64、MIME、数量和大小，再按客户端名称写入：

```text
inputs/<conversation_id>/<task_id>/screen.png
inputs/<conversation_id>/<task_id>/question.wav
```

同一消息内的文件名按 Unicode 规范化和大小写折叠后不能重复，避免在不同文件系统中发生静默覆盖。Agent 收到含客户端文件名的相对路径。`claude.supports_image_input=true`（默认）时可按任务需要读取图片；设为 `false` 时，服务通过 SDK `PreToolUse` 拒绝图片 `Read`，同时提示 Agent 只能把图片作为不透明 UIFlow2 资源按路径引用或写入部署清单，不能解码、OCR、描述或猜测内容。SQLite 任务请求只保存类型、MIME、路径、大小和名称，不保存 Base64。限制由 `messages.max_attachments`、`messages.max_attachment_bytes` 和 `messages.max_total_bytes` 配置。

### `POST /api/v3/asr`

对单个 WAV 音频调用火山引擎 SAUC `bigmodel_nostream`，返回整句识别结果。该接口适用于非实时语音输入；AIFlow 服务端负责 WebSocket 二进制协议、gzip 帧和鉴权头，客户端只提交 multipart 文件。

请求必须带 `X-AIFlow-Context-Token`：

| 字段 | 位置 | 必选 | 说明 |
| --- | --- | --- | --- |
| `file` | multipart | 是 | 有效 WAV，`audio/wav` 或 `audio/x-wav`，大小受 `uploads.max_bytes` 限制 |
| `language` | multipart | 否 | 如 `zh-CN`、`en-US`；为空时由模型自动识别 |
| `enable_punc` | multipart | 否 | 添加标点，默认 `true` |
| `enable_itn` | multipart | 否 | 规范化数字、金额和日期，默认 `true` |
| `enable_ddc` | multipart | 否 | 语义顺滑，默认 `true` |
| `show_utterances` | multipart | 否 | 返回分句信息，默认 `true` |

```bash
curl -fsS -X POST http://127.0.0.1:8880/api/v3/asr \
  -H 'X-AIFlow-Context-Token: ctx_secret_...' \
  -F 'file=@question.wav;type=audio/wav' \
  -F 'language=zh-CN'
```

成功响应：

```json
{
  "text": "打开客厅空调，退出。",
  "log_id": "20260808144713DDB668E2B33A9CECA1CD",
  "duration_ms": 5700,
  "utterances": [{"text": "打开客厅空调，退出。", "start_time": 120, "end_time": 4800, "definite": true}]
}
```

未启用或未配置凭证返回 `503`（`asr_disabled` / `asr_not_configured`）；WAV 无效返回 `400 invalid_audio`；提供方超时返回 `504 asr_timeout`；提供方鉴权失败返回 `502 asr_auth_failed`；其它提供方故障返回 `502 asr_unavailable`。服务配置在 `.env.local`：

```dotenv
AIFLOW_ASR_ENABLED="true"
AIFLOW_ASR_API_KEY="replace-with-volcengine-speech-api-key"
AIFLOW_ASR_RESOURCE_ID="volc.seedasr.sauc.duration"
```

也支持旧版控制台的 `AIFLOW_ASR_APP_KEY` + `AIFLOW_ASR_ACCESS_KEY`。密钥不能写入 JSON、网页请求或设备工作区。没有真实凭证时只能运行 fake WebSocket 协议测试，不能据此宣称火山引擎真实接口可用。

### `POST /api/v3/asr/stream`

给 ESP32 等不能缓存长音频的客户端使用。请求 body 是原始 PCM 字节流，服务端收到每个 HTTP chunk 后立即切成约 200ms 的 SAUC 音频帧并发送，上游处理期间不会把完整音频写入内存或磁盘。

请求头和查询参数：

```http
POST /api/v3/asr/stream?format=pcm&rate=16000&bits=16&channel=1&language=zh-CN
Content-Type: audio/pcm
X-AIFlow-Context-Token: ctx_secret_...
Transfer-Encoding: chunked
```

`format` 必须为 `pcm`；默认 `16000 Hz / 16 bit / mono`。服务端向 SAUC 声明 `pcm` 并发送裸 PCM 帧。客户端应在停止录音后结束 HTTP body，服务端再发送 SAUC 的负序号最终帧并等待整句结果。该接口仍返回同样的 JSON 结果，不提供上游增量文本 SSE。若前面使用 Nginx/Caddy，需关闭该路由的请求体缓冲，否则代理可能先收完整音频再转发。

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

资源项的 `devicePath` 是相对设备 Flash 根目录的目录，例如 `res/img/` 或 `res/audio/`，不能包含资源文件名。它与 UIFlow 运行时的 `/flash/res/img/...`、`file://flash/res/audio/...` 表示同一位置；省略时由上传接口按扩展名自动分配。代码和资源都不能引用工作区的 `.claude`、`.aiflow` 或 `.git` 内部文件。

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
- 输出：`assistant_message_started`、`assistant_text_delta`、`assistant_message`、`assistant_message_finished`；模型思考为 `agent_reasoning`，中断兜底为 `agent_partial_capture`。
- 工具：`tool_started`、`tool_finished`、`server_tool_started`、`server_tool_finished`、`agent_user_message`、`agent_user_content`。
- 原始流状态：`agent_stream_event`。
- 服务任务：`task_queued`、`task_started`、`file_ready`、`deployment_started`、`deployment_finished`、`cancellation_requested`、`task_completed`、`task_failed`、`task_cancelled`、`heartbeat`。

Agent 的所有公开文本块使用用户本人最近一条可识别的自然语言；用户明确指定回复语言时优先。服务不会根据 UIFlow/API 术语、Skill/MCP、官方文档、代码、日志或工具结果的语种切换回复语言。混合语言请求以用户自然语言句子的主体为准，代码、命令、API 名称、标识符和产品名在直译不自然时保留原文。当前请求无语种信号时沿用会话中最近的用户语种；全新且只有附件的请求使用客户端默认简体中文。

`assistant_text_delta` 和流式 `agent_reasoning` 都是实时增量并带 `finalized=false`，正文分别位于 `text` 和 `thinking`；最终 `assistant_message` 和 `agent_reasoning` 带 `finalized=true` 并携带 SDK 最终完整块。服务端从原始 `message_start.message.id` 与最终 `AssistantMessage.message_id` 生成统一 `response_id`，并保留内容块 `block_index`。客户端必须按内容类型分别以 `response_id + block_index` 为键追加 delta，再用最终块覆盖校准；最终块到达后忽略同键晚到 delta，不能把完整块和增量各渲染一遍。流中断时，thinking 的 `agent_partial_capture` 带 `partial=true`、`finalized=false` 和已收到的累计 `thinking`，同样用于覆盖校准。SDK 外层 `message_uuid` 仅用于诊断，不能作为增量与最终消息的关联键。

`tool_started` 和 `tool_finished` 通过 `tool_use_id` 关联，分别携带已脱敏的真实完整 `input` 与 `content/result`。逐字符 `input_json_delta`、签名碎片和高频 `thinking_tokens` 不会单独持久化或下发，也不会转换成“正在组织参数”等推测状态。SDK 实际提供的每个 `thinking_delta` 会作为 `agent_reasoning` 实时持久化和下发，不再节流为空活动标记。事件 payload 中的绝对工作区路径、`deviceId`、`clientId`、MAC、凭据和签名会统一脱敏；thinking 正文完整保留，其他单个长文本仍可能截断。

启用 TLS 对话审计时，公开 API 中的 thinking 与 TLS 最终审计记录来自同一 SDK 内容，但用途和粒度不同：SSE/`task_events` 保留每个脱敏 delta 供实时与断线恢复；TLS 不上传这些 delta，只在收到最终 `AssistantMessage` 后把完整 thinking 块写入 outbox，流中断时才上传一次 `agent_partial_capture`。每条 TLS 物理记录的 envelope 还绑定当前项目的原始 `device_id`、`client_id` 和可选 `mac_address`，便于外部分析；payload 仍按公开规则脱敏。逻辑事件在上传前去重，TLS 至少一次投递造成的物理重传由消费端按 `record_id` 去重。包含原始标识的 Topic 必须使用最小权限和访问审计，具体覆盖矩阵、schema 和权限边界见 [CONVERSATION_LOGGING.md](CONVERSATION_LOGGING.md)。

### `GET /api/v3/tasks/{task_id}/events/history?after=N&limit=200`

持久化事件补偿接口，需上下文令牌。

## 9. 文件与会话

- `GET /api/v3/files`：列出当前项目文件，包括已解码的 `inputs/`，隐藏 `.claude`、`.aiflow`、`.git`。
- `POST /api/v3/files`：multipart 文件上传，受 `uploads.max_bytes` 限制。
- `GET /api/v3/files/{path}`：下载项目文件。
- `POST /api/v3/conversation/reset`：生成新对话；`keep_files=false` 同时清空用户文件。
- `GET /api/v3/conversations`：返回 `device_id` 和该项目 Claude session 摘要。
- `GET /api/v3/conversations/{session_id}/messages`：读取当前设备项目内的原始 session message 结构；SDK transcript 中存在的 thinking 正文会完整返回，内部绝对路径、凭据、设备标识和 provider signature 会脱敏。

## 10. 关键状态码

| 状态 | code | 含义 |
| --- | --- | --- |
| `200` | - | 查询、重连或同步操作成功 |
| `201` | - | 新设备项目或文件已创建 |
| `202` | - | 后台任务已进入执行/等待容量 |
| `400` | `invalid_attachment_name` | 附件名称不是安全的单个文件名 |
| `400` | `duplicate_attachment_name` | 同一消息内存在重复附件名 |
| `400` | `attachment_extension_mismatch` | 附件扩展名与 MIME 不匹配 |
| `400` | `invalid_attachment_base64` 等 | 文件、路径或附件内容格式错误 |
| `401` | `invalid_context_token` | 能力令牌缺失、失效或已被重连轮换 |
| `409` | `context_busy` | 同一设备已有活跃任务 |
| `413` | `attachment_too_large` 等 | 上传或 Base64 附件超过限制 |
| `422` | 请求校验错误 | 缺少必填附件 `name` 等字段 |
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
