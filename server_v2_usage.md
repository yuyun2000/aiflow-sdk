# Server V2 使用文档

## 概述

Server V2 提供了完整的多项目管理、会话历史和详细响应信息的 API 服务。

## 核心概念

### ID 体系设计

```
项目层（你的服务管理）
├── Project ID: "proj_abc123def456"  # 你生成的项目标识
├── Working Directory: "./projects_data/proj_abc123def456/workspace"
└── 会话层（Claude SDK 管理）
    ├── Session ID: "550e8400-e29b-41d4-a716-446655440000"  # Claude 自动生成
    ├── Session ID: "660e8400-e29b-41d4-a716-446655440001"
    └── ...
```

**关键点：**
1. **Project ID** - 你自己生成和管理，用于区分不同项目
2. **Working Directory** - 每个项目有独立的工作目录，Claude 根据这个目录隔离会话
3. **Session ID** - Claude CLI 自动生成的 UUID，每次对话都会创建新的 session

### 为什么这样设计？

- Claude SDK 本身使用 `cwd`（工作目录）来隔离项目
- 每个工作目录下的会话文件存储在 `~/.claude/projects/<sanitized-cwd>/`
- SDK 提供了 `list_sessions()` 和 `get_session_messages()` 来访问历史

## API 端点

### 1. 创建项目

```bash
POST /projects
Content-Type: application/json

{
  "name": "我的 UIFlow 项目",
  "description": "M5Stack 温度监控项目"
}
```

**响应：**
```json
{
  "project_id": "proj_abc123def456",
  "name": "我的 UIFlow 项目",
  "description": "M5Stack 温度监控项目",
  "created_at": "2026-04-13T10:30:00",
  "updated_at": "2026-04-13T10:30:00",
  "working_directory": "/path/to/projects_data/proj_abc123def456/workspace"
}
```

### 2. 列出所有项目

```bash
GET /projects
```

**响应：**
```json
[
  {
    "project_id": "proj_abc123def456",
    "name": "我的 UIFlow 项目",
    "description": "M5Stack 温度监控项目",
    "created_at": "2026-04-13T10:30:00",
    "updated_at": "2026-04-13T10:30:00",
    "working_directory": "/path/to/projects_data/proj_abc123def456/workspace"
  }
]
```

### 3. 获取项目详情（包含会话列表）

```bash
GET /projects/{project_id}
```

**响应：**
```json
{
  "project": {
    "project_id": "proj_abc123def456",
    "name": "我的 UIFlow 项目",
    "description": "M5Stack 温度监控项目",
    "created_at": "2026-04-13T10:30:00",
    "updated_at": "2026-04-13T10:30:00",
    "working_directory": "/path/to/projects_data/proj_abc123def456/workspace"
  },
  "sessions": [
    {
      "session_id": "550e8400-e29b-41d4-a716-446655440000",
      "summary": "创建温度传感器读取代码",
      "last_modified": 1713000000,
      "file_size": 12345
    }
  ]
}
```

### 4. 获取会话历史消息

```bash
GET /projects/{project_id}/sessions/{session_id}/messages?limit=10&offset=0
```

**响应：**
```json
{
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "messages": [
    {
      "type": "user",
      "uuid": "msg-uuid-1",
      "message": {
        "role": "user",
        "content": "创建一个读取温度传感器的代码"
      }
    },
    {
      "type": "assistant",
      "uuid": "msg-uuid-2",
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

### 5. 发起对话（流式响应）

```bash
POST /chat
Content-Type: application/json

{
  "project_id": "proj_abc123def456",
  "prompt": "创建一个显示温度的 UIFlow 程序",
  "session_id": null  // 可选：继续特定会话
}
```

**响应（SSE 流）：**

```
data: {"type":"message","data":{"text":"我来帮你创建温度显示程序..."}}

data: {"type":"message","data":{"text":"正在生成代码..."}}

data: {"type":"file","data":{"name":"main.py","content":"from m5stack import *\n..."}}

data: {"type":"result","data":{"session_id":"550e8400-e29b-41d4-a716-446655440000","total_cost_usd":0.0123,"usage":{"input_tokens":1000,"output_tokens":500,"cache_creation_input_tokens":0,"cache_read_input_tokens":800},"stop_reason":"end_turn","duration_ms":5000,"num_turns":3}}

data: {"type":"done","data":null}
```

### 6. 删除项目

```bash
DELETE /projects/{project_id}
```

**响应：**
```json
{
  "message": "项目已删除"
}
```

## 使用示例

### Python 客户端示例

```python
import httpx
import json

BASE_URL = "http://localhost:8000"

async def main():
    async with httpx.AsyncClient() as client:
        # 1. 创建项目
        response = await client.post(
            f"{BASE_URL}/projects",
            json={
                "name": "温度监控项目",
                "description": "M5Stack 温度传感器"
            }
        )
        project = response.json()
        project_id = project["project_id"]
        print(f"创建项目: {project_id}")

        # 2. 发起对话
        async with client.stream(
            "POST",
            f"{BASE_URL}/chat",
            json={
                "project_id": project_id,
                "prompt": "创建一个显示温度的程序"
            }
        ) as response:
            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    data = json.loads(line[6:])
                    
                    if data["type"] == "message":
                        print(f"Claude: {data['data']['text']}")
                    
                    elif data["type"] == "file":
                        print(f"生成文件: {data['data']['name']}")
                        print(data['data']['content'])
                    
                    elif data["type"] == "result":
                        result = data['data']
                        print(f"Session ID: {result['session_id']}")
                        print(f"Cost: ${result['total_cost_usd']:.4f}")
                        print(f"Tokens: {result['usage']}")
                    
                    elif data["type"] == "done":
                        break

        # 3. 获取项目信息
        response = await client.get(f"{BASE_URL}/projects/{project_id}")
        project_info = response.json()
        print(f"会话数量: {len(project_info['sessions'])}")

        # 4. 获取会话历史
        if project_info['sessions']:
            session_id = project_info['sessions'][0]['session_id']
            response = await client.get(
                f"{BASE_URL}/projects/{project_id}/sessions/{session_id}/messages"
            )
            history = response.json()
            print(f"消息数量: {len(history['messages'])}")

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
```

### JavaScript/TypeScript 客户端示例

```typescript
const BASE_URL = "http://localhost:8000";

async function main() {
  // 1. 创建项目
  const projectRes = await fetch(`${BASE_URL}/projects`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      name: "温度监控项目",
      description: "M5Stack 温度传感器"
    })
  });
  const project = await projectRes.json();
  const projectId = project.project_id;
  console.log(`创建项目: ${projectId}`);

  // 2. 发起对话（SSE）
  const chatRes = await fetch(`${BASE_URL}/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      project_id: projectId,
      prompt: "创建一个显示温度的程序"
    })
  });

  const reader = chatRes.body.getReader();
  const decoder = new TextDecoder();

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    const chunk = decoder.decode(value);
    const lines = chunk.split("\n");

    for (const line of lines) {
      if (line.startsWith("data: ")) {
        const data = JSON.parse(line.slice(6));

        if (data.type === "message") {
          console.log(`Claude: ${data.data.text}`);
        } else if (data.type === "file") {
          console.log(`生成文件: ${data.data.name}`);
          console.log(data.data.content);
        } else if (data.type === "result") {
          console.log(`Session ID: ${data.data.session_id}`);
          console.log(`Cost: $${data.data.total_cost_usd.toFixed(4)}`);
          console.log(`Tokens:`, data.data.usage);
        } else if (data.type === "done") {
          break;
        }
      }
    }
  }

  // 3. 获取项目信息
  const infoRes = await fetch(`${BASE_URL}/projects/${projectId}`);
  const projectInfo = await infoRes.json();
  console.log(`会话数量: ${projectInfo.sessions.length}`);
}

main();
```

## 数据存储结构

```
projects_data/
├── projects.json                    # 项目元数据
└── proj_abc123def456/              # 项目目录
    └── workspace/                   # Claude 工作目录
        └── main.py                  # 生成的文件

~/.claude/projects/                  # Claude 会话存储（自动管理）
└── <sanitized-workspace-path>/
    ├── 550e8400-e29b-41d4-a716-446655440000.jsonl  # 会话1
    └── 660e8400-e29b-41d4-a716-446655440001.jsonl  # 会话2
```

## 关键特性

### 1. 多项目隔离
- 每个项目有独立的 `project_id` 和工作目录
- Claude 会话自动按工作目录隔离

### 2. 会话历史
- 使用 SDK 的 `list_sessions()` 获取项目的所有会话
- 使用 `get_session_messages()` 获取会话的完整历史

### 3. 详细响应信息
- Token 消耗（input/output/cache）
- 成本估算（USD）
- 会话 ID
- 停止原因
- 执行时长
- 对话轮数

### 4. 流式响应
- 实时接收 Claude 的消息
- 支持 SSE（Server-Sent Events）
- 前端可以实时显示生成过程

## 运行服务

```bash
# 安装依赖
pip install fastapi uvicorn

# 运行服务
python server_v2.py

# 或使用 uvicorn
uvicorn server_v2:app --host 0.0.0.0 --port 8000 --reload
```

## 注意事项

1. **Session ID 的生命周期**：每次调用 `/chat` 都会创建新的 session，除非你在请求中指定 `session_id`（但当前实现还未支持继续会话）

2. **工作目录隔离**：不同项目的文件完全隔离，不会互相影响

3. **会话存储位置**：会话文件存储在 `~/.claude/projects/` 下，由 Claude CLI 自动管理

4. **成本计算**：`total_cost_usd` 是 SDK 根据 token 使用量估算的成本

5. **缓存优化**：`cache_read_input_tokens` 显示了 prompt caching 节省的 token 数量
