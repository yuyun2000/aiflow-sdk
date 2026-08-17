(() => {
  "use strict";

  const TOKEN_KEY = "aiflow.analytics.apiToken";
  const REFRESH_MS = 30_000;
  const state = {
    token: sessionStorage.getItem(TOKEN_KEY) || "",
    startDate: "",
    endDate: "",
    bucket: "day",
    page: 1,
    hasNext: false,
    loading: false,
    loadId: 0,
    refreshTimer: null,
    toastTimer: null,
  };

  const $ = (id) => document.getElementById(id);
  const text = (value, fallback = "--") => value === null || value === undefined || value === "" ? fallback : String(value);
  const number = (value, digits = 0) => {
    if (value === null || value === undefined || Number.isNaN(Number(value))) return "--";
    return Number(value).toLocaleString("zh-CN", { maximumFractionDigits: digits });
  };
  const tokenNumber = (value, digits = 1) => {
    if (value === null || value === undefined || Number.isNaN(Number(value))) return "--";
    const numeric = Number(value);
    if (Math.abs(numeric) < 1_000_000) return number(numeric);
    return `${(numeric / 1_000_000).toLocaleString("zh-CN", { maximumFractionDigits: digits })}M`;
  };
  const percent = (value, digits = 1) => value === null || value === undefined ? "--" : `${(Number(value) * 100).toFixed(digits)}%`;
  const usd = (value) => value === null || value === undefined ? "--" : `$${Number(value).toFixed(4)}`;
  const usdPerMillion = (value) => value === null || value === undefined ? "未配置" : `$${Number(value).toFixed(2)} / 1M`;
  const millis = (value) => {
    if (value === null || value === undefined || Number.isNaN(Number(value))) return "--";
    const ms = Number(value);
    if (ms < 1000) return `${Math.round(ms)} ms`;
    if (ms < 60_000) return `${(ms / 1000).toFixed(1)} s`;
    return `${Math.floor(ms / 60_000)}m ${Math.round((ms % 60_000) / 1000)}s`;
  };
  const dateText = (value) => {
    if (!value) return "--";
    const date = new Date(typeof value === "number" ? value : value);
    if (Number.isNaN(date.getTime())) return text(value);
    return date.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
  };
  const dateInputText = (date) => {
    const year = date.getFullYear();
    const month = String(date.getMonth() + 1).padStart(2, "0");
    const day = String(date.getDate()).padStart(2, "0");
    return `${year}-${month}-${day}`;
  };
  const escapeDate = (value) => encodeURIComponent(value);

  class AuthError extends Error {}

  function setText(id, value, fallback = "--") {
    const element = $(id);
    if (element) element.textContent = text(value, fallback);
  }

  function setServiceState(kind, label) {
    const badge = $("service-state");
    badge.className = `status-badge ${kind}`;
    badge.querySelector("span:last-child").textContent = label;
  }

  function showToast(message) {
    const toast = $("toast");
    toast.textContent = message;
    toast.classList.add("show");
    clearTimeout(state.toastTimer);
    state.toastTimer = setTimeout(() => toast.classList.remove("show"), 4500);
  }

  function showLogin(message = "") {
    $("login-layer").hidden = false;
    $("login-error").textContent = message;
    setServiceState("error", message ? "需要重新验证" : "需要验证");
    setTimeout(() => $("token-input").focus(), 0);
  }

  function hideLogin() {
    $("login-layer").hidden = true;
    $("login-error").textContent = "";
  }

  async function api(path, options = {}) {
    const headers = new Headers(options.headers || {});
    if (state.token) headers.set("Authorization", `Bearer ${state.token}`);
    if (options.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
    const response = await fetch(path, { ...options, headers });
    if (response.status === 401) throw new AuthError("API Token 无效或已过期");
    let body = null;
    try { body = await response.json(); } catch (_) { /* empty response */ }
    if (!response.ok) throw new Error(body && body.detail ? body.detail : `请求失败（${response.status}）`);
    return body;
  }

  function queryString() {
    return `start_date=${escapeDate(state.startDate)}&end_date=${escapeDate(state.endDate)}`;
  }

  function setDateRange(days) {
    const end = new Date();
    const start = new Date(end);
    start.setDate(end.getDate() - Math.max(0, days - 1));
    state.startDate = dateInputText(start);
    state.endDate = dateInputText(end);
    $("start-date").value = state.startDate;
    $("end-date").value = state.endDate;
    document.querySelectorAll("#range-presets button").forEach((button) => {
      button.classList.toggle("active", Number(button.dataset.days) === days);
    });
  }

  function setCustomDateRange() {
    state.startDate = $("start-date").value;
    state.endDate = $("end-date").value;
    document.querySelectorAll("#range-presets button").forEach((button) => button.classList.remove("active"));
  }

  function statusLabel(value) {
    return { completed: "已完成", failed: "失败", cancelled: "已取消", running: "运行中" }[value] || text(value, "未知");
  }

  function eventLabel(value) {
    return {
      user_input: "用户输入", task_started: "任务开始", agent_connected: "Agent 连接",
      agent_reasoning: "模型 thinking", assistant_message: "模型回复", assistant_text_delta: "回复增量",
      assistant_message_finished: "回复完成", tool_started: "工具调用", tool_finished: "工具结果",
      task_completed: "任务完成", task_failed: "任务失败", task_cancelled: "任务取消",
      agent_result: "Agent 结果", agent_result_error: "Agent 错误",
    }[value] || text(value, "事件");
  }

  function formatModel(model) {
    if (!model) return "未返回模型";
    const value = String(model);
    return value.length > 30 ? `${value.slice(0, 28)}…` : value;
  }

  function renderDashboard(data) {
    window.__aiflowTrendPoints = data.trends?.points || [];
    const overview = data.overview || {};
    const volume = overview.volume || {};
    const usage = overview.usage || {};
    const cost = overview.cost || {};
    const latency = overview.latency_ms || {};
    const content = overview.content || {};
    const tools = overview.tools || {};
    const deployment = overview.deployment || {};
    setText("metric-turns", number(volume.turns));
    setText("metric-turns-note", `${number(volume.conversations)} 个对话 / ${number(volume.projects)} 个项目`);
    setText("metric-completion", percent(volume.completion_rate));
    setText("metric-completion-note", `${number(volume.completed)} 完成 / ${number(volume.failed)} 失败`);
    setText("metric-tokens", tokenNumber(usage.total_tokens));
    setText("metric-tokens-note", `输入 ${tokenNumber(usage.input_tokens)} / 输出 ${tokenNumber(usage.output_tokens)} / 缓存读取 ${tokenNumber(usage.cache_read_input_tokens)}`);
    setText("metric-cost", usd(cost.actual_usd));
    setText("metric-cost-note", `SDK Claude计价参考 ${usd(cost.sdk_reported_usd)}`);
    setText("metric-latency", millis(latency.service_avg));
    setText("metric-latency-note", `P95 ${millis(latency.service_p95)}`);
    setText("metric-tool-errors", percent(tools.error_rate));
    setText("metric-tool-errors-note", `${number(tools.calls)} 次调用 / ${number(tools.errors)} 次错误`);
    setText("metric-thinking", number(content.thinking_chars));
    setText("metric-thinking-note", `回复字符 ${number(content.assistant_chars)}`);
    setText("metric-quality", qualityPercent(data.data_quality));
    setText("metric-quality-note", `${number((data.data_quality || {}).records?.physical_records)} 个物理记录`);
    setText("api-p95", millis(latency.api_p95));
    setText("queue-avg", millis(latency.queue_avg));
    setText("deploy-rate", percent(deployment.success_rate));
    setText("range-label", `${state.startDate} 至 ${state.endDate} · ${number(volume.turns)} 个任务`);
    renderPricing(overview);
    renderHealth(volume, data.breakdowns || {});
    renderBars("models-list", data.breakdowns?.models || [], (item) => ({
      name: formatModel(item.canonical_model || item.model),
      detail: `${number(item.turns)} 任务 · 实际 ${usd(item.configured_actual_usd)}`,
      value: number(item.input_tokens + item.output_tokens),
      max: item.turns,
    }));
    renderBars("tools-list", data.breakdowns?.tools || [], (item) => ({
      name: item.tool_name || item.tool_type,
      detail: `${number(item.completed)} 完成 · ${number(item.errors)} 错误`,
      value: item.calls,
      max: item.calls,
    }));
    renderBars("statuses-list", data.breakdowns?.statuses || [], (item) => ({
      name: statusLabel(item.value),
      detail: `${tokenNumber(item.tokens)} Token · 实际 ${usd(item.configured_actual_usd)}`,
      value: item.turns,
      max: item.turns,
    }));
    renderQuality(data.data_quality || {});
    drawTrend(data.trends?.points || []);
  }

  function renderPricing(overview) {
    const cost = overview.cost || {};
    const models = cost.model_estimates || [];
    const list = $("pricing-list");
    list.replaceChildren();
    if (!models.length) {
      const empty = document.createElement("div");
      empty.className = "empty-row";
      empty.textContent = "当前范围没有可按模型计价的日志";
      list.append(empty);
    }
    models.forEach((model) => {
      const heading = document.createElement("div");
      heading.className = "pricing-model-heading";
      const modelName = document.createElement("strong");
      modelName.textContent = formatModel(model.model);
      modelName.title = text(model.model);
      const modelStatus = document.createElement("span");
      modelStatus.textContent = model.configured ? "已配置" : "未配置单价";
      modelStatus.className = model.configured ? "pricing-configured" : "pricing-unconfigured";
      heading.append(modelName, modelStatus);
      list.append(heading);
      const tokens = model.tokens || {};
      const units = model.unit_prices_usd_per_million || {};
      const breakdown = model.estimated_breakdown_usd || {};
      [
        ["输入", tokens.input_tokens, units.input, breakdown.input_usd],
        ["输出", tokens.output_tokens, units.output, breakdown.output_usd],
        ["缓存读取", tokens.cache_read_input_tokens, units.cache_read, breakdown.cache_read_usd],
        ["缓存写入", tokens.cache_creation_input_tokens, units.cache_creation, breakdown.cache_creation_usd],
      ].forEach(([label, tokenCount, unit, estimated]) => {
        const row = document.createElement("div");
        row.className = "pricing-row";
        const name = document.createElement("span");
        name.textContent = label;
        const count = document.createElement("span");
        count.className = "pricing-count";
        count.textContent = `${tokenNumber(tokenCount)} Token`;
        const price = document.createElement("span");
        price.className = "pricing-price";
        price.textContent = `${usdPerMillion(unit)} · ${usd(estimated)}`;
        row.append(name, count, price);
        list.append(row);
      });
    });
    setText("actual-cost", usd(cost.actual_usd));
    setText("actual-cost-note", `按 model_pricing.json 计算 · SDK Claude计价参考 ${usd(cost.sdk_reported_usd)}`);
    const badge = $("pricing-badge");
    const configured = models.filter((model) => model.configured).length;
    const allConfigured = models.length > 0 && configured === models.length;
    badge.className = `small-badge ${allConfigured ? "good" : "warn"}`;
    badge.textContent = allConfigured ? "按模型估算" : configured ? "部分配置" : "未配置单价";
  }

  function qualityPercent(quality) {
    const records = quality.records || {};
    const turns = quality.turns || {};
    const physical = Number(records.physical_records || 0);
    const incomplete = Number(records.incomplete_events || 0);
    const missing = Number(turns.missing_terminal || 0) + Number(turns.partial_turns || 0);
    if (!physical && !Number(turns.turns || 0)) return "--";
    const denominator = Math.max(physical, Number(turns.turns || 0));
    return `${Math.max(0, 100 - ((incomplete + missing) / denominator) * 100).toFixed(1)}%`;
  }

  function renderHealth(volume, breakdowns) {
    const completed = Number(volume.completed || 0);
    const failed = Number(volume.failed || 0);
    const cancelled = Number(volume.cancelled || 0);
    const incomplete = Number(volume.incomplete || 0);
    const total = completed + failed + cancelled + incomplete;
    const value = total ? completed / total : 0;
    const degree = `${Math.round(value * 360)}deg`;
    $("health-ring").style.background = `conic-gradient(var(--teal) 0deg ${degree}, #e4eaed ${degree} 360deg)`;
    setText("health-ring-value", total ? percent(value) : "--");
    const legend = $("health-legend");
    legend.replaceChildren();
    [["已完成", completed, "var(--teal)"], ["失败", failed, "var(--coral)"], ["未完成", incomplete, "var(--amber)"], ["已取消", cancelled, "var(--line-strong)"]].forEach(([label, count, color]) => {
      const row = document.createElement("div");
      row.className = "health-legend-row";
      const swatch = document.createElement("i");
      swatch.style.background = color;
      const name = document.createElement("span");
      name.textContent = label;
      const valueNode = document.createElement("strong");
      valueNode.textContent = number(count);
      row.append(swatch, name, valueNode);
      legend.append(row);
    });
    void breakdowns;
  }

  function renderBars(id, items, mapper) {
    const list = $(id);
    list.replaceChildren();
    if (!items.length) {
      const empty = document.createElement("div");
      empty.className = "empty-row";
      empty.textContent = "当前范围暂无数据";
      list.append(empty);
      return;
    }
    const mapped = items.slice(0, 8).map(mapper);
    const max = Math.max(...mapped.map((item) => Number(item.max || 0)), 1);
    mapped.forEach((item) => {
      const row = document.createElement("div");
      row.className = "bar-row";
      const name = document.createElement("span");
      name.className = "bar-name";
      name.title = item.name;
      name.textContent = text(item.name, "未知");
      const track = document.createElement("span");
      track.className = "bar-track";
      const fill = document.createElement("span");
      fill.className = "bar-fill";
      fill.style.width = `${Math.max(2, Number(item.max || 0) / max * 100)}%`;
      track.append(fill);
      const value = document.createElement("span");
      value.className = "bar-value";
      const strong = document.createElement("strong");
      strong.textContent = number(item.value);
      const detail = document.createElement("small");
      detail.textContent = item.detail;
      detail.style.display = "block";
      detail.style.marginTop = "2px";
      detail.style.color = "var(--muted)";
      detail.style.fontSize = "9px";
      value.append(strong, detail);
      row.append(name, track, value);
      list.append(row);
    });
  }

  function renderQuality(quality) {
    const records = quality.records || {};
    const turns = quality.turns || {};
    const tools = quality.tools || {};
    const errors = (quality.ingest_errors || []).reduce((sum, item) => sum + Number(item.occurrences || 0), 0);
    const rows = [
      ["逻辑事件", records.logical_events], ["缺失分块", records.incomplete_events],
      ["缺失终态", turns.missing_terminal], ["Partial 任务", turns.partial_turns],
      ["工具缺少结果", tools.missing_results], ["孤立工具结果", tools.orphan_results],
      ["导入错误", errors], ["重复拉取", records.duplicate_fetches],
    ];
    const list = $("quality-list");
    list.replaceChildren();
    rows.forEach(([label, value]) => {
      const row = document.createElement("div");
      row.className = "quality-row";
      const name = document.createElement("span");
      name.textContent = label;
      const count = document.createElement("strong");
      count.textContent = number(value, 0);
      row.append(name, count);
      list.append(row);
    });
    const bad = rows.slice(1, 7).some(([, value]) => Number(value || 0) > 0);
    const badge = $("quality-badge");
    badge.className = `small-badge ${bad ? "warn" : "good"}`;
    badge.textContent = bad ? "需要关注" : "链路正常";
  }

  function drawTrend(points) {
    const canvas = $("trend-chart");
    const empty = $("trend-empty");
    if (!points.length) { empty.hidden = false; canvas.getContext("2d").clearRect(0, 0, canvas.width, canvas.height); return; }
    empty.hidden = true;
    const width = Math.max(canvas.clientWidth, 320);
    const height = Math.max(canvas.clientHeight, 220);
    const ratio = window.devicePixelRatio || 1;
    canvas.width = width * ratio;
    canvas.height = height * ratio;
    const ctx = canvas.getContext("2d");
    ctx.scale(ratio, ratio);
    const padding = { top: 18, right: 20, bottom: 32, left: 40 };
    const chartWidth = width - padding.left - padding.right;
    const chartHeight = height - padding.top - padding.bottom;
    const values = points.flatMap((point) => [Number(point.turns || 0), Number(point.tokens || 0) / 1000, Number(point.tool_calls || 0)]);
    const maxValue = Math.max(...values, 1);
    ctx.clearRect(0, 0, width, height);
    ctx.font = "10px -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif";
    ctx.strokeStyle = "#e6ecef";
    ctx.fillStyle = "#7a8790";
    ctx.lineWidth = 1;
    for (let index = 0; index <= 4; index += 1) {
      const y = padding.top + chartHeight * (index / 4);
      ctx.beginPath(); ctx.moveTo(padding.left, y); ctx.lineTo(width - padding.right, y); ctx.stroke();
      ctx.fillText(number(maxValue * (1 - index / 4), 1), 6, y + 3);
    }
    const x = (index) => padding.left + (points.length === 1 ? chartWidth / 2 : chartWidth * index / (points.length - 1));
    points.forEach((point, index) => {
      if (points.length <= 8 || index % Math.ceil(points.length / 8) === 0 || index === points.length - 1) {
        const label = dateText(point.bucket).split(" ")[0];
        ctx.fillStyle = "#7a8790"; ctx.fillText(label, x(index) - 16, height - 10);
      }
    });
    [["turns", "#087f76"], ["tokens", "#3975a5"], ["tool_calls", "#c75d4e"]].forEach(([field, color]) => {
      ctx.beginPath();
      points.forEach((point, index) => {
        const raw = field === "tokens" ? Number(point.tokens || 0) / 1000 : Number(point[field] || 0);
        const y = padding.top + chartHeight * (1 - raw / maxValue);
        if (index === 0) ctx.moveTo(x(index), y); else ctx.lineTo(x(index), y);
      });
      ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.stroke();
      points.forEach((point, index) => {
        const raw = field === "tokens" ? Number(point.tokens || 0) / 1000 : Number(point[field] || 0);
        const y = padding.top + chartHeight * (1 - raw / maxValue);
        ctx.beginPath(); ctx.fillStyle = color; ctx.arc(x(index), y, 2.5, 0, Math.PI * 2); ctx.fill();
      });
    });
  }

  function renderTurns(data) {
    const table = $("turns-table");
    table.replaceChildren();
    const items = data.items || [];
    const pagination = data.pagination || {};
    state.hasNext = Boolean(pagination.has_next);
    setText("turns-count", `${number(pagination.total)} 个任务`);
    setText("turns-page-label", `第 ${number(pagination.page || state.page)} 页 · 每页 ${number(pagination.page_size || 20)} 条`);
    $("turns-prev").disabled = state.page <= 1;
    $("turns-next").disabled = !state.hasNext;
    if (!items.length) {
      const row = document.createElement("tr");
      const cell = document.createElement("td");
      cell.colSpan = 8; cell.className = "empty-row"; cell.textContent = "当前范围暂无任务"; row.append(cell); table.append(row); return;
    }
    items.forEach((item) => {
      const row = document.createElement("tr");
      row.addEventListener("click", () => openDetail(item.turn_id));
      const identity = document.createElement("td");
      const main = document.createElement("div"); main.className = "turn-main";
      const id = document.createElement("span"); id.className = "turn-id"; id.title = item.turn_id; id.textContent = text(item.turn_id);
      const conversation = document.createElement("span"); conversation.className = "turn-conversation"; conversation.textContent = `对话 ${text(item.conversation_id)} · 第 ${number(item.turn_index, 0)} 轮`;
      main.append(id, conversation); identity.append(main);
      const status = document.createElement("td"); const pill = document.createElement("span"); pill.className = `state-pill ${text(item.status, "").toLowerCase()}`; pill.textContent = statusLabel(item.status); status.append(pill);
      const model = document.createElement("td"); model.textContent = formatModel(item.primary_model);
      const tokens = document.createElement("td"); tokens.className = "numeric"; tokens.textContent = tokenNumber(item.total_tokens);
      const cost = document.createElement("td"); cost.className = "numeric"; cost.textContent = usd(item.configured_actual_usd);
      const duration = document.createElement("td"); duration.className = "numeric"; duration.textContent = millis(item.service_duration_ms || item.duration_ms);
      const toolCount = document.createElement("td"); toolCount.textContent = `${number(item.tool_call_count)} 次`;
      const updated = document.createElement("td"); updated.textContent = dateText(item.updated_at || item.last_event_ms);
      row.append(identity, status, model, tokens, cost, duration, toolCount, updated); table.append(row);
    });
  }

  function safeJson(value) {
    if (value === undefined || value === null) return "";
    if (typeof value === "string") return value;
    try { return JSON.stringify(value, null, 2); } catch (_) { return String(value); }
  }

  async function openDetail(turnId) {
    const drawer = $("detail-drawer");
    const backdrop = $("drawer-backdrop");
    drawer.classList.add("open"); drawer.setAttribute("aria-hidden", "false"); backdrop.hidden = false;
    setText("detail-title", turnId);
    $("detail-body").replaceChildren(Object.assign(document.createElement("div"), { className: "detail-loading", textContent: "正在加载完整事件时间线…" }));
    try { renderDetail(await api(`/api/v1/turns/${encodeURIComponent(turnId)}`)); }
    catch (error) { if (error instanceof AuthError) showLogin(error.message); else showToast(error.message); }
  }

  function renderDetail(data) {
    const turn = data.turn || {};
    const body = $("detail-body"); body.replaceChildren();
    setText("detail-title", turn.turn_id);
    const summary = document.createElement("div"); summary.className = "detail-summary";
    [["状态", statusLabel(turn.status)], ["模型", formatModel(turn.primary_model)], ["Token", tokenNumber(turn.total_tokens)], ["实际费用", usd(turn.configured_actual_usd)], ["SDK参考", usd(turn.sdk_reported_usd)], ["服务耗时", millis(turn.service_duration_ms)], ["工具调用", number(turn.tool_call_count)]].forEach(([label, value]) => {
      const stat = document.createElement("div"); stat.className = "detail-stat"; const name = document.createElement("span"); name.textContent = label; const val = document.createElement("strong"); val.textContent = value; stat.append(name, val); summary.append(stat);
    });
    body.append(summary);
    const timelineSection = document.createElement("section"); timelineSection.className = "detail-section"; const timelineTitle = document.createElement("h3"); timelineTitle.textContent = `事件时间线（${number((data.events || []).length)}）`; timelineSection.append(timelineTitle);
    const timeline = document.createElement("div"); timeline.className = "timeline";
    (data.events || []).forEach((event) => {
      const item = document.createElement("article"); item.className = "timeline-item";
      const top = document.createElement("div"); top.className = "timeline-top"; const type = document.createElement("strong"); type.className = "timeline-type"; type.textContent = eventLabel(event.event_type); const time = document.createElement("span"); time.className = "timeline-time"; time.textContent = `${text(event.event_type)} · ${dateText(event.event_time_ms)}`; top.append(type, time);
      const content = document.createElement("pre"); content.className = "timeline-content"; content.textContent = safeJson(event.payload); item.append(top, content); timeline.append(item);
    });
    if (!(data.events || []).length) { const empty = document.createElement("div"); empty.className = "empty-row"; empty.textContent = "没有可展示的逻辑事件"; timeline.append(empty); }
    timelineSection.append(timeline); body.append(timelineSection);
    appendDetailRows(body, "工具明细", data.tools || [], "tool");
    appendDetailRows(body, "模型用量", data.models || [], "model");
  }

  function appendDetailRows(body, title, rows, kind) {
    if (!rows.length) return;
    const section = document.createElement("section"); section.className = "detail-section"; const heading = document.createElement("h3"); heading.textContent = title; section.append(heading);
    rows.forEach((row) => {
      const item = document.createElement("div"); item.className = `${kind}-detail-row`;
      const first = document.createElement("strong"); first.textContent = kind === "tool" ? text(row.tool_name || row.tool_type) : formatModel(row.model);
      const second = document.createElement("span"); second.textContent = kind === "tool" ? `${row.is_error ? "错误" : "完成"} · ${millis(row.duration_ms)}` : `输入 ${tokenNumber(row.input_tokens)} · 输出 ${tokenNumber(row.output_tokens)} · 缓存 ${tokenNumber(row.cache_read_input_tokens)}`;
      const third = document.createElement("span"); third.textContent = kind === "tool" ? text(row.tool_use_id) : usd(row.configured_actual_usd);
      item.append(first, second, third); section.append(item);
    });
    body.append(section);
  }

  function closeDetail() {
    $("detail-drawer").classList.remove("open"); $("detail-drawer").setAttribute("aria-hidden", "true"); $("drawer-backdrop").hidden = true;
  }

  function renderSyncStatus(status) {
    const sync = status.sync || {};
    const latest = sync.latest_run || {};
    if (sync.active) { setText("sync-label", `同步状态：进行中（${text(sync.active.current_date || sync.active.start)}）`); }
    else if (sync.historical_sync_needed) { setText("sync-label", "同步状态：等待补齐历史日志"); }
    else if (latest.status === "failed") { setText("sync-label", `同步状态：上次失败（${text(latest.error, "未知原因")}）`); }
    else if (latest.status === "completed") { setText("sync-label", "同步状态：已完成"); }
    else { setText("sync-label", sync.tls_configured ? "同步状态：等待计划任务" : "同步状态：TLS 未配置"); }
    setText("sync-time", `最后同步：${dateText(latest.finished_at || latest.started_at)}`);
  }

  async function loadData() {
    if (state.loading) return;
    state.loading = true; const loadId = ++state.loadId;
    $("refresh-button").classList.add("loading"); setServiceState("pending", "读取中");
    try {
      const query = queryString();
      const [dashboard, status, turns] = await Promise.all([
        api(`/api/v1/dashboard?${query}&bucket=${encodeURIComponent(state.bucket)}&limit=12`),
        api("/api/v1/status"),
        api(`/api/v1/turns?${query}&page=${state.page}&page_size=20`),
      ]);
      if (loadId !== state.loadId) return;
      renderDashboard(dashboard); renderSyncStatus(status); renderTurns(turns);
      hideLogin(); setServiceState("ready", "服务正常"); setText("last-updated", `更新于 ${dateText(Date.now())}`);
    } catch (error) {
      if (error instanceof AuthError) { showLogin(error.message); }
      else { setServiceState("error", "读取失败"); showToast(error.message || "无法读取分析服务"); }
    } finally {
      state.loading = false; $("refresh-button").classList.remove("loading");
    }
  }

  async function triggerSync() {
    const button = $("sync-button"); button.disabled = true;
    try {
      await api("/api/v1/sync", { method: "POST", body: JSON.stringify({ start_date: state.startDate, end_date: state.endDate, force: false }) });
      showToast("同步任务已提交，后台会继续拉取日志");
      setTimeout(loadData, 900);
    } catch (error) { if (error instanceof AuthError) showLogin(error.message); else showToast(error.message || "同步提交失败"); }
    finally { button.disabled = false; }
  }

  function bindEvents() {
    setDateRange(7);
    document.querySelectorAll("#range-presets button").forEach((button) => button.addEventListener("click", () => { setDateRange(Number(button.dataset.days)); state.page = 1; loadData(); }));
    $("start-date").addEventListener("change", () => { setCustomDateRange(); state.page = 1; loadData(); });
    $("end-date").addEventListener("change", () => { setCustomDateRange(); state.page = 1; loadData(); });
    $("bucket-select").addEventListener("change", (event) => { state.bucket = event.target.value; loadData(); });
    $("refresh-button").addEventListener("click", loadData);
    $("sync-button").addEventListener("click", triggerSync);
    $("turns-prev").addEventListener("click", () => { if (state.page > 1) { state.page -= 1; loadData(); } });
    $("turns-next").addEventListener("click", () => { if (state.hasNext) { state.page += 1; loadData(); } });
    $("drawer-close").addEventListener("click", closeDetail); $("drawer-backdrop").addEventListener("click", closeDetail);
    document.addEventListener("keydown", (event) => { if (event.key === "Escape") closeDetail(); });
    $("logout-button").addEventListener("click", () => { sessionStorage.removeItem(TOKEN_KEY); state.token = ""; showLogin("已清除当前会话令牌"); });
    $("login-form").addEventListener("submit", async (event) => {
      event.preventDefault(); const input = $("token-input"); const value = input.value.trim(); if (!value) return;
      state.token = value; sessionStorage.setItem(TOKEN_KEY, value); $("login-error").textContent = "正在验证…"; await loadData();
      if ($("service-state").classList.contains("ready")) input.value = "";
    });
    window.addEventListener("resize", () => { const points = window.__aiflowTrendPoints || []; if (points.length) drawTrend(points); });
  }

  bindEvents();
  state.refreshTimer = setInterval(loadData, REFRESH_MS);
  loadData();
})();
