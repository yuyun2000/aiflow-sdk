"use strict";

const { applyAssistantStreamEvent, applyReasoningStreamEvent } = window.AIFlowAssistantStream;

const TOKEN_HEADER = "X-AIFlow-Context-Token";
const CLIENT_KEY_HEADER = "X-AIFlow-Client-Key";
const CLIENT_TIMESTAMP_HEADER = "X-AIFlow-Timestamp";
const CLIENT_NONCE_HEADER = "X-AIFlow-Nonce";
const CLIENT_CONTENT_HASH_HEADER = "X-AIFlow-Content-SHA256";
const CLIENT_SIGNATURE_HEADER = "X-AIFlow-Signature";
const RESPONSE_TIMESTAMP_HEADER = "X-AIFlow-Response-Timestamp";
const RESPONSE_SIGNATURE_HEADER = "X-AIFlow-Response-Signature";
const CLIENT_AUTH_SCHEME = "AIFLOW-HMAC-SHA256-V1";
const ACTIVE_DEVICE_KEY = "aiflow.activeDevice";
const TOKEN_PREFIX = "aiflow.token.";
const RAW_STREAM_VISIBLE_LIMIT = 2000;
const RAW_STREAM_CHUNK_LINES = 100;
const TERMINAL = new Set(["completed", "failed", "cancelled"]);
const TASK_STATUSES = new Set(["queued", "running", "completed", "failed", "cancelled"]);
const RAW_ONLY_AGENT_EVENTS = new Set([
  "agent_connected",
  "agent_status",
  "agent_system",
  "agent_stream_event",
  "agent_sdk_event",
  "agent_user_message",
  "agent_user_content",
]);
const EVENT_TYPES = [
  "task_queued",
  "task_started",
  "ai_quota_authorized",
  "ai_quota_settled",
  "ai_quota_settlement_pending",
  "ai_quota_released",
  "ai_quota_release_failed",
  "agent_connected",
  "agent_status",
  "agent_system",
  "agent_warning",
  "agent_reasoning",
  "agent_partial_capture",
  "agent_stream_event",
  "agent_sdk_event",
  "agent_user_message",
  "agent_user_content",
  "assistant_message_started",
  "assistant_text_delta",
  "assistant_message",
  "assistant_message_finished",
  "tool_started",
  "tool_finished",
  "server_tool_started",
  "server_tool_finished",
  "agent_rate_limit",
  "agent_result",
  "agent_result_error",
  "file_ready",
  "deployment_started",
  "deployment_finished",
  "cancellation_requested",
  "task_completed",
  "task_failed",
  "task_cancelled",
  "heartbeat",
];

const RUNTIME_EVENT_TYPES = new Set([
  "task_queued",
  "task_started",
  "ai_quota_authorized",
  "ai_quota_settled",
  "ai_quota_settlement_pending",
  "ai_quota_released",
  "ai_quota_release_failed",
  "cancellation_requested",
  "task_completed",
  "task_failed",
  "task_cancelled",
  "task_stalled",
  "stream_connecting",
  "stream_connected",
  "stream_reconnecting",
  "heartbeat",
]);

const EVENT_LABELS = {
  task_queued: "任务排队",
  task_started: "任务启动",
  ai_quota_authorized: "AI 额度授权",
  ai_quota_settled: "AI 额度结算",
  ai_quota_settlement_pending: "AI 额度待核对",
  ai_quota_released: "AI 额度释放",
  ai_quota_release_failed: "AI 额度异常",
  cancellation_requested: "取消请求",
  task_completed: "任务完成",
  task_failed: "任务失败",
  task_cancelled: "任务取消",
  task_stalled: "响应检查",
  stream_connecting: "事件连接",
  stream_connected: "事件连接",
  stream_reconnecting: "事件连接",
  heartbeat: "任务心跳",
  agent_connected: "Agent 连接",
  agent_status: "Agent 状态",
  agent_system: "系统事件",
  agent_warning: "Agent 警告",
  agent_reasoning: "模型分析",
  agent_partial_capture: "模型思考",
  agent_stream_event: "模型流事件",
  agent_sdk_event: "SDK 事件",
  agent_user_message: "上下文消息",
  agent_user_content: "上下文内容",
  assistant_message_started: "模型回复",
  assistant_text_delta: "模型回复",
  assistant_message: "模型回复",
  assistant_message_finished: "模型回复",
  tool_started: "工具调用",
  tool_finished: "工具结果",
  server_tool_started: "服务端工具",
  server_tool_finished: "服务端工具",
  agent_rate_limit: "模型限流",
  agent_result: "Agent 结果",
  agent_result_error: "Agent 错误",
  file_ready: "项目文件",
  deployment_started: "设备推送",
  deployment_finished: "设备推送",
};

const STAGE_LABELS = {
  queued: "等待执行",
  running: "正在执行",
  authorizing_ai_quota: "检查 AI Token 额度",
  preparing_workspace: "准备独立工作区",
  coding: "启动 M5Stack 编程 Agent",
  collecting_files: "整理生成文件",
  validating_deployment: "检查设备推送参数",
  deploying: "推送到设备",
  finalizing: "整理结果",
  completed: "任务完成",
  failed: "任务失败",
  cancelled: "任务已取消",
};

const ui = Object.fromEntries(
  [
    "health-dot", "health-label", "model-label", "capacity-label",
    "device-form", "device-id", "client-id", "mac-address", "product", "connect-button", "connect-error",
    "connection-state", "workspace", "coding-form", "prompt", "image-input",
    "audio-input", "attachment-summary", "submit-button", "cancel-button",
    "coding-error", "task-state", "task-stage",
    "queue-position", "runtime-log", "agent-log", "runtime-summary",
    "raw-stream", "raw-summary",
    "refresh-task", "refresh-project", "file-list",
    "rerun-button", "project-file-input", "reset-button", "active-device", "active-client",
    "conversation-id", "claude-session", "toast",
  ].map((id) => [id, document.getElementById(id)])
);

const state = {
  token: null,
  deviceId: null,
  clientId: null,
  capabilities: null,
  taskId: null,
  stream: null,
  pollTimer: null,
  pollInFlight: false,
  serviceTimer: null,
  toastTimer: null,
  eventSequences: new Set(),
  assistantRows: new Map(),
  reasoningRows: new Map(),
  toolRows: new Map(),
  runtimeRows: new Map(),
  rawChunks: [],
  rawVisibleEventCount: 0,
  rawPending: [],
  rawFlushTimer: null,
  rawEventCount: 0,
  lastEventSequence: 0,
  historyPromise: null,
  historyLoading: false,
};

function tokenKey(deviceId) {
  return `${TOKEN_PREFIX}${deviceId}`;
}

function setError(element, message) {
  element.textContent = message || "";
  element.hidden = !message;
}

function showToast(message, isError = false) {
  clearTimeout(state.toastTimer);
  ui.toast.textContent = message;
  ui.toast.classList.toggle("error", isError);
  ui.toast.hidden = false;
  state.toastTimer = setTimeout(() => {
    ui.toast.hidden = true;
  }, 4200);
}

async function api(path, options = {}, authenticated = true) {
  const headers = new Headers(options.headers || {});
  if (authenticated) {
    if (!state.token) throw new Error("设备项目未连接");
    headers.set(TOKEN_HEADER, state.token);
  }
  const url = new URL(path, window.location.origin);
  const request = new Request(url, { ...options, headers });
  const authRequired = Boolean(state.capabilities?.client_auth?.enabled);
  let requestNonce = null;
  if (authRequired) {
    const bridge = window.aiflowClientAuth;
    if (!bridge?.keyId || typeof bridge.sign !== "function") {
      throw new Error("当前服务只接受官方客户端签名请求");
    }
    const bytes = await request.clone().arrayBuffer();
    const digest = await crypto.subtle.digest("SHA-256", bytes);
    const contentHash = Array.from(new Uint8Array(digest), (value) => value.toString(16).padStart(2, "0")).join("");
    const timestamp = String(Math.floor(Date.now() / 1000));
    requestNonce = randomNonce();
    const target = `${url.pathname}${url.search}`;
    const canonical = [CLIENT_AUTH_SCHEME, request.method, target, timestamp, requestNonce, contentHash].join("\n");
    const signature = await bridge.sign(canonical, {
      method: request.method,
      target,
      timestamp,
      nonce: requestNonce,
      contentHash,
    });
    request.headers.set(CLIENT_KEY_HEADER, bridge.keyId);
    request.headers.set(CLIENT_TIMESTAMP_HEADER, timestamp);
    request.headers.set(CLIENT_NONCE_HEADER, requestNonce);
    request.headers.set(CLIENT_CONTENT_HASH_HEADER, contentHash);
    request.headers.set(CLIENT_SIGNATURE_HEADER, signature);
  }
  const response = await fetch(request);
  if (authRequired && typeof window.aiflowClientAuth?.verifyResponse === "function") {
    const responseTimestamp = response.headers.get(RESPONSE_TIMESTAMP_HEADER) || "";
    const responseSignature = response.headers.get(RESPONSE_SIGNATURE_HEADER) || "";
    if (responseTimestamp || responseSignature) {
      if (!responseTimestamp || !responseSignature) throw new Error("服务端响应签名不完整");
      const canonical = [`${CLIENT_AUTH_SCHEME}-RESPONSE`, requestNonce, String(response.status), responseTimestamp].join("\n");
      const verified = await window.aiflowClientAuth.verifyResponse(canonical, responseSignature);
      if (!verified) throw new Error("服务端响应签名验证失败");
    } else if (response.ok) {
      throw new Error("服务端成功响应缺少签名");
    }
  }
  if (!response.ok) throw await apiError(response);
  if (response.status === 204) return null;
  return response.json();
}

function randomNonce() {
  const bytes = new Uint8Array(18);
  crypto.getRandomValues(bytes);
  return btoa(String.fromCharCode(...bytes)).replaceAll("+", "-").replaceAll("/", "_").replaceAll("=", "");
}

async function apiError(response) {
  let payload = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }
  const detail = payload?.detail || payload || {};
  const error = new Error(detail.message || `HTTP ${response.status}`);
  error.code = detail.code || "http_error";
  error.status = response.status;
  error.taskId = detail.task_id || null;
  return error;
}

function setConnected(connected) {
  ui.workspace.setAttribute("aria-disabled", connected ? "false" : "true");
  ui["connection-state"].textContent = connected ? "已连接" : "未连接";
  ui["connection-state"].classList.toggle("connected", connected);
  ui["submit-button"].disabled = !connected;
  ui["rerun-button"].disabled = !connected;
  ui["refresh-project"].disabled = !connected;
  ui["reset-button"].disabled = !connected;
  ui["project-file-input"].disabled = !connected;
}

function setBusy(busy) {
  ui["submit-button"].disabled = busy || !state.token;
  ui["rerun-button"].disabled = busy || !state.token;
  ui["cancel-button"].disabled = !busy || !state.taskId;
  ui["refresh-task"].disabled = !state.taskId;
}

function compactId(value) {
  if (!value) return "--";
  if (value.length <= 22) return value;
  return `${value.slice(0, 10)}...${value.slice(-8)}`;
}

function formatBytes(bytes) {
  if (!Number.isFinite(bytes)) return "--";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function selectedDeployMode() {
  return document.querySelector('input[name="deployMode"]:checked').value;
}

function updateSubmitLabel() {
  const labels = {
    none: "生成代码",
    server: "生成并由服务端推送",
    agent: "生成并授权 Agent 推送",
  };
  ui["submit-button"].textContent = labels[selectedDeployMode()];
}

async function refreshService() {
  try {
    const [health, capabilities, capacity] = await Promise.all([
      api("/health", {}, false),
      api("/api/v3/capabilities", {}, false),
      api("/api/v3/system/status", {}, false),
    ]);
    state.capabilities = capabilities;
    ui["health-dot"].className = "status-dot online";
    ui["health-label"].textContent = `服务 ${health.version}`;
    ui["model-label"].textContent = `model: ${capabilities.model}`;
    updateCapacity(capacity);
  } catch (error) {
    ui["health-dot"].className = "status-dot offline";
    ui["health-label"].textContent = "服务不可用";
    ui["capacity-label"].textContent = "task: --";
  }
}

async function refreshCapacity() {
  if (document.hidden) return;
  try {
    const capacity = await api("/api/v3/system/status", {}, false);
    updateCapacity(capacity);
    ui["health-dot"].className = "status-dot online";
  } catch {
    ui["health-dot"].className = "status-dot offline";
    ui["capacity-label"].textContent = "task: --";
  }
}

function updateCapacity(capacity) {
  const tasks = capacity.tasks;
  ui["capacity-label"].textContent = `task: ${tasks.running} 运行 / ${tasks.queued} 排队 / ${tasks.available} 可用`;
}

async function connectDevice(deviceId, clientId, macAddress, product) {
  const device = {
    device_id: deviceId,
    client_id: clientId,
    product: product || null,
  };
  if (macAddress) device.mac_address = macAddress;
  const result = await api(
    "/api/v3/contexts",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        label: "AIFlow local web client",
        device,
      }),
    },
    false
  );
  state.deviceId = result.device_id;
  state.clientId = result.client_id;
  state.token = result.access_token;
  ui["mac-address"].value = result.mac_address || result.device?.mac_address || "";
  sessionStorage.setItem(ACTIVE_DEVICE_KEY, state.deviceId);
  sessionStorage.setItem(tokenKey(state.deviceId), state.token);
  setConnected(true);
  updateCapacity(result.system_status);
  ui["active-device"].textContent = compactId(state.deviceId);
  ui["active-client"].textContent = compactId(state.clientId);
  ui["conversation-id"].textContent = compactId(result.conversation_id);
  ui["claude-session"].textContent = "--";
  await refreshProject();
  showToast(result.created ? "设备项目已创建" : "设备项目已重连");
}

async function restoreSession() {
  const deviceId = sessionStorage.getItem(ACTIVE_DEVICE_KEY);
  if (!deviceId) return;
  const token = sessionStorage.getItem(tokenKey(deviceId));
  if (!token) return;
  state.deviceId = deviceId;
  state.token = token;
  ui["device-id"].value = deviceId;
  try {
    setConnected(true);
    await refreshProject();
  } catch (error) {
    state.deviceId = null;
    state.clientId = null;
    state.token = null;
    sessionStorage.removeItem(tokenKey(deviceId));
    setConnected(false);
  }
}

async function refreshProject() {
  const project = await api("/api/v3/project");
  if (project.device_id !== state.deviceId) throw new Error("设备项目标识不匹配");
  if (!project.client_id) throw new Error("设备项目缺少 clientId，请重新连接");
  state.clientId = project.client_id;
  ui["client-id"].value = state.clientId;
  ui["mac-address"].value = project.mac_address || "";
  renderFiles(project.files || []);
  ui["active-device"].textContent = compactId(project.device_id);
  ui["active-client"].textContent = compactId(project.client_id);
  ui["conversation-id"].textContent = compactId(project.conversation_id);
  ui["claude-session"].textContent = compactId(project.current_session_id);
  if (project.active_task_id && project.active_task_id !== state.taskId) {
    state.taskId = project.active_task_id;
    clearEvents();
    await loadEventHistory(state.taskId);
    startPolling(state.taskId);
  }
  return project;
}

function renderFiles(files) {
  ui["file-list"].replaceChildren();
  if (!files.length) {
    const empty = document.createElement("li");
    empty.className = "empty-row";
    empty.textContent = "暂无项目文件";
    ui["file-list"].append(empty);
    return;
  }
  for (const file of files) {
    const row = document.createElement("li");
    const name = document.createElement("span");
    name.className = "file-name";
    name.textContent = file.path;
    const size = document.createElement("span");
    size.className = "file-size";
    size.textContent = formatBytes(file.size);
    const download = document.createElement("button");
    download.type = "button";
    download.className = "file-download";
    download.textContent = "下载";
    download.addEventListener("click", () => downloadFile(file.path));
    row.append(name, size, download);
    ui["file-list"].append(row);
  }
}

async function downloadFile(path) {
  try {
    const response = await fetch(`/api/v3/files/${path.split("/").map(encodeURIComponent).join("/")}`, {
      headers: { [TOKEN_HEADER]: state.token },
    });
    if (!response.ok) throw await apiError(response);
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = path.split("/").pop() || "download";
    document.body.append(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  } catch (error) {
    showToast(error.message, true);
  }
}

function selectedAttachments() {
  return [...ui["image-input"].files, ...ui["audio-input"].files];
}

function updateAttachmentSummary() {
  const files = selectedAttachments();
  if (!files.length) {
    ui["attachment-summary"].textContent = "未选择附件";
    return;
  }
  const total = files.reduce((sum, file) => sum + file.size, 0);
  ui["attachment-summary"].textContent = `${files.length} 个附件 / ${formatBytes(total)}`;
}

function validateAttachments(files) {
  const capabilities = state.capabilities || {};
  const maxCount = capabilities.max_attachments || 6;
  const maxFile = capabilities.max_attachment_bytes || 10 * 1024 * 1024;
  const maxTotal = capabilities.max_attachment_total_bytes || 20 * 1024 * 1024;
  if (files.length > maxCount) throw new Error(`附件不能超过 ${maxCount} 个`);
  const tooLarge = files.find((file) => file.size > maxFile);
  if (tooLarge) throw new Error(`${tooLarge.name} 超过 ${formatBytes(maxFile)}`);
  const total = files.reduce((sum, file) => sum + file.size, 0);
  if (total > maxTotal) throw new Error(`附件总大小超过 ${formatBytes(maxTotal)}`);
}

async function fileToAttachment(file) {
  const kind = file.type.startsWith("image/") ? "image" : file.type.startsWith("audio/") ? "audio" : null;
  if (!kind) throw new Error(`${file.name} 不是支持的图片或音频`);
  const dataUrl = await new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(new Error(`无法读取 ${file.name}`));
    reader.readAsDataURL(file);
  });
  return {
    kind,
    mime_type: file.type,
    name: file.name,
    data_base64: String(dataUrl).split(",", 2)[1],
  };
}

async function submitCoding() {
  const prompt = ui.prompt.value.trim();
  const files = selectedAttachments();
  if (!prompt && !files.length) throw new Error("请输入需求或添加附件");
  validateAttachments(files);
  const attachments = await Promise.all(files.map(fileToAttachment));
  const task = await api("/api/v3/tasks/coding", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ prompt, attachments, deploy_mode: selectedDeployMode() }),
  });
  beginTask(task);
}

function beginTask(task) {
  stopWatching();
  state.taskId = task.task_id;
  clearEvents();
  renderTask({
    status: task.status,
    stage: task.status,
    progress: 0,
    queue_position: task.queue_position,
  });
  setBusy(true);
  watchTask(task);
}

function watchTask(task) {
  const url = new URL(task.events_url, window.location.origin);
  url.searchParams.set("stream_token", task.stream_token);
  const stream = new EventSource(url);
  state.stream = stream;
  appendRuntimeEvent("stream_connecting", {
    created_at: new Date().toISOString(),
    data: { message: "正在连接实时任务事件" },
  });
  stream.onopen = () => {
    if (state.stream !== stream) return;
    stopPolling();
    appendRuntimeEvent("stream_connected", {
      created_at: new Date().toISOString(),
      data: { message: "实时任务事件已连接" },
    });
  };
  for (const type of EVENT_TYPES) {
    stream.addEventListener(type, (event) => {
      let payload;
      try {
        payload = JSON.parse(event.data);
      } catch {
        appendAgentEvent("agent_warning", {
          created_at: new Date().toISOString(),
          data: { message: `无法解析 ${type} 事件` },
        });
        return;
      }
      appendSseEvent(type, payload);
      if (payload.data) renderEventProgress(payload.data);
      else renderEventProgress(payload);
      if (["task_completed", "task_failed", "task_cancelled"].includes(type)) {
        stream.close();
        state.stream = null;
        stopPolling();
        refreshTask().catch(() => {});
      }
    });
  }
  stream.onerror = () => {
    if (state.stream !== stream) return;
    appendRuntimeEvent("stream_reconnecting", {
      created_at: new Date().toISOString(),
      data: { message: "实时事件连接中断，正在自动重连；任务状态仍会轮询更新" },
    });
    startPolling(task.task_id, 10000);
  };
}

function stopPolling() {
  clearInterval(state.pollTimer);
  state.pollTimer = null;
  state.pollInFlight = false;
}

function startPolling(taskId, intervalMs = 10000) {
  if (state.pollTimer) return;
  const poll = async () => {
    if (state.taskId !== taskId || state.pollInFlight || document.hidden) return;
    state.pollInFlight = true;
    try {
      const task = await api(`/api/v3/tasks/${encodeURIComponent(taskId)}`);
      await loadEventHistory(taskId, false);
      renderTask(task);
      if (TERMINAL.has(task.status)) {
        stopWatching();
        setBusy(false);
        await refreshProject();
        await refreshCapacity();
      }
    } catch (error) {
      setError(ui["coding-error"], error.message);
    } finally {
      state.pollInFlight = false;
    }
  };
  poll();
  state.pollTimer = setInterval(poll, intervalMs);
}

function stopWatching() {
  if (state.stream) state.stream.close();
  state.stream = null;
  stopPolling();
}

async function refreshTask() {
  if (!state.taskId) return;
  const task = await api(`/api/v3/tasks/${encodeURIComponent(state.taskId)}`);
  renderTask(task);
  if (TERMINAL.has(task.status)) {
    stopWatching();
    setBusy(false);
    await refreshProject();
  }
}

function renderTask(task) {
  ui["task-stage"].textContent = stageLabel(task.stage || task.status);
  ui["queue-position"].textContent = task.queue_position ? `排队第 ${task.queue_position} 位` : "";
  ui["task-state"].textContent = statusLabel(task.status);
  ui["task-state"].className = `task-state ${task.status || ""}`;
  if (task.possibly_stalled) {
    setError(ui["coding-error"], "任务可能无响应，可检查事件后取消");
    appendRuntimeEvent("task_stalled", {
      created_at: new Date().toISOString(),
      data: {
        message: "任务仍在运行，但 Agent 较长时间没有新活动",
        agent_silence_seconds: task.agent_silence_seconds,
      },
    });
  }
  if (task.error?.message) setError(ui["coding-error"], task.error.message);
  setBusy(!TERMINAL.has(task.status));
}

function renderEventProgress(data) {
  if (TASK_STATUSES.has(data.status)) {
    ui["task-stage"].textContent = stageLabel(data.stage || data.status);
    ui["task-state"].textContent = statusLabel(data.status);
    ui["task-state"].className = `task-state ${data.status}`;
  }
}

function statusLabel(status) {
  return {
    queued: "排队中",
    running: "运行中",
    completed: "已完成",
    failed: "失败",
    cancelled: "已取消",
  }[status] || (status ? String(status) : "等待任务");
}

function stageLabel(stage) {
  return STAGE_LABELS[stage] || (stage ? String(stage) : "等待任务");
}

function clearEvents() {
  for (const entry of [...state.assistantRows.values(), ...state.reasoningRows.values()]) {
    if (entry.flushFrame !== null) cancelAnimationFrame(entry.flushFrame);
  }
  clearTimeout(state.rawFlushTimer);
  state.rawFlushTimer = null;
  ui["runtime-log"].replaceChildren();
  ui["agent-log"].replaceChildren();
  for (const [log, message] of [
    [ui["runtime-log"], "暂无运行状态"],
    [ui["agent-log"], "暂无 Agent 活动"],
  ]) {
    const empty = document.createElement("li");
    empty.className = "empty-row";
    empty.textContent = message;
    log.append(empty);
  }
  ui["runtime-summary"].textContent = "等待任务";
  ui["raw-summary"].textContent = "等待事件";
  ui["raw-stream"].replaceChildren();
  state.eventSequences.clear();
  state.assistantRows.clear();
  state.reasoningRows.clear();
  state.toolRows.clear();
  state.runtimeRows.clear();
  state.rawChunks = [];
  state.rawVisibleEventCount = 0;
  state.rawPending = [];
  state.rawEventCount = 0;
  state.lastEventSequence = 0;
}

function eventData(event) {
  return event?.data && typeof event.data === "object" ? event.data : (event || {});
}

function eventMessage(type, event) {
  const data = event.data || event;
  if (type === "task_queued") {
    return data.queue_position ? `任务已进入队列，当前第 ${data.queue_position} 位` : "任务已提交，等待执行";
  }
  if (type === "task_started") {
    return data.stage === "authorizing_ai_quota" ? "任务已开始，正在检查 AI Token 额度" : "任务已开始，正在准备独立工作区";
  }
  if (type === "ai_quota_authorized") return `AI Token 额度已放行，本次最多 ${data.granted_tokens ?? "--"} Token`;
  if (type === "ai_quota_settled") {
    return `AI Token 已结算，共 ${data.actual_tokens ?? "--"} Token（输入 ${data.input_tokens ?? "--"}，输出 ${data.output_tokens ?? "--"}，缓存创建 ${data.cache_creation_input_tokens ?? 0}，缓存读取 ${data.cache_read_input_tokens ?? 0}）`;
  }
  if (type === "ai_quota_settlement_pending") return "模型可能已产生用量，额度预占保留等待核对";
  if (type === "ai_quota_released") return "未完成请求的 AI Token 预占已释放";
  if (type === "ai_quota_release_failed") return "AI Token 预占释放状态暂未确认";
  if (type === "cancellation_requested") return "已提交取消请求，等待当前操作停止";
  if (type === "task_completed") return "任务已完成";
  if (type === "task_failed") return data.error?.message || data.message || "任务执行失败";
  if (type === "task_cancelled") return "任务已取消";
  if (type === "deployment_started") return "正在把已验证的代码和资源推送到设备";
  if (type === "deployment_finished") return "代码和资源已提交到设备服务";
  if (data.text) return data.text;
  if (data.message) return data.message;
  if (data.error?.message) return data.error.message;
  if (data.path) return data.path;
  if (data.status) return statusLabel(data.status);
  if (data.stage) return stageLabel(data.stage);
  return `收到 ${EVENT_LABELS[type] || type} 事件`;
}

function formatEventTime(event) {
  const date = new Date(event.created_at || Date.now());
  return Number.isNaN(date.getTime()) ? "--:--:--" : date.toLocaleTimeString("zh-CN", { hour12: false });
}

function prettyDetails(value) {
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function detailsSummary(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return "查看详情";
  if ("input" in value && "output" in value) return "查看输入与结果";
  if ("input" in value) return "查看输入";
  if ("output" in value || "result" in value) return "查看结果";
  if ("skills" in value) return "查看 Skill";
  if ("event_count" in value) return "查看通信统计";
  return "查看详情";
}

function appendDetailValue(container, value) {
  const isFlatObject = value && typeof value === "object" && !Array.isArray(value) &&
    Object.values(value).every((item) => item === null || ["string", "number", "boolean"].includes(typeof item));
  if (isFlatObject) {
    const list = document.createElement("dl");
    list.className = "detail-list";
    for (const [key, item] of Object.entries(value)) {
      const row = document.createElement("div");
      const term = document.createElement("dt");
      term.textContent = key;
      const description = document.createElement("dd");
      description.textContent = item === null ? "--" : String(item);
      row.append(term, description);
      list.append(row);
    }
    container.append(list);
    return;
  }
  const content = document.createElement("pre");
  content.textContent = prettyDetails(value);
  container.append(content);
}

function createDetails(value) {
  if (value === undefined || value === null) return null;
  if (typeof value === "object" && !Array.isArray(value) && !Object.keys(value).length) return null;
  const details = document.createElement("details");
  details.className = "event-details";
  const summary = document.createElement("summary");
  summary.textContent = detailsSummary(value);
  details.append(summary);
  const sectionNames = { input: "输入", output: "结果", result: "结果" };
  const sectionKeys = value && typeof value === "object" && !Array.isArray(value)
    ? Object.keys(sectionNames).filter((key) => key in value)
    : [];
  if (sectionKeys.length) {
    for (const key of sectionKeys) {
      const section = document.createElement("div");
      section.className = "detail-section";
      const title = document.createElement("strong");
      title.className = "detail-section-title";
      title.textContent = sectionNames[key];
      section.append(title);
      appendDetailValue(section, value[key]);
      details.append(section);
    }
  } else {
    appendDetailValue(details, value);
  }
  return details;
}

function createActivityRow(log, type, event, { label, message, tone = "", details } = {}) {
  const empty = log.querySelector(".empty-row");
  if (empty) empty.remove();
  const follow = log.scrollHeight - log.scrollTop - log.clientHeight < 56;
  const row = document.createElement("li");
  row.className = `event-row${tone ? ` ${tone}` : ""}`;
  row.dataset.eventType = type;
  const meta = document.createElement("div");
  meta.className = "event-meta";
  const time = document.createElement("time");
  time.className = "event-time";
  time.textContent = formatEventTime(event);
  const eventLabel = document.createElement("span");
  eventLabel.className = "event-label";
  eventLabel.textContent = label || EVENT_LABELS[type] || "其他事件";
  const eventType = document.createElement("span");
  eventType.className = "event-type";
  eventType.textContent = type;
  meta.append(time, eventLabel, eventType);
  const body = document.createElement("div");
  body.className = "event-body";
  const messageElement = document.createElement("div");
  messageElement.className = "event-message";
  messageElement.textContent = message || eventMessage(type, event);
  body.append(messageElement);
  const detailsElement = createDetails(details);
  if (detailsElement) body.append(detailsElement);
  row.append(meta, body);
  log.append(row);
  if (follow) log.scrollTop = log.scrollHeight;
  return { row, time, label: eventLabel, type: eventType, message: messageElement, body, details: detailsElement };
}

function updateActivityRow(entry, event, { type, label, message, tone, details } = {}) {
  const log = entry.row.parentElement;
  const follow = log && log.scrollHeight - log.scrollTop - log.clientHeight < 56;
  if (type) {
    entry.row.dataset.eventType = type;
    entry.type.textContent = type;
  }
  if (label) entry.label.textContent = label;
  if (message !== undefined) entry.message.textContent = message;
  if (tone !== undefined) entry.row.className = `event-row${tone ? ` ${tone}` : ""}`;
  entry.time.textContent = formatEventTime(event);
  if (details !== undefined) {
    entry.details?.remove();
    entry.details = createDetails(details);
    if (entry.details) entry.body.append(entry.details);
  }
  if (follow && log) log.scrollTop = log.scrollHeight;
}

function formatAge(seconds) {
  if (!Number.isFinite(Number(seconds))) return null;
  const value = Math.max(0, Math.round(Number(seconds)));
  if (value < 60) return `${value} 秒`;
  return `${Math.floor(value / 60)} 分 ${value % 60} 秒`;
}

function heartbeatMessage(data) {
  const parts = ["连接正常", statusLabel(data.status), stageLabel(data.stage)];
  const silence = formatAge(data.agent_silence_seconds);
  if (silence) parts.push(`Agent ${silence}前有活动`);
  if (data.possibly_stalled) parts.push("可能无响应");
  return parts.filter(Boolean).join(" · ");
}

function appendRuntimeEvent(type, event) {
  const data = eventData(event);
  const singleton = ["heartbeat", "stream_connecting", "stream_connected", "stream_reconnecting", "task_stalled"].includes(type);
  const key = type === "heartbeat" ? "heartbeat" : type.startsWith("stream_") ? "event_stream" : type;
  const tone = type.includes("failed") || type === "task_stalled" ? "error" :
    type === "task_completed" || type === "stream_connected" || type === "ai_quota_settled" ? "success" :
      type === "stream_reconnecting" || type === "ai_quota_settlement_pending" ? "warning" : "";
  const message = type === "heartbeat" ? heartbeatMessage(data) : eventMessage(type, event);
  const details = type === "task_failed" ? data.error || data :
    type === "task_completed" ? { result: data.result } :
      type === "task_stalled" ? { agent_silence_seconds: data.agent_silence_seconds } : undefined;
  let entry = singleton ? state.runtimeRows.get(key) : null;
  if (entry) {
    updateActivityRow(entry, event, { type, label: EVENT_LABELS[type], message, tone, details });
  } else {
    entry = createActivityRow(ui["runtime-log"], type, event, { message, tone, details });
    if (singleton) state.runtimeRows.set(key, entry);
  }
  ui["runtime-summary"].textContent = type === "heartbeat" ? `${statusLabel(data.status)} · 心跳正常` : message;
}

function ensureAssistantRow(entry, event) {
  if (!entry.row) {
    const rowEntry = createActivityRow(ui["agent-log"], "assistant_text_delta", event, {
      label: "模型原文",
      message: entry.text,
    });
    Object.assign(entry, rowEntry);
  }
  return entry;
}

function flushAssistantEntry(entry) {
  entry.flushFrame = null;
  if (!entry.text) return;
  ensureAssistantRow(entry, entry.lastEvent);
  updateActivityRow(entry, entry.lastEvent, {
    type: entry.finished ? "assistant_message_finished" : entry.finalized ? "assistant_message" : "assistant_text_delta",
    label: "模型原文",
    message: entry.text,
    tone: entry.finished ? "success" : "",
  });
}

function scheduleAssistantFlush(entry) {
  if (entry.flushFrame !== null || state.historyLoading) return;
  entry.flushFrame = requestAnimationFrame(() => flushAssistantEntry(entry));
}

function appendToolEvent(type, event) {
  const data = eventData(event);
  const serverTool = type.startsWith("server_tool_");
  const key = `${serverTool ? "server" : "client"}:${data.tool_use_id || event.sequence || Date.now()}`;
  const started = state.toolRows.get(key);
  if (type.endsWith("_started")) {
    createActivityRow(ui["agent-log"], type, event, {
      label: data.tool || (serverTool ? "ServerTool" : "Tool"),
      message: prettyDetails(data.input ?? {}),
    });
    state.toolRows.set(key, { tool: data.tool, serverTool });
    return;
  }
  const failed = Boolean(data.is_error);
  const output = data.content ?? data.result;
  const tool = started?.tool || data.tool || (serverTool ? "ServerTool" : "Tool");
  createActivityRow(ui["agent-log"], type, event, {
    label: `${tool} ${failed ? "失败" : "结果"}`,
    message: prettyDetails(output === undefined ? data : output),
    tone: failed ? "error" : "success",
  });
}

function appendAssistantEvent(type, event) {
  const updates = applyAssistantStreamEvent(state.assistantRows, type, event);
  for (const { kind, entry } of updates) {
    if (kind === "delta") {
      scheduleAssistantFlush(entry);
      continue;
    }
    if (entry.flushFrame !== null) cancelAnimationFrame(entry.flushFrame);
    entry.flushFrame = null;
    flushAssistantEntry(entry);
  }
}

function ensureReasoningRow(entry, event) {
  if (!entry.row) {
    const rowEntry = createActivityRow(ui["agent-log"], "agent_reasoning", event, {
      label: "模型思考",
      message: entry.thinking,
    });
    Object.assign(entry, rowEntry);
  }
  return entry;
}

function flushReasoningEntry(entry) {
  entry.flushFrame = null;
  if (!entry.thinking) return;
  ensureReasoningRow(entry, entry.lastEvent);
  updateActivityRow(entry, entry.lastEvent, {
    type: entry.partial ? "agent_partial_capture" : "agent_reasoning",
    label: entry.partial ? "模型思考（未完整）" : "模型思考",
    message: entry.thinking,
    tone: entry.partial ? "warning" : "",
  });
}

function scheduleReasoningFlush(entry) {
  if (entry.flushFrame !== null || state.historyLoading) return;
  entry.flushFrame = requestAnimationFrame(() => flushReasoningEntry(entry));
}

function appendReasoningEvent(type, event) {
  const updates = applyReasoningStreamEvent(state.reasoningRows, type, event);
  for (const { kind, entry } of updates) {
    if (kind === "delta") {
      scheduleReasoningFlush(entry);
      continue;
    }
    if (entry.flushFrame !== null) cancelAnimationFrame(entry.flushFrame);
    entry.flushFrame = null;
    flushReasoningEntry(entry);
  }
}

function rawEventLine(type, event) {
  const sequence = Number(event?.sequence);
  const sequenceText = Number.isInteger(sequence) && sequence > 0 ? `#${sequence}` : "#live";
  let payload;
  try {
    payload = JSON.stringify(eventData(event));
  } catch {
    payload = String(eventData(event));
  }
  return `${sequenceText} ${formatEventTime(event)} ${type} ${payload}`;
}

function appendRawEvent(type, event) {
  state.rawPending.push(rawEventLine(type, event));
  state.rawEventCount += 1;
  const visible = Math.min(state.rawEventCount, RAW_STREAM_VISIBLE_LIMIT);
  const windowText = state.rawEventCount > RAW_STREAM_VISIBLE_LIMIT ? ` · 显示最近 ${visible} 条` : "";
  ui["raw-summary"].textContent = `${state.rawEventCount} 条${windowText} · ${type}`;
  if (state.historyLoading || state.rawFlushTimer) return;
  state.rawFlushTimer = setTimeout(flushRawEvents, 50);
}

function appendRawLines(lines) {
  let offset = 0;
  while (offset < lines.length) {
    let chunk = state.rawChunks[state.rawChunks.length - 1];
    if (!chunk || chunk.lineCount >= RAW_STREAM_CHUNK_LINES) {
      const node = document.createTextNode("");
      ui["raw-stream"].append(node);
      chunk = { node, lineCount: 0 };
      state.rawChunks.push(chunk);
    }
    const count = Math.min(RAW_STREAM_CHUNK_LINES - chunk.lineCount, lines.length - offset);
    chunk.node.appendData(`${lines.slice(offset, offset + count).join("\n")}\n`);
    chunk.lineCount += count;
    state.rawVisibleEventCount += count;
    offset += count;
  }
}

function pruneRawWindow() {
  while (state.rawVisibleEventCount > RAW_STREAM_VISIBLE_LIMIT && state.rawChunks.length > 1) {
    const chunk = state.rawChunks.shift();
    chunk.node.remove();
    state.rawVisibleEventCount -= chunk.lineCount;
  }
}

function flushRawEvents() {
  clearTimeout(state.rawFlushTimer);
  state.rawFlushTimer = null;
  if (!state.rawPending.length) return;
  const follow = ui["raw-stream"].scrollHeight - ui["raw-stream"].scrollTop - ui["raw-stream"].clientHeight < 40;
  let lines = state.rawPending;
  state.rawPending = [];
  if (lines.length > RAW_STREAM_VISIBLE_LIMIT) {
    lines = lines.slice(-RAW_STREAM_VISIBLE_LIMIT);
    ui["raw-stream"].replaceChildren();
    state.rawChunks = [];
    state.rawVisibleEventCount = 0;
  }
  appendRawLines(lines);
  pruneRawWindow();
  if (follow) ui["raw-stream"].scrollTop = ui["raw-stream"].scrollHeight;
}

function appendAgentEvent(type, event) {
  if (["assistant_message_started", "assistant_text_delta", "assistant_message", "assistant_message_finished"].includes(type)) {
    appendAssistantEvent(type, event);
    return;
  }
  if (type === "agent_reasoning" || (type === "agent_partial_capture" && eventData(event).block_type === "thinking")) {
    appendReasoningEvent(type, event);
    return;
  }
  if (["tool_started", "tool_finished", "server_tool_started", "server_tool_finished"].includes(type)) {
    appendToolEvent(type, event);
    return;
  }
  if (RAW_ONLY_AGENT_EVENTS.has(type)) return;
  const data = eventData(event);
  const tone = type === "agent_warning" || type === "agent_rate_limit" ? "warning" :
    type === "agent_result_error" ? "error" :
      ["agent_result", "deployment_finished"].includes(type) ? "success" : "";
  createActivityRow(ui["agent-log"], type, event, {
    message: prettyDetails(data),
    tone,
  });
}

function appendSseEvent(type, event) {
  const sequence = Number(event?.sequence);
  if (Number.isInteger(sequence) && sequence > 0) {
    if (state.eventSequences.has(sequence)) return;
    state.eventSequences.add(sequence);
    state.lastEventSequence = Math.max(state.lastEventSequence, sequence);
  }
  appendRawEvent(type, event);
  if (RUNTIME_EVENT_TYPES.has(type)) appendRuntimeEvent(type, event);
  else appendAgentEvent(type, event);
}

async function loadEventHistory(taskId, reset = true) {
  if (state.historyPromise) await state.historyPromise;
  const request = (async () => {
    state.historyLoading = true;
    try {
      if (reset) clearEvents();
      let after = reset ? 0 : state.lastEventSequence;
      while (state.taskId === taskId) {
        const result = await api(`/api/v3/tasks/${encodeURIComponent(taskId)}/events/history?after=${after}&limit=1000`);
        const events = result.events || [];
        for (const event of events) appendSseEvent(event.type, event);
        if (!events.length) break;
        after = Math.max(after, ...events.map((event) => Number(event.sequence) || 0));
        if (events.length < 1000) break;
      }
    } finally {
      state.historyLoading = false;
      flushRawEvents();
      for (const entry of state.assistantRows.values()) flushAssistantEntry(entry);
      for (const entry of state.reasoningRows.values()) flushReasoningEntry(entry);
    }
  })();
  state.historyPromise = request;
  try {
    await request;
  } finally {
    if (state.historyPromise === request) state.historyPromise = null;
  }
}

async function cancelTask() {
  if (!state.taskId) return;
  const task = await api(`/api/v3/tasks/${encodeURIComponent(state.taskId)}/cancel`, { method: "POST" });
  renderTask(task);
  showToast("已请求取消任务");
}

async function directRun() {
  await api("/api/v3/deployments/plan", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ code_path: "main.py", include_resources: true }),
  });
  const task = await api("/api/v3/tasks/direct-run", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ code_path: "main.py", include_resources: true }),
  });
  beginTask(task);
}

async function uploadProjectFile(file) {
  const form = new FormData();
  form.append("file", file, file.name);
  await api("/api/v3/files", { method: "POST", body: form });
  await refreshProject();
  showToast(`${file.name} 已上传`);
}

async function resetConversation() {
  const result = await api("/api/v3/conversation/reset", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ keep_files: true }),
  });
  ui["conversation-id"].textContent = compactId(result.conversation_id);
  ui["claude-session"].textContent = "--";
  showToast("已创建新会话，项目文件保留");
}

ui["device-form"].addEventListener("submit", async (event) => {
  event.preventDefault();
  setError(ui["connect-error"], "");
  ui["connect-button"].disabled = true;
  try {
    await connectDevice(
      ui["device-id"].value.trim(),
      ui["client-id"].value.trim(),
      ui["mac-address"].value.trim(),
      ui.product.value.trim()
    );
  } catch (error) {
    setError(ui["connect-error"], error.message);
  } finally {
    ui["connect-button"].disabled = false;
  }
});

ui["coding-form"].addEventListener("submit", async (event) => {
  event.preventDefault();
  setError(ui["coding-error"], "");
  ui["submit-button"].disabled = true;
  try {
    await submitCoding();
  } catch (error) {
    setError(ui["coding-error"], error.message);
    setBusy(false);
  }
});

for (const input of document.querySelectorAll('input[name="deployMode"]')) {
  input.addEventListener("change", updateSubmitLabel);
}

ui["image-input"].addEventListener("change", updateAttachmentSummary);
ui["audio-input"].addEventListener("change", updateAttachmentSummary);
ui["cancel-button"].addEventListener("click", () => cancelTask().catch((error) => showToast(error.message, true)));
ui["rerun-button"].addEventListener("click", () => directRun().catch((error) => showToast(error.message, true)));
ui["refresh-task"].addEventListener("click", () => refreshTask().catch((error) => showToast(error.message, true)));
ui["refresh-project"].addEventListener("click", () => refreshProject().catch((error) => showToast(error.message, true)));
ui["reset-button"].addEventListener("click", () => resetConversation().catch((error) => showToast(error.message, true)));
ui["project-file-input"].addEventListener("change", async () => {
  const [file] = ui["project-file-input"].files;
  if (!file) return;
  try {
    await uploadProjectFile(file);
  } catch (error) {
    showToast(error.message, true);
  } finally {
    ui["project-file-input"].value = "";
  }
});

window.addEventListener("beforeunload", stopWatching);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) {
    refreshCapacity().catch(() => {});
    if (state.taskId && !state.stream) startPolling(state.taskId, 10000);
  }
});

async function initialize() {
  setConnected(false);
  setBusy(false);
  updateSubmitLabel();
  await refreshService();
  await restoreSession();
  state.serviceTimer = setInterval(refreshCapacity, 20000);
}

initialize().catch((error) => showToast(error.message, true));
