# AIFlow Analytics Backend Guidance

本文件适用于 `analytics/` 目录。它补充仓库根 `AGENTS.md`，记录独立对话日志分析后台的数据契约、同步语义、统计口径、前端行为和验证要求。

## Goal And Boundary

- 本服务独立拉取火山引擎 TLS 中的 `aiflow_conversation_trace` Schema V2 日志，重建逻辑事件和任务，再提供统计 API 与工作人员监控页面。
- 它与 AIFlow 主服务分进程、分依赖、分 SQLite。入口是 `server.py`，只运行单个 Uvicorn worker；不要导入、查询或修改主服务运行数据库。
- 分析服务只需要 TLS Topic 的只读权限。不要复用上传凭据，不要把 TLS AK/SK、Bearer Token、真实 MAC、用户输入、thinking 或工具结果写入测试、日志摘要或提交记录。
- 根页面和 `/api/v1/*` 面向受信任工作人员。业务 API 默认要求 Bearer Token；`/health` 和 `/ready` 不返回业务日志。

## Source Map

- `aiflow_analytics/parser.py`：TLS envelope 校验、类型解析和 MAC 规范化。
- `aiflow_analytics/database.py`：SQLite schema、非破坏性迁移、物理记录导入、事件组装和 turn 重建。
- `aiflow_analytics/analytics.py`：周期、对比、趋势、成本、会话、设备和数据质量统计。
- `aiflow_analytics/app.py`：FastAPI 生命周期、鉴权、日期参数和 REST 路由。
- `aiflow_analytics/sync.py`、`tls_client.py`：逐日同步、重叠窗口、分页和历史查询回退。
- `web/index.html`、`web/assets/app.js`、`web/assets/app.css`：无构建步骤的同源监控台。
- `model_pricing.json`：按日志实际模型名配置的美元/百万 Token 单价。
- `tests/fixtures.py`：虚构 TLS 物理日志和完整 turn fixture；扩展统计时优先复用它。
- `README.md`：部署、配置、统计口径和 API 的用户文档。

## Reconstruction Contract

数据流固定为：

```text
TLS SearchLogsV2
  -> raw_records (record_id 幂等)
  -> events (event_id 完整分块组装)
  -> turns / tool_calls / turn_model_usage
  -> Analytics
  -> /api/v1/* and web console
```

- `raw_records` 必须保存完整 `raw_json`。`record_id` 是物理记录幂等键，`event_id + chunk_index` 用于分块去重。
- 只有 `chunk_index=0..chunk_count-1` 完整且 envelope 一致时才能写入逻辑 `events`；缺块保留在原始层并进入数据质量统计。
- `turn_id` 是任务主键，`conversation_id` 分组多轮会话，`turn_index` 排序，`event_sequence` 排序单任务事件。会话唯一口径使用 `(project_id, conversation_id)`，不能仅按 `conversation_id` 去重。
- 只从一次 `agent_result` 或 `agent_result_error` 读取 Token、模型用量、SDK 费用和耗时；`task_completed`、`task_failed`、`task_cancelled` 只确认终态，不重复累计用量。
- 同步 SQLite 和 TLS SDK 操作不得阻塞 FastAPI 事件循环；API 继续使用 `asyncio.to_thread` 调用同步分析和存储逻辑。

## Token And Cost Metrics

- `input_tokens` 表示未缓存输入。
- `input_tokens_including_cache = input_tokens + cache_read_input_tokens + cache_creation_input_tokens`。
- `total_tokens = input_tokens_including_cache + output_tokens`。总览、趋势、模型、会话、任务和设备统计必须使用同一口径。
- `cache_hit_rate = cache_read_input_tokens / input_tokens_including_cache`；分母为零时返回 `null`，不要返回虚构的 `0`。
- `configured_actual_usd` / `cost.actual_usd` 只在本周期涉及的所有模型都有完整输入、输出、缓存读取和缓存写入价格时返回。不能拿其他模型价格兜底，也不能静默少算。
- 日志中的 `total_cost_usd` 聚合为 `sdk_reported_usd`，只作为 Claude SDK 计价参考，不作为第三方模型真实账单。
- 修改价格或费用口径时，同时覆盖总览、逐模型、单任务、会话、设备和页面展示，并更新 `README.md` 与测试。

## MAC And Device Metrics

- `mac_address` 位于 TLS envelope 顶层，不在 `payload`。解析后必须贯通 `raw_records`、`events` 和 `turns`。
- 缺失、`None`、空白、字符串 `null` 或 `none` 统一保存为空字符串，并完全排除在设备数和 `/api/v1/devices` 明细之外。
- 12 位十六进制 MAC 的裸格式、冒号、短横线、点分和空格格式统一为 `AA:BB:CC:DD:EE:FF`。无法识别但非空的值只 `strip()` 后保留，避免误丢上游标识。
- 同一事件的所有分块必须具有一致 MAC；turn 内只在所有非空事件 MAC 唯一时记录设备，否则留空，避免把冲突数据归到错误设备。
- 旧库迁移必须使用非破坏性 `ALTER TABLE`。首次新增 MAC 列时从 `raw_records.raw_json` 回填、重新组装相关事件并重建 turn；不要要求删除数据库或重新拉取 TLS，也不要每次启动全表扫描旧空值。
- `overview.volume.devices` 是筛选周期内 `turns.mac_address != ''` 的去重设备数。
- `/api/v1/devices` 必须独立分页，至少返回 MAC、项目数、会话数、任务及终态、四类 Token、含缓存总 Token、缓存命中率、按模型价格费用、SDK 参考费用、工具数和最近活动时间。网页当前每页展示 10 台，但 API 可支持更大的受限页大小。

## Sync Semantics

- `SearchLogsV2` 单页最多 100 条，配置值不能让实际请求超过该上限。
- 物理 TLS 是 at-least-once；重复、重叠窗口和重试继续依赖 `record_id` 幂等，不删除已导入记录。
- 当 `event:aiflow_conversation_trace` 索引查询为空时，对同一窗口使用 `*` 回退并在本地按 `event` 过滤。只有拉到记录或通配回退确认确实为空，历史日期才能标记为已同步。
- 当天不写入永久完成标记；启动和周期任务要继续刷新当天重叠窗口。旧 `fetched=0` 且未通过回退验证的日期必须重新检查。
- 常规测试和 smoke test 不请求真实 TLS。手动 `POST /api/v1/sync`、真实历史回填、生产数据迁移或远端重启需要用户本次明确授权。

## Web Console Contract

- 页面是运维工作台，不是营销页。保持信息密度、可扫描性和现有视觉系统，不引入构建步骤或新前端框架。
- 核心卡片展示任务、设备、会话深度、含缓存 Token、缓存命中率、费用、耗时和错误率；数据质量保留独立面板。
- 设备列表独立分页；桌面按列展示，移动端改为紧凑明细，不能产生横向滚动、文本遮挡或元素重叠。
- 最近活动按“项目 -> 会话 -> 任务”分组，会话内任务按时间顺序排列。用户消息直接突出显示；模型 thinking、回复、工具事件、工具结果和模型用量默认折叠。
- 所有日志内容通过 `textContent` 纯文本展示，不能插入未清洗 HTML。浏览器令牌只放当前会话 `sessionStorage`。
- 修改静态资源时保留明确的 cache-busting 版本，避免无热更新服务或浏览器缓存继续加载旧页面。

## Development Commands

在 `analytics/` 目录运行：

```bash
./manage.sh install
./manage.sh run
./manage.sh start
./manage.sh status
./manage.sh logs
./manage.sh stop
./manage.sh config
```

`status` 必须同时检查进程、`/health` 和 `/ready`。`config` 输出必须脱敏；不要直接 dump `.env` 或完整环境变量。

## Verification Matrix

完整分析模块回归：

```bash
PYTHONPATH=. .venv/bin/pytest -q
.venv/bin/ruff check aiflow_analytics tests server.py
.venv/bin/python -m py_compile aiflow_analytics/*.py server.py
node --check web/assets/app.js
bash -n manage.sh
git diff --check
```

从仓库根目录运行时使用根 `.venv` 和 `PYTHONPATH=analytics`：

```bash
PYTHONPATH=analytics .venv/bin/pytest -q analytics/tests
.venv/bin/ruff check analytics/aiflow_analytics analytics/tests analytics/server.py
```

按改动范围至少执行：

- `parser.py`：`tests/test_parser.py`，覆盖 envelope、分块和 MAC 空值/格式规范化。
- `database.py`：`tests/test_database.py`，覆盖幂等、缺块、turn 重建、旧库 schema 迁移、Token 修正和 `raw_json` MAC 回填。
- `analytics.py`：`tests/test_analytics.py`，覆盖总览、价格完整性、模型、会话、设备、对比和数据质量。
- `app.py` 或 API：`tests/test_api.py`，覆盖鉴权、日期范围、分页和返回字段。
- `sync.py` / `tls_client.py`：`tests/test_sync.py`、`tests/test_tls_client.py`，只能使用 fake client。
- `web/`：JavaScript 语法检查，并用虚构 SQLite 启动本地服务，在实际浏览器检查至少 1440px 桌面和 390px 移动视口。验证设备分页、会话分组、任务详情、用户消息突出、非用户事件默认折叠、无横向溢出/遮挡/控制台错误。
- 跨仓库事件或主服务改动：再运行根目录 `.venv/bin/python -m pytest -q` 和 `node --test tests/assistant_stream_state.test.cjs`。

## Done Criteria

- 实现、数据库迁移、API、页面、测试和 `README.md` 的字段与口径一致。
- 旧 SQLite 可原地升级且数据可恢复；没有要求用户删除数据库，没有破坏同步完成状态。
- 测试只使用虚构 MAC、临时 SQLite 和 fake TLS，不访问真实 Topic、模型或设备。
- 业务 API 仍受鉴权保护，页面无敏感值泄露，日志只按纯文本渲染。
- 最终 diff 不包含 `.env`、`data/`、`.runtime/`、数据库、日志、证书、私钥或无关文件。
