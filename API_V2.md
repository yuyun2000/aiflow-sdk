# UIFlow Code Generator API V2 接口文档

> 本文档面向下游调用方，对应服务端 `server_v2.py`。
> 与旧版 (`server.py` / `API.md`) 相比，V2 新增多项目管理、会话隔离与历史回溯能力。

---

## 服务地址

```
http://<host>:8880
```

默认端口 **8880**（旧版为 8000，注意区分）。

---

## 核心概念

```
项目（Project）
├── project_id   — 服务端生成，形如 "proj_abc123def456"
├── working_directory — 该项目的 Claude 工作目录（文件隔离）
└── 会话（Session）  — 每次 /chat 调用自动创建
    ├── session_id   — Claude SDK 自动生成的 UUID
    └── messages     — 本次对话的完整历史消息
```

**关键点：**
- `project_id` 由服务端在创建项目时生成，调用方保存后用于后续所有请求。
- `session_id` 由 Claude CLI 自动生成，每次 `/chat` 都会产生新会话，服务端会在 `result` 事件中返回本次的 `session_id`，调用方可保存用于历史查询。
- 不同项目的文件与会话完全隔离，互不影响。

---

## 接口总览

| 方法 | 路径 | 说明 |
|------|------|------|
| GET  | `/health` | 健康检查 |
| POST | `/projects` | 创建项目 |
| GET  | `/projects` | 列出所有项目 |
| GET  | `/projects/{project_id}` | 获取项目详情及会话列表 |
| DELETE | `/projects/{project_id}` | 删除项目（含所有文件） |
| GET  | `/projects/{project_id}/sessions/{session_id}/messages` | 获取会话历史消息 |
| POST | `/chat` | 发起对话（SSE 流式响应） |
| POST | `/chat/abort` | 中断正在进行的对话任务 |

---

## GET `/health` — 健康检查

```http
GET /health
```

**响应**

```json
{"status": "ok"}
```

---

## POST `/projects` — 创建项目

```http
POST /projects
Content-Type: application/json
```

### 请求体

```json
{
  "name": "温度监控项目",
  "description": "M5Stack Tab5 温度传感器展示"
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `name` | string | 是 | 项目名称，仅用于展示 |
| `description` | string | 否 | 项目描述，默认为空 |

### 响应 `200`

```json
{
  "project_id": "proj_abc123def456",
  "name": "温度监控项目",
  "description": "M5Stack Tab5 温度传感器展示",
  "created_at": "2026-04-13T10:30:00.123456",
  "updated_at": "2026-04-13T10:30:00.123456",
  "working_directory": "/abs/path/to/projects_data/proj_abc123def456/workspace"
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `project_id` | string | 项目唯一 ID，后续所有请求的主键 |
| `name` | string | 项目名称 |
| `description` | string | 项目描述 |
| `created_at` | string | 创建时间（ISO 8601） |
| `updated_at` | string | 最后更新时间（ISO 8601） |
| `working_directory` | string | Claude 工作目录绝对路径（可不关心） |

---

## GET `/projects` — 列出所有项目

```http
GET /projects
```

### 响应 `200`

返回所有项目的数组，每个元素结构与"创建项目"响应相同。

```json
[
  {
    "project_id": "proj_abc123def456",
    "name": "温度监控项目",
    "description": "M5Stack Tab5 温度传感器展示",
    "created_at": "2026-04-13T10:30:00.123456",
    "updated_at": "2026-04-13T10:35:00.000000",
    "working_directory": "/abs/path/to/..."
  },
  {
    "project_id": "proj_def456abc789",
    "name": "麦克风波形项目",
    "description": "",
    "created_at": "2026-04-13T11:00:00.000000",
    "updated_at": "2026-04-13T11:00:00.000000",
    "working_directory": "/abs/path/to/..."
  }
]
```

---

## GET `/projects/{project_id}` — 获取项目详情

```http
GET /projects/proj_abc123def456
```

### 路径参数

| 参数 | 说明 |
|------|------|
| `project_id` | 项目 ID |

### 响应 `200`

```json
{
  "project": {
    "project_id": "proj_abc123def456",
    "name": "温度监控项目",
    "description": "M5Stack Tab5 温度传感器展示",
    "created_at": "2026-04-13T10:30:00.123456",
    "updated_at": "2026-04-13T10:35:00.000000",
    "working_directory": "/abs/path/to/..."
  },
  "sessions": [
    {
      "session_id": "550e8400-e29b-41d4-a716-446655440000",
      "summary": "创建温度传感器读取代码",
      "last_modified": 1713000000,
      "file_size": 12345
    },
    {
      "session_id": "660e8400-e29b-41d4-a716-446655440001",
      "summary": "优化刷新频率",
      "last_modified": 1713001000,
      "file_size": 13000
    }
  ]
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `project` | object | 项目元数据（同创建响应） |
| `sessions` | array | 该项目下的所有历史会话 |
| `sessions[].session_id` | string | 会话 UUID（用于历史消息查询） |
| `sessions[].summary` | string | Claude 自动生成的会话摘要 |
| `sessions[].last_modified` | int | 最后修改时间（Unix 时间戳，秒） |
| `sessions[].file_size` | int \| null | 会话文件大小（字节） |

### 响应 `404`

```json
{"detail": "项目不存在"}
```

---

## DELETE `/projects/{project_id}` — 删除项目

> **警告**：此操作会删除该项目的工作目录及其中所有生成的文件，不可恢复。

```http
DELETE /projects/proj_abc123def456
```

### 响应 `200`

```json
{"message": "项目已删除"}
```

### 响应 `404`

```json
{"detail": "项目不存在"}
```

---

## GET `/projects/{project_id}/sessions/{session_id}/messages` — 获取会话历史

```http
GET /projects/proj_abc123def456/sessions/550e8400-e29b-41d4-a716-446655440000/messages?limit=10&offset=0
```

### 路径参数

| 参数 | 说明 |
|------|------|
| `project_id` | 项目 ID |
| `session_id` | 会话 UUID（从项目详情接口获取） |

### 查询参数

| 参数 | 类型 | 必填 | 默认 | 说明 |
|------|------|------|------|------|
| `limit` | int | 否 | 全部 | 最多返回多少条消息 |
| `offset` | int | 否 | `0` | 跳过前 N 条消息（分页用） |

### 响应 `200`

```json
{
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "messages": [
    {
      "type": "user",
      "uuid": "msg-uuid-001",
      "message": {
        "role": "user",
        "content": "创建一个读取温度传感器的 UIFlow 代码"
      }
    },
    {
      "type": "assistant",
      "uuid": "msg-uuid-002",
      "message": {
        "role": "assistant",
        "content": [
          {
            "type": "text",
            "text": "我来帮你创建温度传感器代码..."
          }
        ]
      }
    }
  ]
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `session_id` | string | 会话 UUID |
| `messages` | array | 消息列表，按时间顺序排列 |
| `messages[].type` | string | `"user"` 或 `"assistant"` |
| `messages[].uuid` | string | 消息唯一 ID |
| `messages[].message` | object | 消息内容，结构与 OpenAI Messages API 兼容 |

### 响应 `404`

```json
{"detail": "项目不存在"}
```

---

## POST `/chat` — 发起对话（SSE 流式）

这是核心接口。使用 `text/event-stream` 实时推送 Claude 的处理过程和最终生成的代码文件。

```http
POST /chat
Content-Type: application/json
```

### 请求体

```json
{
  "project_id": "proj_abc123def456",
  "prompt": "创建一个显示温度和湿度的 UIFlow 程序，1 秒刷新一次",
  "session_id": null
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `project_id` | string | 是 | 目标项目的 ID（决定文件隔离目录） |
| `prompt` | string | 是 | 自然语言描述的 UIFlow 功能需求 |
| `session_id` | string \| null | 否 | 会话 ID。传 `null` 或不传则创建新会话；传已有 session_id 则继续该会话的上下文对话 |

**会话继续说明：**
- 首次对话时传 `null`，服务端会创建新会话并在 `result` 事件中返回 `session_id`
- 保存该 `session_id` 后，下次请求时传入相同的 `session_id`，Claude 会加载该会话的完整历史继续对话
- 不同 `session_id` 的对话历史完全隔离，适合在同一项目内管理多个独立任务

### 响应格式

```http
Content-Type: text/event-stream
Cache-Control: no-cache
Connection: keep-alive
```

每条 SSE 消息格式：
```
data: <JSON 字符串>\n\n
```

---

### 事件类型详解

#### ⓪ `task_start` — 任务启动（第一条，必然出现）

```json
{
  "type": "task_start",
  "data": {
    "task_id": "task_abc123def456"
  }
}
```

| 字段 | 说明 |
|------|------|
| `data.task_id` | 本次任务的唯一 ID，用于调用 `/chat/abort` 中断任务 |

> 客户端应保存 `task_id`，在需要中断时传给 `/chat/abort`。

---

#### ① `message` — Claude 文字输出（多条，实时推送）

```json
{
  "type": "message",
  "data": {
    "text": "我来帮你创建温度显示程序，使用 SHT30 传感器..."
  }
}
```

| 字段 | 说明 |
|------|------|
| `data.text` | Claude 输出的一段文字，可能有多条，按顺序拼接即为完整回复 |

---

#### ② `file` — 生成的代码文件（一条，Claude 写完文件后推送）

```json
{
  "type": "file",
  "data": {
    "name": "main.py",
    "content": "import os, sys\nfrom m5stack import *\n..."
  }
}
```

| 字段 | 说明 |
|------|------|
| `data.name` | 固定为 `"main.py"` |
| `data.content` | 完整的 MicroPython 代码，可直接写入设备运行 |

> 若 Claude 未生成文件（如对话失败），此事件可能不出现。

---

#### ③ `result` — 对话统计信息（一条，紧跟 `file` 之后）

```json
{
  "type": "result",
  "data": {
    "session_id": "550e8400-e29b-41d4-a716-446655440000",
    "total_cost_usd": 0.0123,
    "usage": {
      "input_tokens": 1000,
      "output_tokens": 500,
      "cache_creation_input_tokens": 0,
      "cache_read_input_tokens": 800
    },
    "stop_reason": "end_turn",
    "duration_ms": 5432,
    "num_turns": 3
  }
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `data.session_id` | string | **本次对话的会话 ID**，保存后可用于历史查询 |
| `data.total_cost_usd` | float | SDK 估算的本次对话费用（美元） |
| `data.usage.input_tokens` | int | 输入 token 数 |
| `data.usage.output_tokens` | int | 输出 token 数 |
| `data.usage.cache_creation_input_tokens` | int | 写入缓存的 token 数 |
| `data.usage.cache_read_input_tokens` | int | 命中缓存的 token 数（越大代表节省越多） |
| `data.stop_reason` | string | 停止原因，通常为 `"end_turn"` |
| `data.duration_ms` | int | 本次请求总耗时（毫秒） |
| `data.num_turns` | int | 对话轮数 |
| `data.is_error` | bool | 本次对话是否异常终止（`true` 时应检查是否有 `error` 事件） |

---

#### ④ `error` — 错误（出现时推送）

错误事件包含结构化的错误信息，便于下游根据错误类型做出不同处理。

```json
{
  "type": "error",
  "data": {
    "error_code": "rate_limit",
    "message": "请求频率超限，请稍后重试",
    "category": "model",
    "retryable": true
  }
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `data.error_code` | string | 错误码，见下方错误码表 |
| `data.message` | string | 人类可读的错误描述 |
| `data.category` | string | 错误分类：`"model"` / `"environment"` / `"sdk"` / `"server"` |
| `data.retryable` | bool | 是否建议重试 |
| `data.session_id` | string \| 无 | 出错时的会话 ID（仅部分错误携带） |
| `data.exit_code` | int \| 无 | CLI 进程退出码（仅 `process_error` 携带） |

**错误码表**

| error_code | category | 说明 | retryable |
|------------|----------|------|-----------|
| `authentication_failed` | model | API 认证失败，API Key 无效或过期 | 否 |
| `billing_error` | model | 账户计费异常（余额不足等） | 否 |
| `rate_limit` | model | Anthropic API 请求频率超限 | 是 |
| `invalid_request` | model | 请求参数无效 | 否 |
| `server_error` | model | Anthropic 服务端错误 | 是 |
| `result_error` | model | 对话异常终止（ResultMessage 报错） | 否 |
| `cli_not_found` | environment | Claude CLI 未安装或路径不正确 | 否 |
| `cli_connection_error` | environment | 无法连接 Claude CLI 进程 | 是 |
| `process_error` | environment | CLI 进程异常退出 | 否 |
| `sdk_error` | sdk | Claude SDK 内部错误 | 否 |
| `internal_error` | server | 服务端未预期的内部错误 | 否 |

> 一次对话中可能出现多条 `error` 事件（例如 `AssistantMessage` 级别的模型错误 + `ResultMessage` 级别的结果错误）。
> 出现 `error` 事件后，仍会紧跟一条 `done` 事件。

---

#### ⑤ `aborted` — 任务被中断（调用 `/chat/abort` 后推送）

```json
{
  "type": "aborted",
  "data": {
    "task_id": "task_abc123def456",
    "usage": {
      "input_tokens": 500,
      "output_tokens": 120
    },
    "total_cost_usd": 0.005,
    "session_id": "550e8400-e29b-41d4-a716-446655440000"
  }
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `data.task_id` | string | 被中断的任务 ID |
| `data.usage` | object \| 无 | 中断前已产生的 token 消耗（可能为空） |
| `data.total_cost_usd` | float \| 无 | 中断前已产生的费用（可能为空） |
| `data.session_id` | string \| 无 | 会话 ID（可能为空） |

> 费用信息取决于中断时机：如果 Claude 尚未返回 `ResultMessage`，则费用字段为空。
> `aborted` 事件后仍会紧跟一条 `done` 事件。

---

#### ⑥ `done` — 流结束（最后一条，必然出现）

```json
{
  "type": "done",
  "data": null
}
```

收到此事件后，连接关闭。

---

### 完整事件流示例

**正常流程：**

```
data: {"type":"task_start","data":{"task_id":"task_abc123def456"}}

data: {"type":"message","data":{"text":"我来帮你创建温度显示程序..."}}

data: {"type":"message","data":{"text":"正在查询 SHT30 传感器 API..."}}

data: {"type":"message","data":{"text":"代码已写入 main.py。"}}

data: {"type":"file","data":{"name":"main.py","content":"import os, sys, io\nfrom m5stack import *\n..."}}

data: {"type":"result","data":{"session_id":"550e8400-...","total_cost_usd":0.0123,"usage":{...},"stop_reason":"end_turn","duration_ms":5432,"num_turns":3,"is_error":false}}

data: {"type":"done","data":null}
```

**错误流程（如 API 限流）：**

```
data: {"type":"task_start","data":{"task_id":"task_abc123def456"}}

data: {"type":"message","data":{"text":"我来帮你..."}}

data: {"type":"error","data":{"error_code":"rate_limit","message":"请求频率超限，请稍后重试","category":"model","retryable":true}}

data: {"type":"result","data":{"session_id":"550e8400-...","is_error":true,...}}

data: {"type":"done","data":null}
```

**环境错误（如 CLI 未安装）：**

```
data: {"type":"task_start","data":{"task_id":"task_abc123def456"}}

data: {"type":"error","data":{"error_code":"cli_not_found","message":"Claude Code not found at: /usr/bin/claude","category":"environment","retryable":false}}

data: {"type":"done","data":null}
```

**中断流程（客户端调用 `/chat/abort`）：**

```
data: {"type":"task_start","data":{"task_id":"task_abc123def456"}}

data: {"type":"message","data":{"text":"我来帮你创建温度显示程序..."}}

data: {"type":"message","data":{"text":"正在查询传感器 API..."}}

data: {"type":"aborted","data":{"task_id":"task_abc123def456","usage":{"input_tokens":500,"output_tokens":120},"total_cost_usd":0.005,"session_id":"550e8400-..."}}

data: {"type":"done","data":null}
```

---

---

## POST `/chat/abort` — 中断对话任务

在对话进行中，客户端可调用此接口中断当前任务。

```http
POST /chat/abort
Content-Type: application/json
```

### 请求体

```json
{
  "task_id": "task_abc123def456"
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `task_id` | string | 是 | 从 SSE `task_start` 事件中获取的任务 ID |

### 响应 `200`

```json
{
  "message": "已请求中断",
  "task_id": "task_abc123def456"
}
```

### 响应 `404`

```json
{"detail": "任务不存在或已结束"}
```

### 使用说明

1. 客户端发起 `/chat` 请求后，从第一个 SSE 事件 `task_start` 中获取 `task_id`
2. 需要中断时，调用 `POST /chat/abort` 传入 `task_id`
3. 服务端会优雅终止 Claude 进程（等待 flush → SIGTERM → SIGKILL）
4. SSE 流会收到 `aborted` 事件（附带已产生的费用信息，如有），随后收到 `done` 事件
5. 中断后该 `task_id` 失效，重复调用会返回 404

---

## 典型调用流程

### 场景 1：首次创建项目并对话

```
1. POST /projects          → 获取 project_id（一次性，长期保存）
2. POST /chat              → 传入 project_id + prompt，session_id=null
   ├─ 收到 task_start 事件，保存 task_id（用于中断）
   ├─ 实时展示 message 事件
   ├─ 保存 file 事件中的代码
   └─ 从 result 事件中记录 session_id（如 "550e8400-..."）
```

### 场景 2：在同一项目内继续对话（连续多轮）

```
3. POST /chat              → 传入 project_id + prompt，session_id="550e8400-..."
   └─ Claude 会加载该会话的完整历史，基于上下文继续回答
4. POST /chat              → 再次传入相同 session_id，实现多轮连续对话
```

### 场景 3：查看历史会话

```
5. GET /projects/{project_id}
   └─ 列出该项目下所有历史会话（含 session_id、摘要、时间）
6. GET /projects/{project_id}/sessions/{session_id}/messages
   └─ 获取指定会话的完整对话记录（user/assistant 消息列表）
```

### 场景 4：清理项目

```
7. DELETE /projects/{project_id}
   └─ 删除项目及所有文件（慎用）
```

### 场景 5：中断正在进行的对话

```
1. POST /chat              → 开始对话
   ├─ 收到 task_start 事件，保存 task_id
   └─ 收到若干 message 事件...
2. POST /chat/abort        → 传入 task_id，请求中断
3. SSE 流收到 aborted 事件（含已产生的费用信息）
4. SSE 流收到 done 事件，连接关闭
```

---

## 错误码

### HTTP 错误码

| HTTP 状态码 | 说明 |
|-------------|------|
| `200` | 成功（SSE 流内的错误通过 `error` 事件传递） |
| `404` | 项目不存在 |
| `422` | 请求体格式错误（字段缺失或类型错误） |
| `500` | 服务端内部错误（流式响应开始前的异常） |

### SSE 错误分类

| category | 含义 | 典型场景 |
|----------|------|----------|
| `model` | Anthropic API / 模型层面的错误 | 认证失败、限流、计费异常、对话异常终止 |
| `environment` | 运行环境错误 | CLI 未安装、CLI 进程崩溃、连接断开 |
| `sdk` | Claude SDK 内部错误 | SDK 解析异常等 |
| `server` | 本服务未预期的错误 | 代码 bug、文件系统异常等 |

> 下游建议：对 `retryable: true` 的错误可实现自动重试（建议指数退避），对 `retryable: false` 的错误应提示用户检查配置或联系管理员。

---

## 注意事项

1. **超时**：Claude 单次处理通常需要 **30～120 秒**，调用方客户端超时应设为至少 **300 秒**。
2. **会话继续**：
   - 首次对话传 `session_id=null`，从 `result` 事件获取新的 `session_id`
   - 后续对话传入相同 `session_id`，Claude 会加载完整历史继续对话
   - 不同 `session_id` 的对话完全隔离，可在同一项目内管理多个独立任务
3. **文件路径**：生成的 `main.py` 保存在该项目的 `working_directory` 中，可直接刷写到 M5Stack 设备。
4. **并发**：服务支持并发请求，不同项目之间完全隔离。
5. **缓存**：`cache_read_input_tokens` 越大说明 Prompt Caching 命中率越高，重复类似任务时成本会明显降低。
6. **端口**：V2 服务默认监听 **8880**，注意与旧版 8000 区分。
7. **对话日志**：每次对话（含正常完成、错误、中断）都会记录到 `projects_data/logs/{project_id}.jsonl`，格式为每行一条 JSON。日志独立于项目目录，删除项目时不会被清除。日志字段包括：

   ```json
   {
     "timestamp": "2026-04-20T15:30:00.123456",
     "project_id": "proj_abc123def456",
     "project_name": "温度监控项目",
     "task_id": "task_abc123def456",
     "prompt": "创建温度传感器读取代码",
     "session_id": "550e8400-...",
     "duration_ms": 5432,
     "num_turns": 3,
     "usage": {"input_tokens": 1000, "output_tokens": 500, "cache_read_input_tokens": 800},
     "total_cost_usd": 0.0123,
     "stop_reason": "end_turn",
     "is_error": false,
     "aborted": false,
     "error": null
   }
   ```
