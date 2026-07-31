"""
UIFlow Code Generator API Server - V2
支持多项目管理、会话历史、详细响应信息
"""

import asyncio
import json
import logging
import os
import shutil
import signal
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import AsyncGenerator, Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    ToolResultBlock,
    list_sessions,
    get_session_messages,
)
from claude_agent_sdk._errors import (
    CLINotFoundError,
    CLIConnectionError,
    ProcessError,
    ClaudeSDKError,
)

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(
    title="UIFlow Code Generator API V2",
    version="2.0.0",
    description="多项目支持的 UIFlow 代码生成服务",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# 项目存储配置
# ---------------------------------------------------------------------------

# 项目数据存储目录
PROJECTS_BASE_DIR = Path("./projects_data")
PROJECTS_BASE_DIR.mkdir(exist_ok=True)

# 项目元数据文件
PROJECTS_METADATA_FILE = PROJECTS_BASE_DIR / "projects.json"

# 对话日志目录（独立于项目目录，删除项目时不受影响）
LOGS_DIR = PROJECTS_BASE_DIR / "logs"
LOGS_DIR.mkdir(exist_ok=True)

# ---------------------------------------------------------------------------
# 错误码映射与 SSE 辅助函数
# ---------------------------------------------------------------------------

_ERROR_CODE_MESSAGES: dict[str, str] = {
    "authentication_failed": "API 认证失败，请检查 API Key 配置",
    "billing_error": "账户计费异常，请检查 Anthropic 账户余额",
    "rate_limit": "请求频率超限，请稍后重试",
    "invalid_request": "请求参数无效",
    "server_error": "Anthropic 服务端错误",
    "unknown": "未知模型错误",
}


def _make_error_sse(
    error_code: str,
    message: str,
    category: str,
    retryable: bool,
    **extra: Any,
) -> str:
    event = ChatStreamEvent(
        type="error",
        data={
            "error_code": error_code,
            "message": message,
            "category": category,
            "retryable": retryable,
            **extra,
        },
    )
    return f"data: {event.model_dump_json(ensure_ascii=False)}\n\n"


def _make_done_sse() -> str:
    event = ChatStreamEvent(type="done", data=None)
    return f"data: {event.model_dump_json(ensure_ascii=False)}\n\n"


# 活跃任务跟踪：task_id -> ClaudeSDKClient
_active_tasks: dict[str, ClaudeSDKClient] = {}
# 正在使用中的 session_id 集合
_active_sessions: set[str] = set()
# session_id -> CLI 进程 PID（用于清理孤儿进程）
_session_pids: dict[str, int] = {}

PIDS_FILE = PROJECTS_BASE_DIR / "active_pids.json"


def _save_pids() -> None:
    with open(PIDS_FILE, "w") as f:
        json.dump(_session_pids, f)


def _kill_pid(pid: int) -> bool:
    """尝试终止指定 PID 的进程，返回是否成功"""
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def _cleanup_stale_pids() -> None:
    """服务启动时清理上次残留的孤儿进程"""
    # 1. 通过 PID 文件清理已知进程
    if PIDS_FILE.exists():
        try:
            with open(PIDS_FILE, "r") as f:
                stale_pids: dict[str, int] = json.load(f)
            for session_id, pid in stale_pids.items():
                _kill_pid(pid)
                logging.info(f"Killed stale CLI process pid={pid} for session={session_id}")
        except Exception:
            pass
        PIDS_FILE.unlink(missing_ok=True)

    # 2. 兜底：杀掉所有残留的 claude CLI 交互进程
    import subprocess
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/IM", "claude.exe"],
                capture_output=True, timeout=5,
            )
        else:
            subprocess.run(
                ["pkill", "-f", "claude.*--print-session-id"],
                capture_output=True, timeout=5,
            )
    except Exception:
        pass


_cleanup_stale_pids()


def _append_chat_log(
    project_id: str,
    project_name: str,
    task_id: str,
    prompt: str,
    *,
    session_id: str | None = None,
    result_info: dict[str, Any] | None = None,
    actions: list[dict[str, Any]] | None = None,
    generated_file: str | None = None,
    aborted: bool = False,
    error: str | None = None,
) -> None:
    """追加一条对话日志到 JSONL 文件"""
    record: dict[str, Any] = {
        "timestamp": datetime.now().isoformat(),
        "project_id": project_id,
        "project_name": project_name,
        "task_id": task_id,
        "prompt": prompt,
        "aborted": aborted,
    }
    if result_info:
        record["session_id"] = result_info.get("session_id")
        record["duration_ms"] = result_info.get("duration_ms")
        record["num_turns"] = result_info.get("num_turns")
        record["usage"] = result_info.get("usage")
        record["total_cost_usd"] = result_info.get("total_cost_usd")
        record["stop_reason"] = result_info.get("stop_reason")
        record["is_error"] = result_info.get("is_error", False)
    elif session_id:
        record["session_id"] = session_id
    if error:
        record["error"] = error
    if actions:
        record["actions"] = actions
    if generated_file:
        record["generated_file"] = generated_file

    log_file = LOGS_DIR / f"{project_id}.jsonl"
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

class ProjectMetadata(BaseModel):
    """项目元数据"""
    project_id: str
    name: str
    description: str = ""
    created_at: str
    updated_at: str
    working_directory: str  # Claude 使用的工作目录


class CreateProjectRequest(BaseModel):
    """创建项目请求"""
    name: str
    description: str = ""


class ChatRequest(BaseModel):
    """对话请求"""
    project_id: str
    prompt: str
    session_id: str | None = None  # 可选：继续特定会话


class ChatStreamEvent(BaseModel):
    """流式响应事件"""
    type: str  # "task_start" | "message" | "file" | "result" | "error" | "aborted" | "done"
    data: dict[str, Any] | None = None


class AbortRequest(BaseModel):
    """中断请求"""
    task_id: str


class SessionInfo(BaseModel):
    """会话信息"""
    session_id: str
    summary: str
    last_modified: int
    file_size: int | None = None


class ProjectResponse(BaseModel):
    """项目响应"""
    project: ProjectMetadata
    sessions: list[SessionInfo]


# ---------------------------------------------------------------------------
# 项目管理
# ---------------------------------------------------------------------------

def _load_projects() -> dict[str, ProjectMetadata]:
    """加载所有项目元数据"""
    if not PROJECTS_METADATA_FILE.exists():
        return {}

    with open(PROJECTS_METADATA_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
        return {pid: ProjectMetadata(**meta) for pid, meta in data.items()}


def _save_projects(projects: dict[str, ProjectMetadata]) -> None:
    """保存项目元数据"""
    data = {pid: meta.model_dump() for pid, meta in projects.items()}
    with open(PROJECTS_METADATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


_projects_lock = asyncio.Lock()


def _get_project_working_dir(project_id: str) -> Path:
    """获取项目的工作目录"""
    return PROJECTS_BASE_DIR / project_id / "workspace"


# ---------------------------------------------------------------------------
# API 端点
# ---------------------------------------------------------------------------

@app.post("/projects", response_model=ProjectMetadata)
async def create_project(req: CreateProjectRequest):
    """创建新项目"""
    async with _projects_lock:
        projects = _load_projects()

        # 生成项目 ID
        project_id = f"proj_{uuid.uuid4().hex[:12]}"

        # 创建工作目录
        work_dir = _get_project_working_dir(project_id)
        work_dir.mkdir(parents=True, exist_ok=True)

        # 创建项目元数据
        now = datetime.now().isoformat()
        metadata = ProjectMetadata(
            project_id=project_id,
            name=req.name,
            description=req.description,
            created_at=now,
            updated_at=now,
            working_directory=str(work_dir.absolute()),
        )

        projects[project_id] = metadata
        _save_projects(projects)

    return metadata


@app.get("/projects", response_model=list[ProjectMetadata])
async def list_projects():
    """列出所有项目"""
    projects = _load_projects()
    return list(projects.values())


@app.get("/projects/{project_id}", response_model=ProjectResponse)
async def get_project(project_id: str):
    """获取项目详情和会话列表"""
    projects = _load_projects()

    if project_id not in projects:
        raise HTTPException(status_code=404, detail="项目不存在")

    project = projects[project_id]
    work_dir = _get_project_working_dir(project_id)

    # 获取该项目的所有会话
    sessions = list_sessions(directory=str(work_dir))

    session_infos = [
        SessionInfo(
            session_id=s.session_id,
            summary=s.summary,
            last_modified=s.last_modified,
            file_size=s.file_size,
        )
        for s in sessions
    ]

    return ProjectResponse(project=project, sessions=session_infos)


@app.get("/projects/{project_id}/sessions/{session_id}/messages")
async def get_session_history(
    project_id: str,
    session_id: str,
    limit: int | None = None,
    offset: int = 0,
):
    """获取会话历史消息"""
    projects = _load_projects()

    if project_id not in projects:
        raise HTTPException(status_code=404, detail="项目不存在")

    work_dir = _get_project_working_dir(project_id)
    messages = get_session_messages(
        session_id=session_id,
        directory=str(work_dir),
        limit=limit,
        offset=offset,
    )

    return {
        "session_id": session_id,
        "messages": [
            {
                "type": msg.type,
                "uuid": msg.uuid,
                "message": msg.message,
            }
            for msg in messages
        ],
    }


@app.delete("/projects/{project_id}")
async def delete_project(project_id: str):
    """删除项目"""
    async with _projects_lock:
        projects = _load_projects()

        if project_id not in projects:
            raise HTTPException(status_code=404, detail="项目不存在")

        # 删除项目目录（含 workspace 和 .claude 等所有内容）
        project_dir = PROJECTS_BASE_DIR / project_id
        if project_dir.exists():
            shutil.rmtree(project_dir)

        # 删除元数据
        del projects[project_id]
        _save_projects(projects)

    return {"message": "项目已删除"}


@app.post("/chat")
async def chat(req: ChatRequest):
    """
    与 Claude 对话（流式响应）

    返回 SSE 流，包含：
    - message 事件：Claude 的文本消息
    - file 事件：生成的文件内容
    - result 事件：最终结果（包含 token 消耗、模型信息等）
    - error 事件：错误信息
    - done 事件：流结束
    """
    # 检查该 session 是否正在被使用
    if req.session_id:
        if req.session_id in _active_sessions:
            raise HTTPException(
                status_code=409,
                detail=f"会话 {req.session_id} 正在使用中，请等待当前对话结束或中断后再试",
            )

    async with _projects_lock:
        projects = _load_projects()

        if req.project_id not in projects:
            raise HTTPException(status_code=404, detail="项目不存在")

        project = projects[req.project_id]
        work_dir = Path(project.working_directory)

        # 更新项目的最后修改时间
        project.updated_at = datetime.now().isoformat()
        projects[req.project_id] = project
        _save_projects(projects)

    async def generate() -> AsyncGenerator[str, None]:
        task_id = f"task_{uuid.uuid4().hex[:12]}"
        result_info: dict[str, Any] | None = None
        log_aborted = False
        log_error: str | None = None
        actions: list[dict[str, Any]] = []
        generated_file: str | None = None

        # 发送 task_start 事件
        event = ChatStreamEvent(type="task_start", data={"task_id": task_id})
        yield f"data: {event.model_dump_json(ensure_ascii=False)}\n\n"

        try:
            options = ClaudeAgentOptions(
                cwd=str(work_dir),
                allowed_tools=["Read", "Write", "Bash", "Skill"],
                permission_mode="acceptEdits",
                resume=req.session_id,
            )

            full_prompt = (
            f"用户需求如下：{req.prompt}\n\n请将生成的代码写入 main.py 文件。"
            f"要求：\n"
            f"- 文件名必须是 main.py\n"
            f"- 代码必须完整、可直接运行\n"
            f"- 使用 Write 工具写入文件，不要只打印代码\n"
            f"【重要规则】：\n"
            f"1. 产品型号：严格使用我提供的产品名称，不要自行修改或理解\n"
            f"2. 硬件参数：禁止凭经验编造！所有引脚、接口、规格必须查询官方文档\n"
            f"3. 验证结果：查询后检查返回的产品名称是否与需求一致，不匹配则重新查询\n"
            f"4. 工作流程：先查询硬件规格 (m5stack-assistant) → 再生成代码 (uiflow2-coder)\n"
            f"5. 自我检查：生成代码前确认所有参数都有文档支持，没有猜测成分\n"
            )

            async with ClaudeSDKClient(options=options) as client:
                _active_tasks[task_id] = client
                if req.session_id:
                    _active_sessions.add(req.session_id)

                # 记录 CLI 进程 PID，用于孤儿进程清理
                try:
                    pid = client._transport._process.pid
                    if pid and req.session_id:
                        _session_pids[req.session_id] = pid
                        _save_pids()
                except (AttributeError, TypeError):
                    pass

                await client.query(full_prompt)

                async for message in client.receive_response():
                    if isinstance(message, AssistantMessage):
                        if message.error:
                            error_event = ChatStreamEvent(
                                type="error",
                                data={
                                    "error_code": message.error,
                                    "message": _ERROR_CODE_MESSAGES.get(
                                        message.error, f"模型错误: {message.error}"
                                    ),
                                    "category": "model",
                                    "retryable": message.error == "rate_limit",
                                }
                            )
                            yield f"data: {error_event.model_dump_json(ensure_ascii=False)}\n\n"

                        for block in message.content:
                            if isinstance(block, TextBlock):
                                actions.append({"type": "text", "text": block.text})

                                event = ChatStreamEvent(
                                    type="message",
                                    data={"text": block.text}
                                )
                                yield f"data: {event.model_dump_json(ensure_ascii=False)}\n\n"

                            elif isinstance(block, ToolUseBlock):
                                actions.append({
                                    "type": "tool_use",
                                    "tool": block.name,
                                    "input": block.input,
                                })

                            elif isinstance(block, ToolResultBlock):
                                action: dict[str, Any] = {
                                    "type": "tool_result",
                                    "tool_use_id": block.tool_use_id,
                                }
                                if block.is_error:
                                    action["is_error"] = True
                                if isinstance(block.content, str):
                                    action["content"] = block.content[:500]
                                actions.append(action)

                    elif isinstance(message, ResultMessage):
                        usage = message.usage or {}
                        result_info = {
                            "session_id": message.session_id,
                            "total_cost_usd": message.total_cost_usd,
                            "usage": usage,
                            "stop_reason": message.stop_reason,
                            "duration_ms": message.duration_ms,
                            "num_turns": message.num_turns,
                            "is_error": message.is_error,
                        }

                        if message.is_error:
                            error_event = ChatStreamEvent(
                                type="error",
                                data={
                                    "error_code": "result_error",
                                    "message": "; ".join(message.errors) if message.errors else "对话异常终止",
                                    "category": "model",
                                    "retryable": False,
                                    "session_id": message.session_id,
                                }
                            )
                            yield f"data: {error_event.model_dump_json(ensure_ascii=False)}\n\n"

                # 响应结束后立即断开，确保 CLI 进程退出并释放 session 锁
                await client.disconnect()

            # 检查是否被中断（abort 会从 _active_tasks 中移除 task_id）
            if task_id not in _active_tasks:
                log_aborted = True
                abort_data: dict[str, Any] = {"task_id": task_id}
                if result_info:
                    abort_data["usage"] = result_info.get("usage")
                    abort_data["total_cost_usd"] = result_info.get("total_cost_usd")
                    abort_data["session_id"] = result_info.get("session_id")
                event = ChatStreamEvent(type="aborted", data=abort_data)
                yield f"data: {event.model_dump_json(ensure_ascii=False)}\n\n"
                yield _make_done_sse()
                return

            # 读取生成的文件
            main_py_path = work_dir / "main.py"
            if main_py_path.exists():
                with open(main_py_path, "r", encoding="utf-8") as f:
                    file_content = f.read()

                generated_file = file_content

                event = ChatStreamEvent(
                    type="file",
                    data={
                        "name": "main.py",
                        "content": generated_file,
                    }
                )
                yield f"data: {event.model_dump_json(ensure_ascii=False)}\n\n"

            if result_info:
                event = ChatStreamEvent(type="result", data=result_info)
                yield f"data: {event.model_dump_json(ensure_ascii=False)}\n\n"

            event = ChatStreamEvent(type="done", data=None)
            yield f"data: {event.model_dump_json(ensure_ascii=False)}\n\n"

        except CLINotFoundError as exc:
            log_error = str(exc)
            yield _make_error_sse("cli_not_found", str(exc), "environment", False)
            yield _make_done_sse()

        except CLIConnectionError as exc:
            if task_id not in _active_tasks:
                log_aborted = True
                abort_data: dict[str, Any] = {"task_id": task_id}
                if result_info:
                    abort_data["usage"] = result_info.get("usage")
                    abort_data["total_cost_usd"] = result_info.get("total_cost_usd")
                    abort_data["session_id"] = result_info.get("session_id")
                event = ChatStreamEvent(type="aborted", data=abort_data)
                yield f"data: {event.model_dump_json(ensure_ascii=False)}\n\n"
                yield _make_done_sse()
            else:
                log_error = str(exc)
                yield _make_error_sse("cli_connection_error", str(exc), "environment", True)
                yield _make_done_sse()

        except ProcessError as exc:
            if task_id not in _active_tasks:
                log_aborted = True
                abort_data = {"task_id": task_id}
                if result_info:
                    abort_data["usage"] = result_info.get("usage")
                    abort_data["total_cost_usd"] = result_info.get("total_cost_usd")
                    abort_data["session_id"] = result_info.get("session_id")
                event = ChatStreamEvent(type="aborted", data=abort_data)
                yield f"data: {event.model_dump_json(ensure_ascii=False)}\n\n"
                yield _make_done_sse()
            else:
                log_error = str(exc)
                yield _make_error_sse(
                    "process_error", str(exc), "environment", False,
                    exit_code=exc.exit_code,
                )
                yield _make_done_sse()

        except ClaudeSDKError as exc:
            log_error = str(exc)
            yield _make_error_sse("sdk_error", str(exc), "sdk", False)
            yield _make_done_sse()

        except Exception as exc:
            log_error = str(exc)
            yield _make_error_sse("internal_error", str(exc), "server", False)
            yield _make_done_sse()

        finally:
            _active_tasks.pop(task_id, None)
            if req.session_id:
                _active_sessions.discard(req.session_id)
                _session_pids.pop(req.session_id, None)
                _save_pids()
            _append_chat_log(
                project_id=req.project_id,
                project_name=project.name,
                task_id=task_id,
                prompt=req.prompt,
                session_id=req.session_id,
                result_info=result_info,
                actions=actions,
                generated_file=generated_file,
                aborted=log_aborted,
                error=log_error,
            )

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/chat/abort")
async def abort_chat(req: AbortRequest):
    """中断正在进行的对话任务"""
    client = _active_tasks.pop(req.task_id, None)
    if client is None:
        raise HTTPException(status_code=404, detail="任务不存在或已结束")

    await client.disconnect()
    return {"message": "已请求中断", "task_id": req.task_id}


class UnlockRequest(BaseModel):
    """解锁会话请求"""
    session_id: str


@app.post("/chat/unlock")
async def unlock_session(req: UnlockRequest):
    """
    强制解锁被占用的会话。

    用于处理孤儿进程导致的 "Session ID is already in use" 错误。
    会终止持有该 session 的 CLI 进程。
    """
    # 1. 如果有活跃任务在用这个 session，先断开
    for task_id, client in list(_active_tasks.items()):
        try:
            opts = getattr(client, '_options', None)
            if opts and getattr(opts, 'session_id', None) == req.session_id:
                _active_tasks.pop(task_id, None)
                await client.disconnect()
                _active_sessions.discard(req.session_id)
                _session_pids.pop(req.session_id, None)
                _save_pids()
                return {"message": "已断开活跃任务并释放会话", "session_id": req.session_id}
        except Exception:
            pass

    # 2. 尝试通过记录的 PID 杀死孤儿进程
    pid = _session_pids.pop(req.session_id, None)
    if pid and _kill_pid(pid):
        _save_pids()
        _active_sessions.discard(req.session_id)
        return {"message": f"已终止孤儿进程 (pid={pid})，会话已释放", "session_id": req.session_id}

    # 3. PID 不存在或已死，尝试从持久化文件恢复
    if PIDS_FILE.exists():
        try:
            with open(PIDS_FILE, "r") as f:
                saved_pids = json.load(f)
            pid = saved_pids.pop(req.session_id, None)
            if pid:
                _kill_pid(pid)
                with open(PIDS_FILE, "w") as f:
                    json.dump(saved_pids, f)
                _active_sessions.discard(req.session_id)
                return {"message": f"已终止残留进程 (pid={pid})，会话已释放", "session_id": req.session_id}
        except Exception:
            pass

    # 4. 都找不到，清理内存状态
    _active_sessions.discard(req.session_id)
    return {
        "message": "未找到持有该会话的进程，已清理内部状态。如仍报错，可能需要手动终止 claude 进程",
        "session_id": req.session_id,
    }


@app.get("/health")
async def health_check():
    """健康检查"""
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "server_v2:app",
        host="0.0.0.0",
        port=8880,
        reload=True,
        reload_excludes=["projects_data/*"],
    )
