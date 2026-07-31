# 匿名网页安全与 Agent 事件

## 1. 能做到什么

当前客户端是部署在服务器上的普通网页，用户不登录即可对话。这个前提下，浏览器里不存在不可复制的秘密：JavaScript、请求格式、Header 和 Cookie 都能被观察或由脚本模仿。因此不能通过“隐藏加密算法”严格证明请求来自真人或官方网页。

可执行的安全目标是：

1. 浏览器只能访问公网 Web BFF，不能直接寻址 AIFlow 核心应用和模型服务。
2. HTTPS/TLS 加密浏览器到 BFF 的请求与响应。
3. BFF 使用仅存在于服务端进程内的密钥调用核心 API；抓取浏览器流量得不到该密钥。
4. 匿名会话、来源 IP、核心全局预算和有界队列共同限制机械调用的最大费用。
5. 公网高风险流量由 CDN/WAF、异常封禁和风险触发式验证码继续筛选。

没有登录、平台设备证明或人机验证时，无法保证脚本绝对调不了公网 BFF。Origin/CORS 也只能减少跨站盗用，不是机器人认证。

## 2. 默认 BFF 架构

`./manage.sh run/start` 只启动：

```text
aiflow_server.gateway:app
```

网关在同一进程中创建私有 `aiflow_server.app`，但不把核心路由绑定到独立公网端口。每次启动生成随机的 32 字节内部密钥，不落盘、不返回浏览器。浏览器提交的任何 `X-AIFlow-Client-*` 头都会被删除，网关重新计算请求体哈希、时间戳、nonce 和 HMAC 后调用核心。

`GET /api/v3/capabilities` 对浏览器返回：

```json
{
  "client_auth": {
    "enabled": false,
    "mode": "server_bff",
    "browser_holds_secret": false,
    "core_authenticated": true
  }
}
```

不要手工运行以下入口并监听公网：

```text
aiflow_server.app:app
```

若未来把 BFF 与核心拆到两台机器，应让核心只监听私网地址或 Unix socket，并配置防火墙只接受 BFF；第 5 节的 HMAC 协议可直接作为两服务间协议。

## 3. 匿名限额

网关签发 `aiflow_web_session` Cookie：随机、HMAC 防篡改、HttpOnly、SameSite=Lax。生产 HTTPS 必须设置 `web_gateway.cookie_secure=true` 或 `AIFLOW_WEB_COOKIE_SECURE=true`。

配置示例：

```json
{
  "web_gateway": {
    "require_same_origin": true,
    "cookie_secure": true,
    "trusted_proxy_ips": ["127.0.0.1", "::1"],
    "max_requests_per_session_minute": 120,
    "max_requests_per_ip_minute": 300,
    "max_ai_tasks_per_session_minute": 3,
    "max_ai_tasks_per_session_day": 20,
    "max_ai_tasks_per_ip_day": 100
  },
  "cost_guard": {
    "max_ai_tasks_per_client_minute": 10,
    "max_ai_tasks_per_client_day": 200,
    "max_ai_tasks_global_day": 1000
  }
}
```

网关只在直接连接地址属于 `trusted_proxy_ips` 时读取 `X-Forwarded-For`，因此反向代理必须覆盖而不是追加客户端自带的头。公网不要开放绕过 Nginx/CDN 的备用端口。

清除 Cookie 可以获得新会话，但不会绕过 IP 日限额。代理池仍可绕过 IP 限额，因此费用敏感场景需要 CDN/WAF 规则与 Turnstile/验证码。核心 `cost_guard` 是最后的全局费用保险，不应设置为无限。

超过任一窗口时网关返回 HTTP `429`，错误码格式为 `web_rate_limit_<scope>_<window>`，同时通过 `Retry-After` 响应头和 JSON 的 `retry_after_seconds` 告知客户端最早重试时间。客户端不得收到 `429` 后自动立即重试。

普通请求的会话/IP 分钟计数保存在当前网关进程内，按分钟自动清理，服务重启后重新计数，避免状态轮询和 SSE 恢复请求为每次访问同步写磁盘。AI 任务分钟/每日额度仍持久化到 SQLite，并在线程池中执行；即使服务器磁盘提交变慢，也不会阻塞网页、健康检查或 SSE 的事件循环。当前服务使用单 Uvicorn worker；改为多 worker 或多实例前，应把普通请求计数迁移到共享 Redis 等原子存储。

## 4. 同源与项目能力令牌

POST/PATCH/PUT/DELETE 请求必须满足以下之一：

- `Origin` 的 host 与 API `Host` 相同。
- `Origin` 精确列入 `server.cors_origins`。

这会拦截缺少 Origin 的机械 POST 和普通跨站请求，但 Header 可伪造，所以它不是用户身份验证。

设备连接成功后返回的 `X-AIFlow-Context-Token` 仍用于项目隔离，只能访问对应设备项目、文件、任务和会话。匿名会话 Cookie 与项目 token 是两层不同能力，均不要写入 URL、日志或第三方埋点。SSE 使用任务级只读 stream token。

## 5. 内部 HMAC 协议

Canonical request：

```text
AIFLOW-HMAC-SHA256-V1
METHOD
/path?query
unix_timestamp
nonce
lowercase_sha256_hex_of_body
```

请求头：

```http
X-AIFlow-Client-Key: anonymous-web-bff
X-AIFlow-Timestamp: 1785460000
X-AIFlow-Nonce: unique-base64url-nonce
X-AIFlow-Content-SHA256: 64-lowercase-hex-characters
X-AIFlow-Signature: base64url-hmac-without-padding
```

核心对 key、时钟窗口、请求体哈希和 HMAC 做常量时间校验，再在 SQLite 原子登记 nonce 和请求频率。相同 nonce 无法重放。JSON、multipart 和空 body 都签实际发送字节。

Canonical response acknowledgement：

```text
AIFLOW-HMAC-SHA256-V1-RESPONSE
request_nonce
http_status_code
response_timestamp
```

网关验证核心确认签名后才把响应交给浏览器，并删除全部内部签名头。确认签名不包含响应体，不能替代 TLS。

仓库的 [client_v3.py](../examples/client_v3.py) 保留密钥文件参数，供拆分核心时联调；默认网关模式不要给浏览器或 CLI 配置 HMAC 密钥。

## 6. Agent 事件边界

服务端开启 Claude SDK partial messages，并将有独立展示价值的生命周期、助手文本增量与最终块、完整工具输入/结果、服务端工具、限流、用量、结果和异常按序写入 SQLite，再通过 SSE 发送。逐字符工具参数、签名碎片和高频隐藏推理计数会在写库前过滤，以免单个任务产生上万次无意义写入。高频事件持久化在线程池完成，落库后回到事件循环唤醒 SSE；SQLite 使用 WAL 与 `synchronous=NORMAL`，避免虚拟磁盘的逐事件 fsync 反压模型流。正常服务重启仍会保留事件；宿主机突然断电时，最新少量事务的持久性弱于 `FULL`，但 WAL 数据库仍保持一致。默认每任务保留最近 `10000` 条，可通过 `tasks.event_retention` 调整；内置客户端的原始事件区只保留最近 `2000` 条 DOM 记录。

客户端决定展示策略：实时 UI 可拼接 `assistant_text_delta`，历史 UI 可使用 `assistant_message`；同时消费两者时需按消息 ID 去重。工具事件通过 `tool_use_id` 配对。断线后使用 `/events/history?after=<sequence>` 补偿，再用 SSE 的 `Last-Event-ID` 继续。

以下内容不会原样发送：

- 模型隐藏 chain-of-thought 或 thinking 内容，只发送 `agent_reasoning` 状态及 `content_redacted=true`。
- 绝对工作区路径、`deviceId`、`clientId`、环境中的 API key/token/password/secret、签名字段。
- 超过单事件上限的长文本和过深/过大的结构。

这条边界由服务端强制执行，客户端不能请求关闭脱敏。
