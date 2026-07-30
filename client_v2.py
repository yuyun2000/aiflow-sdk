"""
UIFlow Code Generator API V2 — 交互式客户端

功能：
  - 多项目管理（创建、列出、切换、删除）
  - 连续对话（同一项目内多轮对话，保留 session 历史）
  - 历史查看（列出会话、查看会话消息）
  - 流式实时输出

用法：
  python client_v2.py                        # 交互式菜单
  python client_v2.py --server http://x:8880 # 指定服务器
"""

import argparse
import json
import sys
import textwrap
from datetime import datetime

import requests

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

DEFAULT_SERVER = "http://192.168.20.38:8880"

# ---------------------------------------------------------------------------
# API 封装
# ---------------------------------------------------------------------------


class APIClient:
    def __init__(self, server: str):
        self.server = server.rstrip("/")
        self.session = requests.Session()

    def _url(self, path: str) -> str:
        return f"{self.server}{path}"

    # ---- 项目 ----

    def create_project(self, name: str, description: str = "") -> dict:
        resp = self.session.post(
            self._url("/projects"),
            json={"name": name, "description": description},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def list_projects(self) -> list[dict]:
        resp = self.session.get(self._url("/projects"), timeout=30)
        resp.raise_for_status()
        return resp.json()

    def get_project(self, project_id: str) -> dict:
        resp = self.session.get(self._url(f"/projects/{project_id}"), timeout=30)
        resp.raise_for_status()
        return resp.json()

    def delete_project(self, project_id: str) -> dict:
        resp = self.session.delete(self._url(f"/projects/{project_id}"), timeout=30)
        resp.raise_for_status()
        return resp.json()

    # ---- 会话历史 ----

    def get_session_messages(
        self, project_id: str, session_id: str, limit: int | None = None
    ) -> dict:
        params = {"offset": 0}
        if limit is not None:
            params["limit"] = limit
        resp = self.session.get(
            self._url(f"/projects/{project_id}/sessions/{session_id}/messages"),
            params=params,
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    # ---- 对话（流式） ----

    def chat_stream(self, project_id: str, prompt: str, session_id: str | None = None):
        """
        生成器，逐条 yield 解析后的 SSE 事件 dict。
        事件类型: message / file / result / error / done
        """
        with self.session.post(
            self._url("/chat"),
            json={"project_id": project_id, "prompt": prompt, "session_id": session_id},
            stream=True,
            timeout=300,
        ) as resp:
            resp.raise_for_status()
            for raw in resp.iter_lines():
                if not raw:
                    continue
                line = raw.decode("utf-8") if isinstance(raw, bytes) else raw
                if not line.startswith("data: "):
                    continue
                event = json.loads(line[6:])
                yield event
                if event.get("type") == "done":
                    break

    def health(self) -> bool:
        try:
            resp = self.session.get(self._url("/health"), timeout=5)
            return resp.status_code == 200
        except Exception:
            return False


# ---------------------------------------------------------------------------
# 显示工具
# ---------------------------------------------------------------------------

DIVIDER = "─" * 60


def _ts(unix_ts: int) -> str:
    try:
        if not unix_ts:
            return "—"
        return datetime.fromtimestamp(unix_ts).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, OverflowError, ValueError):
        return str(unix_ts)


def print_project_table(projects: list[dict]) -> None:
    if not projects:
        print("（暂无项目）")
        return
    print(f"\n{'#':<4} {'项目ID':<24} {'名称':<20} {'最后更新'}")
    print(DIVIDER)
    for i, p in enumerate(projects, 1):
        updated = p["updated_at"][:19].replace("T", " ")
        name = p["name"][:18]
        pid = p["project_id"]
        print(f"{i:<4} {pid:<24} {name:<20} {updated}")
    print()


def print_session_table(sessions: list[dict]) -> None:
    if not sessions:
        print("（该项目暂无会话）")
        return
    print(f"\n{'#':<4} {'会话ID':<40} {'最后修改':<22} 摘要")
    print(DIVIDER)
    for i, s in enumerate(sessions, 1):
        sid = s["session_id"]
        ts = _ts(s["last_modified"])
        summary = s["summary"][:30] if s.get("summary") else "—"
        print(f"{i:<4} {sid:<40} {ts:<22} {summary}")
    print()


def run_chat(api: APIClient, project_id: str, project_name: str) -> None:
    """在指定项目内进行连续对话，直到用户输入 /exit。"""
    print(f"\n进入项目「{project_name}」对话模式")
    print("  输入需求后回车发送")
    print("  输入 /exit 退出对话模式")
    print("  输入 /new 开启新会话")
    print("  输入 /continue <session_id> 继续指定会话")
    print(DIVIDER)

    turn = 1
    current_session_id: str | None = None

    while True:
        session_hint = f"[新会话]" if not current_session_id else f"[会话 {current_session_id[:8]}...]"
        try:
            prompt = input(f"\n[第{turn}轮] {session_hint} 你: ").strip()
        except (KeyboardInterrupt, EOFError):
            print()
            break

        if not prompt:
            continue
        if prompt.lower() in ("/exit", "/quit", "/q"):
            break
        if prompt.lower() == "/new":
            current_session_id = None
            print("已切换到新会话模式")
            continue
        if prompt.lower().startswith("/continue "):
            sid = prompt.split(maxsplit=1)[1].strip()
            current_session_id = sid
            print(f"已切换到会话 {sid[:8]}... 继续对话")
            continue

        print(f"\nClaude: ", end="", flush=True)

        file_data: dict | None = None
        result_data: dict | None = None
        error_msg: str | None = None

        try:
            for event in api.chat_stream(project_id, prompt, session_id=current_session_id):
                etype = event.get("type")
                data = event.get("data") or {}

                if etype == "message":
                    text = data.get("text", "")
                    # 缩进续行，保持阅读整洁
                    lines = text.split("\n")
                    for j, ln in enumerate(lines):
                        if j == 0:
                            print(ln, end="", flush=True)
                        else:
                            print("\n       " + ln, end="", flush=True)

                elif etype == "file":
                    file_data = data

                elif etype == "result":
                    result_data = data
                    # 更新当前会话 ID（如果是新会话，服务端会返回新 ID）
                    returned_sid = data.get("session_id")
                    if returned_sid:
                        current_session_id = returned_sid

                elif etype == "error":
                    error_msg = data.get("message", "未知错误")

                elif etype == "done":
                    break

        except requests.exceptions.Timeout:
            print("\n[超时] 请求超时，请稍后重试")
            continue
        except requests.exceptions.ConnectionError:
            print(f"\n[连接失败] 无法连接到 {api.server}")
            break

        print()  # 消息换行

        # 打印生成文件信息
        if file_data:
            fname = file_data.get("name", "main.py")
            content = file_data.get("content", "")
            print(f"\n[文件] {fname}  ({len(content)} 字符)")
            # 打印前 20 行预览
            lines = content.splitlines()
            preview = lines[:20]
            for ln in preview:
                print("  " + ln)
            if len(lines) > 20:
                print(f"  ... （共 {len(lines)} 行，已截断）")

        # 打印统计信息
        if result_data:
            usage = result_data.get("usage") or {}
            cost = result_data.get("total_cost_usd") or 0
            dur = result_data.get("duration_ms") or 0
            turns = result_data.get("num_turns") or 0
            in_tok = usage.get("input_tokens", 0)
            out_tok = usage.get("output_tokens", 0)
            cache_read = usage.get("cache_read_input_tokens", 0)
            sid_short = (current_session_id or "")[:8]
            print(
                f"\n[统计] session={sid_short}...  "
                f"tokens={in_tok}in/{out_tok}out  "
                f"cache_hit={cache_read}  "
                f"cost=${cost:.4f}  "
                f"耗时={dur}ms  "
                f"turns={turns}"
            )

        if error_msg:
            print(f"\n[错误] {error_msg}")

        turn += 1

    if current_session_id:
        print(f"\n本次会话 ID: {current_session_id}")
        print("（可通过「查看会话历史」回溯对话记录）")


# ---------------------------------------------------------------------------
# 菜单操作
# ---------------------------------------------------------------------------


def cmd_new_project(api: APIClient) -> dict | None:
    name = input("项目名称: ").strip()
    if not name:
        print("[取消]")
        return None
    desc = input("项目描述（回车跳过）: ").strip()
    project = api.create_project(name, desc)
    print(f"\n[已创建] {project['project_id']}  {project['name']}")
    return project


def cmd_select_project(api: APIClient) -> dict | None:
    """列出项目并让用户选择，返回选中的项目 dict，或 None。"""
    projects = api.list_projects()
    print_project_table(projects)
    if not projects:
        return None
    try:
        choice = input("输入编号选择项目（回车取消）: ").strip()
        if not choice:
            return None
        idx = int(choice) - 1
        if 0 <= idx < len(projects):
            return projects[idx]
        print("[超出范围]")
    except ValueError:
        print("[无效输入]")
    return None


def cmd_chat_with_project(api: APIClient, project: dict) -> None:
    run_chat(api, project["project_id"], project["name"])


def cmd_view_sessions(api: APIClient) -> None:
    project = cmd_select_project(api)
    if not project:
        return
    info = api.get_project(project["project_id"])
    sessions = info.get("sessions", [])
    print(f"\n项目「{project['name']}」共 {len(sessions)} 个会话：")
    print_session_table(sessions)
    if not sessions:
        return

    choice = input("输入编号查看会话消息（回车跳过）: ").strip()
    if not choice:
        return
    try:
        idx = int(choice) - 1
        if not (0 <= idx < len(sessions)):
            print("[超出范围]")
            return
    except ValueError:
        print("[无效输入]")
        return

    session = sessions[idx]
    limit_str = input("最多查看多少条消息（回车查看全部）: ").strip()
    limit = int(limit_str) if limit_str.isdigit() else None

    history = api.get_session_messages(
        project["project_id"], session["session_id"], limit=limit
    )
    messages = history.get("messages", [])
    print(f"\n── 会话 {session['session_id'][:8]}... 共 {len(messages)} 条消息 ──\n")

    for msg in messages:
        role = msg.get("type", "?")
        content = msg.get("message", {})
        if role == "user":
            raw = content.get("content", "")
            text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
            wrapped = textwrap.fill(text[:500], width=70, initial_indent="  ", subsequent_indent="  ")
            print(f"[用户]\n{wrapped}")
        else:
            # assistant content 是 list of blocks
            raw = content.get("content", [])
            if isinstance(raw, list):
                for block in raw:
                    if isinstance(block, dict) and block.get("type") == "text":
                        t = block.get("text", "")
                        wrapped = textwrap.fill(t[:500], width=70, initial_indent="  ", subsequent_indent="  ")
                        print(f"[Claude]\n{wrapped}")
            else:
                print(f"[Claude]\n  {str(raw)[:300]}")
        print()


def cmd_delete_project(api: APIClient) -> None:
    project = cmd_select_project(api)
    if not project:
        return
    confirm = input(
        f"确认删除项目「{project['name']}」({project['project_id']})？[y/N]: "
    ).strip()
    if confirm.lower() != "y":
        print("[取消]")
        return
    result = api.delete_project(project["project_id"])
    print(f"[已删除] {result.get('message')}")


# ---------------------------------------------------------------------------
# 快速对话模式（无菜单）
# ---------------------------------------------------------------------------


def quick_chat(api: APIClient, project_id: str, prompt: str) -> None:
    """单次对话，打印结果后退出。用于脚本调用。"""
    print(f"[项目] {project_id}")
    print(f"[请求] {prompt}\n")
    print("Claude: ", end="", flush=True)

    for event in api.chat_stream(project_id, prompt):
        etype = event.get("type")
        data = event.get("data") or {}

        if etype == "message":
            print(data.get("text", ""), end="", flush=True)

        elif etype == "file":
            print(f"\n\n[文件] {data.get('name')}\n")
            print(data.get("content", ""))

        elif etype == "result":
            usage = data.get("usage") or {}
            print(
                f"\n[统计] session={data.get('session_id', '')[:8]}...  "
                f"cost=${data.get('total_cost_usd', 0):.4f}  "
                f"tokens={usage.get('input_tokens', 0)}in/{usage.get('output_tokens', 0)}out"
            )

        elif etype == "error":
            print(f"\n[错误] {data.get('message')}")

        elif etype == "done":
            break
    print()


# ---------------------------------------------------------------------------
# 主菜单
# ---------------------------------------------------------------------------

MENU = """
╔══════════════════════════════════╗
║  UIFlow Code Generator V2 客户端 ║
╚══════════════════════════════════╝
  1. 新建项目
  2. 选择项目并对话
  3. 查看会话历史
  4. 删除项目
  5. 列出所有项目
  0. 退出
"""


def interactive_menu(api: APIClient) -> None:
    if not api.health():
        print(f"[警告] 无法连接服务器 {api.server}，请确认服务已启动。")

    while True:
        print(MENU)
        choice = input("请选择操作: ").strip()

        if choice == "1":
            cmd_new_project(api)

        elif choice == "2":
            project = cmd_select_project(api)
            if project:
                cmd_chat_with_project(api, project)

        elif choice == "3":
            cmd_view_sessions(api)

        elif choice == "4":
            cmd_delete_project(api)

        elif choice == "5":
            projects = api.list_projects()
            print_project_table(projects)

        elif choice == "0":
            print("再见！")
            sys.exit(0)

        else:
            print("[无效选项]")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="UIFlow Code Generator V2 客户端")
    parser.add_argument(
        "--server",
        default=DEFAULT_SERVER,
        help=f"服务器地址（默认：{DEFAULT_SERVER}）",
    )
    parser.add_argument(
        "--project",
        metavar="PROJECT_ID",
        help="直接指定项目 ID，配合 --prompt 使用（跳过交互菜单）",
    )
    parser.add_argument(
        "--prompt",
        help="直接发送请求（配合 --project 使用）",
    )
    args = parser.parse_args()

    api = APIClient(args.server)

    if args.project and args.prompt:
        # 脚本模式：直接发一条请求
        quick_chat(api, args.project, args.prompt)
    else:
        # 交互式菜单
        interactive_menu(api)


if __name__ == "__main__":
    main()
