# V2 到 V3 迁移说明

根目录 `server_v2.py` 是 V3 兼容启动入口。旧 V2 实现保存在 `legacy/v2/server_v2.py`，只用于短期迁移和行为比对。

## 主要变化

| V2 | V3 |
| --- | --- |
| 调用方创建/列出任意项目 | `deviceId` 幂等连接设备项目，不提供全局列表 |
| `project_id` 直接决定工作区 | token 只能访问所属 context/workspace |
| `/chat` 请求生命周期绑定 SSE | Coding 在后台运行，SSE 断开后仍可查询 |
| 只依赖流式事件判断进度 | 状态快照、心跳、Agent 静默、stall 标志、事件历史 |
| 无设备资料和直接重跑 | 配对设备资料、部署计划、Agent/服务端部署、direct-run |
| 模型和 Skill 依赖机器环境 | `server_config.json` 配置模型，`skills/` 复制为项目 Skill |
| 无全局容量控制 | 总设备会话、并发执行和等待队列均有限制 |
| 只接收文字 | Coding 支持 Base64 图片和语音附件 |

## 路由迁移

| V2 | V3 |
| --- | --- |
| `POST /projects` | `POST /api/v3/contexts` |
| `POST /chat` | `POST /api/v3/tasks/coding` |
| `/chat` SSE | `GET /api/v3/tasks/{id}/events` |
| `POST /chat/abort` | `POST /api/v3/tasks/{id}/cancel` |
| `GET /projects/{id}` | `GET /api/v3/project` |
| session messages | `/api/v3/conversations/{session_id}/messages` |

V3 的 `access_token` 在首次连接和同 `deviceId` 重连时返回。重连会轮换令牌，网页端应立即更新 `sessionStorage`，后续使用 `X-AIFlow-Context-Token` Header。

完整协议见 [API_V3.md](../API_V3.md)，网页示例见 [WEB_CLIENT_INTEGRATION.md](../WEB_CLIENT_INTEGRATION.md)。
