# AIFlow 对话日志分析后端

独立拉取火山引擎 TLS 中的 `aiflow_conversation_trace` Schema V2 日志，将物理分块重建为逻辑事件和完整 turn，再提供用量、成本、耗时、thinking、工具、对话深度、趋势对比与数据质量 API。

分析服务和 AIFlow 主服务完全分进程、分依赖、分数据库运行，可部署在另一台只具备 TLS 只读权限的服务器。它不会读取 AIFlow 主服务 SQLite，也不会影响 Coding/SSE 主线程。

## 数据模型

1. `raw_records` 按 `record_id` 保存 TLS 物理记录，天然幂等。
2. `events` 按 `event_id` 检查 `chunk_index=0..chunk_count-1` 后拼接 JSON；缺块时不制造半条事件。
3. `turns`、`tool_calls`、`turn_model_usage` 按 `turn_id` 重建任务状态、内容量、token、费用、耗时、模型和工具链。
4. API 按 `conversation_id`、`project_id`、时间范围聚合，不返回原始设备 ID 或 client ID。

物理 TLS 链路是 at-least-once，分析库以 `record_id` 去重。`event_sequence` 空档是正常的，因为高频 delta 和结构事件本来就不上传 TLS。

## 统计能力

- 任务、对话、项目量，完成率、失败率、取消与不完整任务。
- input/output/cache token、token/turn、输出输入比。
- 总费用、单任务费用、成功任务费用、每千 token 费用。
- Agent/API/排队/服务总耗时的平均值、P50、P95。
- thinking/回复块和字符量、thinking/output 比、partial 数量。
- 工具调用、结果、错误率、平均/P95 耗时、孤立调用与结果。
- 模型级 token、缓存、Web Search、成本、provider、canonical model。
- 文件与部署次数、部署成功率。
- 当前周期与等长上一周期的 delta、变化率，以及小时/日/周趋势。
- conversation 多轮深度、成本、token、工具与内容量。
- 分块缺失、解析错误、缺终态、缺 ResultMessage、partial、物理重复等质量指标。
- 单个 turn 的完整逻辑事件时间线，用于还原输入、thinking、回复和工具过程。

## 单轮用量与耗时来源

每个 `turn_id` 只从一次 `agent_result`（失败时为 `agent_result_error`）读取 Claude Code SDK
的权威结果，不从 `task_completed` 再累计一遍：

- `input_tokens`、`output_tokens`、`cache_read_input_tokens`、
  `cache_creation_input_tokens` 来自 `ResultMessage.usage`。
- `total_cost_usd` 来自 `ResultMessage.total_cost_usd`；第三方模型未返回时保留为空，
  分析服务不按公开价猜测实际账单。
- `duration_ms` 是 SDK 报告的 Agent 总耗时，`duration_api_ms` 是其中的模型 API 耗时。
- `turn_model_usage` 保存 `ResultMessage.model_usage` 的逐模型 token、成本、provider、
  canonical model 和 Web Search 次数。
- `queue_duration_ms` 由 `user_input -> task_started` 计算，`service_duration_ms` 由
  `user_input -> task_completed/task_failed/task_cancelled` 计算，工具耗时按同一个
  `tool_use_id` 的开始和结束时间计算。

`task_completed` 只确认终态并引用已经存在的 Agent、文件和部署事件，因此总览、趋势和
conversation 聚合不会重复累计同一轮用量。

## TLS 前置条件

服务使用 `SearchLogsV2` 分页协议。TLS Topic 应为以下 envelope 字段建立键值索引，并为 `payload` 建全文索引：

`event`、`schema_version`、`record_id`、`event_id`、`project_id`、`conversation_id`、`turn_id`、`turn_index`、`event_sequence`、`event_type`、`chunk_index`、`is_terminal`。

默认查询为 `event:aiflow_conversation_trace`。如果该键值查询返回空结果，客户端会对同一时间窗口自动用 `*` 查询并在本地按 `event` 过滤，以兼容索引建立后历史日志未及时完成索引的情况；长期仍建议配置索引，否则回填会更慢且查询成本更高。建议为分析服务器创建只读 AK/SK，不复用上传凭据。

## 安装与运行

需要 Python 3.11 或更新版本。如果系统默认 `python3` 较旧，可通过 `AIFLOW_ANALYTICS_PYTHON=/path/to/python3.12 ./manage.sh install` 指定解释器。

```bash
cd analytics
./manage.sh install
cp .env.example .env
chmod 600 .env
```

编辑 `.env`，填入 TLS 只读 AK/SK，并用 `openssl rand -hex 32` 生成 API Token。随后：

```bash
./manage.sh config
./manage.sh start
./manage.sh status
./manage.sh logs
./manage.sh restart
./manage.sh stop
```

`start`/`restart` 会等待 `http://127.0.0.1:5090/ready`。`stop` 只操作当前 analytics 目录 PID 文件记录且所有权校验通过的进程。

## Web 监控台

服务根路径现在提供工作人员使用的实时监控页面：

```text
http://<服务器地址>:5090/
```

页面会自动每 30 秒刷新，也可以手动刷新或提交选定日期范围的后台 TLS 同步。首次打开时在登录框输入
`AIFLOW_ANALYTICS_API_TOKEN` 对应的 Bearer Token；令牌只保存在当前浏览器的 `sessionStorage`，关闭浏览器后不会保留。
页面展示任务、完成率、Token、费用、耗时、thinking 字符、工具错误率、趋势、模型/工具分布、数据质量和最近任务。
点击最近任务可以查看该轮用户输入、模型 thinking、回复、工具调用、工具结果和终态事件的完整时间线。日志内容按纯文本展示，不会作为 HTML 执行。

根路径只返回页面，不返回业务 JSON。健康检查仍使用 `/health` 和 `/ready`，接口文档使用 `/docs`；未授权访问业务 API 仍返回 `401`。

## 增量同步

- 首次启动从 `AIFLOW_ANALYTICS_START_DATE` 起逐日回填。
- `AIFLOW_ANALYTICS_TLS_PAGE_SIZE` 应保持在 `1` 到 `100`；这是 Volcengine `SearchLogsV2` 的单页上限。旧配置写成更大的值时，客户端会自动按 `100` 请求并记录警告。
- 已成功完成的历史日写入 `sync_days`，默认不重复拉取；只有实际取到过记录，或通配查询已确认该日没有目标事件，才算完成。旧版本留下的 `fetched=0` 未验证标记、缺少成功标记或上次有解析错误的日期会在后续周期自动重试。
- 每次服务启动都会重新拉取启动日的当天窗口；当天不会写入 `sync_days`，所以重启后仍会刷新当天日志。启动历史回填失败后，周期任务会先补齐历史缺口，再同步最近窗口，不需要再次手动删除数据库。
- 当天每 `AIFLOW_ANALYTICS_SYNC_INTERVAL_SECONDS` 秒同步，并向前重叠 `AIFLOW_ANALYTICS_SYNC_OVERLAP_MINUTES` 分钟。
- 重叠、分页重复和超时重发均由 `record_id` 幂等处理。
- `POST /api/v1/sync` 只启动后台线程；同一时间只允许一个同步任务。

## 鉴权

日志包含用户输入、模型 thinking、回复和工具结果。默认所有 `/api/v1/*` 都要求：

```http
Authorization: Bearer <AIFLOW_ANALYTICS_API_TOKEN>
```

`/health`、`/ready` 和根服务发现接口不返回业务数据。只有在端口被防火墙严格隔离时，才考虑设置 `AIFLOW_ANALYTICS_AUTH_DISABLED=true`。

## REST API

时间范围使用包含首尾日期的 `start_date`、`end_date`；不传时默认最近 7 天。

| 接口 | 用途 |
| --- | --- |
| `GET /health`、`GET /ready` | 存活与 SQLite 就绪状态 |
| `GET /api/v1/status` | 非敏感配置、同步覆盖和进度 |
| `POST /api/v1/sync` | 后台触发日期范围回填 |
| `GET /api/v1/overview` | 复杂总览指标 |
| `GET /api/v1/compare` | 当前周期与等长上一周期对比 |
| `GET /api/v1/trends?bucket=day` | 小时/日/周趋势 |
| `GET /api/v1/breakdowns` | 模型、工具、状态、原因、项目分解 |
| `GET /api/v1/dashboard` | 一次返回总览、对比、趋势、分解和质量 |
| `GET /api/v1/conversations` | 多轮 conversation 聚合与分页 |
| `GET /api/v1/turns` | 按状态、项目、conversation、模型、工具筛选 |
| `GET /api/v1/turns/{turn_id}` | 完整逻辑事件、工具与模型用量时间线 |
| `GET /api/v1/data-quality` | 分块、解析、终态、partial 和工具配对质量 |

`GET /api/v1/status` 的 `sync.historical_sync_needed=true` 表示开始日期到昨天仍有未成功回填的日期；后台周期任务会自动重试这些日期。当天不写入 `sync_days`，每次服务启动都会重新拉取当天窗口。

```bash
curl -H "Authorization: Bearer $AIFLOW_ANALYTICS_API_TOKEN" \
  "http://127.0.0.1:5090/api/v1/dashboard?start_date=2026-08-01&end_date=2026-08-10&bucket=day"
```

## systemd

默认 unit 使用 `/opt/aiflow-sdk/analytics` 和用户 `aiflow-analytics`：

```bash
sudo useradd --system --home /opt/aiflow-sdk/analytics --shell /usr/sbin/nologin aiflow-analytics
sudo mkdir -p /opt/aiflow-sdk/analytics/data /opt/aiflow-sdk/analytics/.runtime
sudo chown -R aiflow-analytics:aiflow-analytics /opt/aiflow-sdk/analytics
sudo cp deploy/aiflow-analytics.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now aiflow-analytics
```

实际路径或用户不同时，调整 `WorkingDirectory`、`EnvironmentFile`、`ExecStart` 和 `ReadWritePaths`。

## 验证

```bash
cd analytics
.venv/bin/pip install -r requirements-dev.txt
PYTHONPATH=. .venv/bin/pytest -q
.venv/bin/ruff check aiflow_analytics tests server.py
.venv/bin/python -m py_compile aiflow_analytics/*.py server.py
bash -n manage.sh
./manage.sh help
```

测试使用临时 SQLite 和 fake TLS client，不请求真实 Topic、不消耗模型费用，也不接触设备。
