# AIFlow SDK Project Guidance

本文件是 `aiflow-sdk` 的项目级约束。它补充用户的全局 `AGENTS.md`，只记录本仓库的架构、命令、行为契约和验证要求。

## Project Goal

本项目提供面向免登录网页客户端的 M5Stack UIFlow2/MicroPython Coding 与设备部署服务：

- FastAPI 匿名 Web BFF 对外提供同源网页和 V3 API。
- 私有核心 API 与 BFF 运行在同一进程，通过进程内随机 HMAC 密钥通信。
- Claude Code Agent SDK 在按设备隔离的工作区中编写代码，并通过 SSE 公开任务事件。
- 服务端或 Agent 可在明确授权的部署模式下调用 AIFlow 设备推送 API。

维护本仓库的 Codex 可以处理后端、前端、测试、文档和部署问题。下面的 M5Stack 范围限制约束的是服务中运行的 Coding Agent，不应阻止仓库维护工作。

## Source Of Truth

- `aiflow_server/`：当前 V3 服务实现。
- `web/`：无构建步骤的同源网页客户端，入口为 `web/index.html` 和 `web/app.js`。
- `analytics/`：独立的 TLS 对话日志分析后台、SQLite 分析库和监控页面；处理统计、设备用量、历史同步或后台 UI 前，先读 `analytics/AGENTS.md`。
- `skills/`：复制到设备工作区 `.claude/skills/` 的运行时 Skills。
- `server_config.json`：非机密默认配置和容量、限流、Agent、推送设置。
- `.env.example`：第三方模型和部署环境变量示例；真实值只放 `.env.local` 或外部环境文件。
- `docs/API_V3.md`、`docs/WEB_CLIENT_INTEGRATION.md`、`docs/CLIENT_SECURITY.md`：当前协议、网页接入和安全边界。
- `docs/THIRD_PARTY_DEVICE_PUSH_API.md`：底层设备推送协议。
- `server_v2.py`：兼容入口，仍必须启动 `aiflow_server.gateway:app`。
- `legacy/v2/` 和 `docs/legacy/`：迁移参考，不是当前实现依据，除非任务明确要求迁移兼容，否则不要修改。

接口、字段、配置或网页行为变化时，同步更新 README、对应 V3 文档、示例和测试。较大的架构变更在 `docs/plans/` 增加或更新计划。

## Analytics Backend Boundary

- 分析后台入口是 `analytics/server.py`，核心实现位于 `analytics/aiflow_analytics/`，页面位于 `analytics/web/`；它与 `aiflow_server.gateway:app` 分进程、分依赖、分数据库运行。
- 分析后台通过只读火山引擎 TLS `SearchLogsV2` 拉取 `aiflow_conversation_trace`，不得读取或修改 AIFlow 主服务 SQLite。日常本地运行和配置检查使用 `cd analytics && ./manage.sh ...`。
- TLS 物理记录先进入 `raw_records`，完整分块再组装为 `events`，最后按 `turn_id` 重建 `turns`、`tool_calls` 和 `turn_model_usage`；不要绕过原始层或用不完整分块制造事件。
- 总 Token 口径固定为“未缓存输入 + 缓存读取 + 缓存写入 + 输出”；缓存命中率固定为“缓存读取 /（未缓存输入 + 缓存读取 + 缓存写入）”。费用优先按 `analytics/model_pricing.json` 的逐模型价格计算，SDK `total_cost_usd` 仅作为 Claude 计价参考。
- Claude Code 的零用量 `<synthetic>` 只作为原始追踪记录保留，不进入模型展示或计费完整性判断；纯零用量任务按已知 `$0.00` 处理，synthetic 错误回复不得覆盖运行时主模型。任何非零 synthetic 用量仍需显式展示，避免漏算。
- TLS 顶层 `mac_address` 是可选设备统计维度。缺失、空白、`null` 或 `none` 的旧日志不计入设备数和设备明细；常见 MAC 格式统一为大写冒号格式。旧分析库升级必须非破坏性加列，并从 `raw_records.raw_json` 回填已有非空 MAC，不要求重拉 TLS。
- 最近活动按项目、会话、任务分层展示；会话按 `(project_id, conversation_id)` 识别，内部任务按时间顺序排列。用户消息保持突出，thinking、模型回复、工具和模型用量默认折叠。
- 分析趋势图将任务/工具次数放左轴、Token 放右轴；模型、计费、工具、状态和数据质量面板允许拖动排序并在浏览器本地保存。模型 API P95 和部署成功率必须通过可聚焦问号说明 SDK 耗时与部署尝试口径。
- 分析后台的详细数据契约、同步规则、测试矩阵和浏览器验收要求以 `analytics/AGENTS.md` 为准。

## Runtime Architecture Invariants

- 对外只能运行 `aiflow_server.gateway:app`。不要把 `aiflow_server.app:app` 单独监听到公网。
- 保持单个 Uvicorn worker。任务队列、取消信号、订阅器和部分限流是单进程内存状态；引入多 worker 前必须先迁移到共享调度/队列。
- 浏览器不持有核心 HMAC 密钥、模型密钥或长期客户端密钥。不要把固定密钥、签名算法秘密或模型凭证写入 JavaScript。
- 修改请求继续执行同源校验；生产 CORS 使用精确 Origin，不使用通配 Origin 访问能力令牌接口。
- `device_id` 是设备项目的稳定主键，`client_id` 是资源上传标识。创建上下文时两者都必填；不要猜测、生成、交换或用 `context_id`/MAC 兜底。
- 同一 `device_id` 重连必须复用工作区和会话、更新 `client_id`、轮换能力令牌。SQLite 只保存令牌哈希。
- 工作区必须按 context 隔离。所有用户文件和部署清单路径都要经过边界检查，禁止越出当前 workspace，也不能公开 `.claude`、`.aiflow` 或 `.git` 内部文件。
- `deploy_mode=none` 不接触设备；`server` 在 Agent 完成后由服务端推送；`agent` 才向 Agent 暴露推送 Skill、目标和网络，并保持一次离线 `plan` 加一次最终推送。
- `direct-run` 跳过 Agent，但仍是后台任务并占用同一并发队列。`main.py` 必须走代码接口，不能作为资源上传。
- HTTP 推送成功只表示代码或资源已提交到推送服务，不代表设备已经执行、保存或 ACK。

## Embedded Agent Contract

修改 `aiflow_server/agent.py`、运行时 Skills 或相关配置时，保持以下行为：

- Coding Agent 只接受以 UIFlow2/MicroPython 代码为交付物的 M5Stack 编程、调试、审查和解释任务。
- 非 M5Stack 编程请求应简短拒绝且不调用工具；目标不清楚时先询问产品型号和编程目标，不写文件、不部署。
- `uiflow2-coder` 是编程时优先建议使用的官方文档来源，但不是强制的第一个工具。已有信息足够时可跳过；需要规格、引脚、电气特性或官方排障依据时，允许直接先用 `m5stack-assistant`。
- 只有确认官方资料缺失、冲突、明显错误、示例损坏或 MCP 异常时，才按 `m5stack-assistant` 规则提交反馈；普通用户代码 bug 不上报。
- 不虚构进度或内部推理。禁止用“已初始化”“正在思考”“正在组织参数”等占位总结替代 SDK 实际公开事件。
- 所有公开 TextBlock 都按用户本人最近一条可识别自然语言回复；用户明确指定语言时优先。不得根据 UIFlow/API 术语、Skill/MCP、文档、代码、日志或工具输入输出切换语种。混合文本以用户自然语言主体为准，代码标识和产品名保持自然；表达应尊重、直接、符合用户技术水平，避免机械翻译和流程化套话。
- SDK 实际提供的 thinking delta、最终块和中断 partial 必须经统一脱敏后进入公开任务事件；不得用空活动标记替代，也不得节流丢失。TLS 仍只上传最终块或一次 partial，避免重复。
- 事件中必须脱敏模型密钥、令牌、`device_id`、`client_id`、绝对工作区路径和敏感工具参数。
- 缺少关键硬件/API 事实或本地验证失败时停止部署，不猜测设备行为。

## SSE And Task Semantics

- `GET /api/v3/tasks/{task_id}` 是权威状态；SSE 是实时体验层，断线后必须能按 sequence 从 history 恢复。
- 保留 SDK 提供且允许公开的模型文本、工具调用、工具结果和分析事件。不要为了降低事件数而丢弃或机械聚合 `assistant_text_delta`。
- `assistant_text_delta` 必须边到边发；最终 `assistant_message` 用于校准同一 response/block，不得造成重复文本、覆盖错误或跨 block 合并。
- 心跳、连接状态、任务阶段、模型输出和工具事件是不同信息层。前端应分区展示，不能用心跳伪装 Agent 活动，也不能让虚构百分比来回跳。
- 高频同步 SQLite 或网络操作不得阻塞 asyncio 事件循环。使用现有 `asyncio.to_thread` 模式处理同步持久化和设备 HTTP。
- 任务事件数据库保持 WAL；当前使用 `synchronous=NORMAL` 是针对慢虚拟磁盘的明确性能选择。变更该模式前必须测量事件写入延迟并说明断电持久性权衡。
- 不要恢复每个模型 delta 都同步更新任务状态的行为。Agent 活跃时间允许节流写入，但公开文本事件本身不能被节流丢失。
- SSE 等待使用任务订阅信号，不恢复固定间隔数据库轮询。

## Security And Side Effects

- 不提交或打印 `.env.local`、API key、auth token、能力令牌、Cookie、SSH 凭证、真实 `device_id`/`client_id` 或生产请求体。示例和测试使用明显的虚构值。
- 模型凭证只通过 `ANTHROPIC_BASE_URL`、`ANTHROPIC_AUTH_TOKEN` 或 `ANTHROPIC_API_KEY` 等环境变量配置；认证变量按提供方要求二选一。
- 未经用户当前请求明确授权，不执行真实模型任务、设备推送、远端部署/重启、项目删除、上下文删除或生产数据迁移。
- 单元和集成测试必须使用 fake runner、临时目录和本地 recording HTTP server，不消耗模型费用、不访问真实设备推送端点。
- 分析后台测试必须使用 fake TLS client、临时 SQLite 和虚构 MAC；常规验证不拉取真实 TLS Topic，不展示或提交真实设备 MAC。真实同步、生产迁移和远端分析服务重启需要用户本次明确授权。
- 部署计划接口是离线校验；实际 `direct-run`、`deploy_mode=server/agent` 和推送脚本的非 `plan` 命令都属于设备状态变更。
- 删除上下文会删除项目、任务、事件和文件，属于破坏性操作；先解析精确目标并取得明确授权。

## Development Commands

首次安装或虚拟环境失效：

```bash
./manage.sh install
```

日常本地运行：

```bash
./manage.sh run
./manage.sh start
./manage.sh status
./manage.sh logs
./manage.sh stop
```

`start` 默认等待健康检查 60 秒，可通过 `AIFLOW_START_TIMEOUT_SECONDS` 调整。`status` 必须同时验证进程、`/health` 和 `/ready`。配置排查优先使用会脱敏输出的：

```bash
./manage.sh config
```

不要在诊断输出中直接 dump 完整环境变量或 `.env.local`。

## Verification Matrix

已安装开发依赖时，完整 Python 回归：

```bash
.venv/bin/python -m pytest -q
```

网页流合并逻辑还需直接运行 Node 测试：

```bash
node --test tests/assistant_stream_state.test.cjs
```

按改动范围至少执行：

- `agent.py` 或 Agent prompt/事件：`tests/test_agent_behavior.py`、`tests/test_task_event_efficiency.py`、网页流测试。
- `gateway.py`、鉴权、限流：`tests/test_gateway.py`、`tests/test_client_auth.py`。
- `tasks.py`、`storage.py`、SSE：`tests/test_server_v3.py`、`tests/test_storage_migration.py`、`tests/test_task_event_efficiency.py`、`tests/test_web_stream_state.py`。
- `device_push.py` 或推送 Skill：`tests/test_device_push.py`，只允许本地 fake server。
- `schemas.py`、`config.py`、API 字段：`tests/test_config.py`、相关 V3 测试和文档示例。
- `web/`：Node 测试，并在实际浏览器检查桌面/移动视口、SSE 增量、工具结果、心跳区、断线恢复和无重复最终回复。
- `manage.sh`：`bash -n manage.sh`，并验证目标命令的成功与失败路径。
- `analytics/`：按 `analytics/AGENTS.md` 运行分析模块测试、Ruff、Python/JavaScript 语法和 `analytics/manage.sh` 检查；后台页面改动还要用虚构数据检查桌面和移动视口、独立分页、任务详情及无横向溢出。

提交前通用检查：

```bash
.venv/bin/python -m py_compile aiflow_server/*.py server_v2.py examples/client_v3.py
bash -n manage.sh
git diff --check
```

运行时相关变更在端口可用且没有真实任务时，再做本地 smoke test：启动 gateway 后检查 `/health`、`/ready`、`/api/v3/system/status` 和 `/client`。不要用真实 Coding 请求作为常规 smoke test。

## Remote Deployment Procedure

仅在用户明确授权本次远端变更后执行：

1. 只同步本次修改涉及的运行文件，不覆盖远端配置、数据目录、虚拟环境或用户改动。
2. 在远端 `.runtime/` 下按时间戳备份将被替换的文件，并记录本地/远端哈希。
3. 重启前运行 `py_compile` 或对应静态检查。
4. 使用 `./manage.sh restart` 和 `./manage.sh status`，再检查 `/health`、`/ready`、`/api/v3/system/status` 与最新日志。
5. 默认用无费用、无设备副作用的检查验证；真实模型和设备验证需要单独明确授权。
6. 报告备份位置、验证结果和可回滚范围，但不在聊天中复述密码、令牌或真实设备标识。

## Done Criteria

完成修改前确认：

- 行为符合上述架构和安全不变量。
- 相关实现、测试、README/API/客户端文档保持一致。
- 没有意外修改 `legacy/`、运行数据、密钥文件或无关用户文件。
- 相关测试、静态检查和必要 smoke test 已执行；无法执行的项目和剩余风险已明确说明。
- 涉及模型费用、设备、远端服务或数据删除的操作都具有本次任务的明确授权和可核对结果。
