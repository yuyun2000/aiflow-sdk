# 网页客户端接入指南

本指南面向免账号登录、完成 UIFlow 配对后进行 Coding 和设备运行的普通浏览器客户端。浏览器只访问公网 BFF，不保存核心签名密钥。

仓库已提供同源参考客户端：启动服务后打开 `http://<服务端局域网 IP>:8880/client`，或运行 `./manage.sh client`。它覆盖本指南中的连接、附件、Coding、SSE/轮询恢复、文件、取消和直接运行流程，可用于实际 `deviceId + clientId` 联调。

## 1. 接入前提

服务默认监听 `0.0.0.0:8880`。内置客户端使用同源 API，无需添加 CORS；独立部署的网页应把精确 Origin 加入 [server_config.json](../server_config.json) 的 `server.cors_origins`。生产环境不要使用通配 CORS，并使用 HTTPS。

服务端信任客户端初始化时提交的 `deviceId` 和 `clientId`。如果服务对其他主机开放，应由可信配对网关验证设备关系或签名设备断言；CORS 和能力令牌都不证明配对归属。

```js
const API_BASE = window.location.origin;
const TOKEN_PREFIX = "aiflow.token.";

function tokenKey(deviceId) {
  return `${TOKEN_PREFIX}${deviceId}`;
}

function authHeaders(deviceId, json = true) {
  const token = sessionStorage.getItem(tokenKey(deviceId));
  if (!token) throw new Error("AIFlow device project is not connected");
  return {
    ...(json ? { "Content-Type": "application/json" } : {}),
    "X-AIFlow-Context-Token": token,
  };
}
```

网关自动设置 `aiflow_web_session` HttpOnly Cookie 并在服务端完成内部签名。前端继续使用普通同源 `fetch`，不要添加 `X-AIFlow-Client-*` 头，也不要把任何长期密钥放入 JavaScript、Cookie 或 Web Storage。修改请求必须带浏览器自动生成的同源 `Origin`；独立前端域名需精确加入 `server.cors_origins`。

前端的项目、任务和历史索引统一使用配对得到的 `deviceId`。`clientId` 随设备项目保存并用于资源上传；服务端内部 `context_id` 只作为实现细节保存，不能作为 Client ID 兜底。

## 2. 配对后连接或重连

```js
async function connectDevice(pairedDevice) {
  if (!pairedDevice.deviceId) throw new Error("paired deviceId is required");
  if (!pairedDevice.clientId) throw new Error("clientId is required");
  const response = await fetch(`${API_BASE}/api/v3/contexts`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      label: `web-${pairedDevice.deviceId}`,
      device: {
        device_id: pairedDevice.deviceId,
        client_id: pairedDevice.clientId,
        product: pairedDevice.product ?? null,
        firmware_version: pairedDevice.firmwareVersion ?? null,
        capabilities: pairedDevice.capabilities ?? {},
      },
    }),
  });
  if (!response.ok) throw await apiError(response);
  const project = await response.json();
  sessionStorage.setItem(tokenKey(project.device_id), project.access_token);
  return project;
}
```

- 首次连接返回 `201`、`created=true`。
- 相同 `deviceId` 重连返回 `200`、`created=false`，复用原工作区和 Claude 历史，并以本次传入值更新 `clientId`。
- 重连会轮换令牌，旧标签页令牌立即失效；始终用响应中的新令牌覆盖缓存。
- 响应自带 `system_status`，前端可立即展示当前会话和队列容量。

新设备达到总会话容量会返回 `503 session_capacity_full`，已有设备不受影响，仍可重连。

## 3. 实时系统容量

```js
async function getSystemStatus() {
  const response = await fetch(`${API_BASE}/api/v3/system/status`);
  if (!response.ok) throw await apiError(response);
  return response.json();
}
```

该接口不返回设备信息，可在连接页每 2 至 5 秒轮询：

- `sessions.accepting_new=false`：禁用新设备连接，允许已存在 deviceId 重连。
- `sessions.used`：持久化设备项目占用；`sessions.recently_active`：最近活动窗口内的设备数。
- `tasks.accepting_new=false`：禁用新的 Coding/重跑按钮。
- `tasks.running`：正在执行的总任务数。
- `tasks.queued`：等待执行槽的总任务数。

后端保持同一 `deviceId` 同时最多一个任务，并在全局范围限制执行数和排队数。

## 4. Base64 图片和语音

前端把 `File` 转为原始 Base64，不发送 `data:<mime>;base64,` 前缀：

```js
async function fileToAttachment(file, kind) {
  const bytes = new Uint8Array(await file.arrayBuffer());
  let binary = "";
  const chunkSize = 0x8000;
  for (let offset = 0; offset < bytes.length; offset += chunkSize) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + chunkSize));
  }
  return {
    kind,
    mime_type: file.type,
    name: file.name,
    data_base64: btoa(binary),
  };
}
```

浏览器应在编码前检查 `/api/v3/capabilities` 返回的附件数量、单文件和总大小限制，避免无意义地生成大字符串。

`name` 必须由客户端提供，服务端会按该名称保存并把相对路径交给 Agent。只传 `File.name` 这样的单个文件名，不要传本地绝对路径或目录；扩展名必须与 `mime_type` 一致，同一消息内的名称不能重复（忽略大小写和 Unicode 等价形式）。

## 5. 提交 Coding

```js
async function startCoding(deviceId, { text = "", images = [], audio = [], deployMode = "none" }) {
  const imageAttachments = await Promise.all(images.map((file) => fileToAttachment(file, "image")));
  const audioAttachments = await Promise.all(audio.map((file) => fileToAttachment(file, "audio")));
  const response = await fetch(`${API_BASE}/api/v3/tasks/coding`, {
    method: "POST",
    headers: authHeaders(deviceId),
    body: JSON.stringify({
      prompt: text,
      attachments: [...imageAttachments, ...audioAttachments],
      deploy_mode: deployMode,
    }),
  });
  if (!response.ok) throw await apiError(response);
  return response.json();
}
```

支持纯文字、纯图片/语音或混合消息。服务端解码后以客户端提供的 `name` 保存到当前设备项目的 `inputs/<conversation_id>/<task_id>/`，并把含该文件名的相对路径传给 Agent。前端不需要再单独上传同一附件。

按钮建议：

- “生成代码”：`deploy_mode=none`。
- “生成并运行”：`deploy_mode=server`。
- 只有明确需要 Agent 自行控制部署步骤时使用 `agent`。

任务返回 `202` 后立即进入任务界面。`queue_position` 不为空时展示“排队第 N 位”，不要让 HTTP 请求等待 Agent 完成。

## 6. SSE 与恢复

```js
function watchTask(task, handlers) {
  const url = new URL(`${API_BASE}${task.events_url}`);
  url.searchParams.set("stream_token", task.stream_token);
  const source = new EventSource(url);
  const types = [
    "task_queued", "task_started", "agent_connected", "agent_system",
    "agent_status", "agent_warning", "agent_reasoning", "agent_partial_capture",
    "agent_stream_event",
    "agent_sdk_event", "agent_user_message", "agent_user_content",
    "assistant_message_started", "assistant_text_delta", "assistant_message",
    "assistant_message_finished", "tool_started", "tool_finished",
    "server_tool_started", "server_tool_finished", "agent_rate_limit",
    "agent_result", "agent_result_error",
    "file_ready", "deployment_started", "deployment_finished",
    "cancellation_requested", "task_completed", "task_failed",
    "task_cancelled", "heartbeat",
  ];
  for (const type of types) {
    source.addEventListener(type, (event) => {
      const payload = JSON.parse(event.data);
      handlers.onEvent?.(type, payload);
      if (["task_completed", "task_failed", "task_cancelled"].includes(type)) {
        source.close();
        handlers.onTerminal?.(type, payload);
      }
    });
  }
  source.onerror = () => handlers.onDisconnect?.();
  return () => source.close();
}
```

SSE token 只读一个任务。SSE 断开不影响后台执行，改用状态轮询和历史补偿：

SSE 在事件写入后立即唤醒连接，不按固定时间轮询。`assistant_text_delta` 和 `agent_reasoning` 增量都带 `finalized=false`，正文分别是 `text` 和 `thinking`；按内容类型分别以 `response_id + block_index` 追加到同一块。最终 `assistant_message` 和 `agent_reasoning` 带 `finalized=true`，用 SDK 最终完整内容覆盖校准，不能再追加一份。收到最终块后忽略该键的晚到 delta。`agent_partial_capture` 的 thinking 是流中断前累计内容，应覆盖校准并标记未完整。`assistant_message_started/finished` 是响应级事件，`finished` 应结束相同 `response_id` 下的全部文本块。不要用 SDK 外层 `message_uuid` 关联增量与最终消息，因为它可能不同。

`tool_started/tool_finished` 用 `tool_use_id` 关联，但应分别展示真实完整输入与真实结果，不要改写成“正在组织参数”等推测文案。逐字符 `input_json_delta`、签名碎片和高频 `thinking_tokens` 没有独立展示价值，服务端不会单独持久化或下发；其他 `agent_stream_event` 会保留并脱敏。SDK 实际提供的 `thinking_delta` 会逐片通过 `agent_reasoning` 公开，内置客户端将其独立显示为“模型思考”。thinking 和其他事件仍会脱敏凭据、设备标识、签名和服务端绝对路径。内置客户端保留事件总计数，但原始事件 DOM 只显示最近 `2000` 条。

```js
async function getTask(deviceId, taskId) {
  const response = await fetch(`${API_BASE}/api/v3/tasks/${taskId}`, {
    headers: authHeaders(deviceId, false),
  });
  if (!response.ok) throw await apiError(response);
  return response.json();
}
```

状态中的 `queue_position`、`stage`、`possibly_stalled` 是权威生命周期信息。`progress` 是兼容字段，只会在排队/运行时为 `0`、终态为 `100`；它不是模型完成百分比，客户端不得显示成 20%、50% 等进度，也不得据此推测模型正在做什么。`possibly_stalled=true` 时展示警告和取消按钮，不要自动重复请求。

## 7. deviceId 项目与历史恢复

```js
async function getProject(deviceId) {
  const response = await fetch(`${API_BASE}/api/v3/project`, {
    headers: authHeaders(deviceId, false),
  });
  if (!response.ok) throw await apiError(response);
  const project = await response.json();
  if (project.device_id !== deviceId) throw new Error("device project mismatch");
  return project;
}
```

`GET /api/v3/project` 一次返回文件、当前 conversation、Claude session 摘要和 `active_task_id`。页面刷新后：

1. 用当前 `deviceId` 读取项目。
2. 有 `active_task_id` 时读取任务状态和事件历史。
3. 重新建立 SSE。
4. 令牌丢失或失效时重新调用 `connectDevice`，原项目历史不会新建一份。

会话详情：`GET /api/v3/conversations/{session_id}/messages`。所有会话响应都带 `device_id`，前端应校验后再写入本地历史缓存。

## 8. 直接重跑

```js
async function rerunSavedCode(deviceId) {
  const body = JSON.stringify({ code_path: "main.py", include_resources: true });
  const plan = await fetch(`${API_BASE}/api/v3/deployments/plan`, {
    method: "POST",
    headers: authHeaders(deviceId),
    body,
  });
  if (!plan.ok) throw await apiError(plan);

  const response = await fetch(`${API_BASE}/api/v3/tasks/direct-run`, {
    method: "POST",
    headers: authHeaders(deviceId),
    body,
  });
  if (!response.ok) throw await apiError(response);
  return response.json();
}
```

直接重跑跳过 Agent，但仍占用全局执行槽，队列满时同样返回 `429 task_queue_full`。

## 9. 文件、取消和删除

- 文件列表：`GET /api/v3/files`。
- 文件下载：`GET /api/v3/files/{relativePath}`。
- multipart 上传：`POST /api/v3/files`；不要手动上传 `.aiflow` 内部文件。
- 取消：`POST /api/v3/tasks/{task_id}/cancel`。排队任务会立即释放容量。
- 新对话：`POST /api/v3/conversation/reset`。
- 删除设备项目：`DELETE /api/v3/context?confirm=true`。

服务端不会自动过期设备项目。用户明确删除项目或解除配对时应调用删除接口；生产部署还需要运维保留期策略。

## 10. 错误处理

```js
async function apiError(response) {
  let payload;
  try {
    payload = await response.json();
  } catch {
    payload = { detail: { code: "http_error", message: response.statusText } };
  }
  const detail = payload.detail ?? payload;
  const error = new Error(detail.message ?? `HTTP ${response.status}`);
  error.code = detail.code ?? "http_error";
  error.status = response.status;
  error.taskId = detail.task_id;
  error.systemStatus = detail.system_status;
  return error;
}
```

关键分支：

- `cross_site_request_rejected`：页面 Origin 未与 API 同源且未加入 `server.cors_origins`。
- `web_rate_limit_*` / `ai_task_limit_*`：匿名会话、IP 或核心费用保护触发，按窗口等待，不要自动高频重试。
- `session_capacity_full`：新设备容量已满；显示 `system_status.sessions`。
- `task_queue_full`：执行和等待容量都满；等待后重试，不要自动高频循环。
- `context_busy`：恢复返回的 `task_id`，不要创建同设备第二个任务。
- `invalid_context_token`：重新用同一 `deviceId` 连接并替换令牌。
- `invalid_attachment_name` / `duplicate_attachment_name`：只传单个文件名，并在提交前检查消息内重名。
- `attachment_extension_mismatch` / `unsupported_attachment_type`：检查文件扩展名和 MIME 的对应关系。
- `invalid_attachment_base64`：检查 Base64 编码。
- `attachment_too_large` / `attachments_too_large`：按 capabilities 限制重新选择文件。
