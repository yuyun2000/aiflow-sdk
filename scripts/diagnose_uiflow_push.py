#!/usr/bin/env python3
"""Run connectivity diagnostics for the UIFlow push API.

The default mode is read-only and never sends a POST or uploads code/resources.
The explicit ``--execute`` mode sends one diagnostic code POST to a named test
device. Run it on the same host that normally runs aiflow_push.py so the
network path matches the failed deployment. HTTP 4xx/5xx responses are
retained as evidence: they still prove that the remote HTTP service answered.
"""

from __future__ import annotations

import argparse
import json
import socket
import ssl
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from http.client import HTTPException, HTTPResponse
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit, urlunsplit
from urllib.request import Request, urlopen

DEFAULT_BASE_URL = "https://uiflow2.m5stack.com/m5stack/"
DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_DEVICE_ID = "__diagnostic_invalid__"
DIAGNOSTIC_CODE = '# AIFlow connectivity diagnostic\nprint("aiflow diagnostic ok")\n'
MAX_BODY_BYTES = 512
MAX_CODE_BYTES = 64 * 1024
ROLE_LABELS = {"client": "客户端", "uiflow": "UIFlow 服务端"}
PROBE_LABELS = {
    "dns": "DNS 解析",
    "tcp": "TCP 连接",
    "tls": "TLS 握手",
    "http_get_base": "HTTP 基础路径 GET",
    "http_options_push_code": "推送路由 OPTIONS",
    "http_get_push_code": "推送路由 GET（方法探测）",
    "http_post_push_code": "推送路由 POST（真实验证）",
}
STATUS_LABELS = {"pass": "通过", "resp": "已响应", "fail": "失败"}


@dataclass
class Probe:
    name: str
    kind: str
    ok: bool
    elapsed_ms: float
    status: int | None = None
    application_code: int | None = None
    detail: str = ""


def mask(value: str) -> str:
    if len(value) <= 4:
        return "****"
    return value[:2] + "***" + value[-2:]


def normalize_base_url(value: str) -> tuple[str, str, int]:
    parts = urlsplit(value.strip())
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError("base URL must be an absolute http(s) URL")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("base URL must not contain credentials, query, or fragment")
    host = parts.hostname
    if not host:
        raise ValueError("base URL hostname is empty")
    default_port = 443 if parts.scheme == "https" else 80
    port = parts.port or default_port
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme, parts.netloc, path, "", "")), host, port


def endpoint_url(base_url: str, path: str) -> str:
    return base_url.rstrip("/") + "/" + path.lstrip("/")


def elapsed(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 1)


def probe_dns(host: str, port: int, timeout: float) -> Probe:
    start = time.perf_counter()
    try:
        previous = socket.getdefaulttimeout()
        socket.setdefaulttimeout(timeout)
        try:
            records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        finally:
            socket.setdefaulttimeout(previous)
        addresses = sorted({record[4][0] for record in records})
        detail = "addresses=" + ",".join(addresses) if addresses else "no addresses"
        return Probe("dns", "network", bool(addresses), elapsed(start), detail=detail)
    except (OSError, ValueError) as exc:  # pragma: no cover - resolver errors vary
        return Probe("dns", "network", False, elapsed(start), detail=f"{type(exc).__name__}: {exc}")


def probe_tcp(host: str, port: int, timeout: float) -> Probe:
    start = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return Probe("tcp", "network", True, elapsed(start), detail=f"{host}:{port}")
    except OSError as exc:
        return Probe("tcp", "network", False, elapsed(start), detail=f"{type(exc).__name__}: {exc}")


def probe_tls(host: str, port: int, timeout: float) -> Probe:
    start = time.perf_counter()
    context = ssl.create_default_context()
    try:
        with socket.create_connection((host, port), timeout=timeout) as raw, context.wrap_socket(
            raw, server_hostname=host
        ) as secure:
            version = secure.version() or "unknown"
        return Probe("tls", "network", True, elapsed(start), detail=f"version={version}")
    except (OSError, ValueError) as exc:
        return Probe("tls", "network", False, elapsed(start), detail=f"{type(exc).__name__}: {exc}")


def read_body(response: HTTPResponse) -> str:
    raw = response.read(MAX_BODY_BYTES)
    return raw.decode("utf-8", errors="replace").replace("\x00", "")


def read_error_body(error: HTTPError) -> str:
    try:
        return error.read(MAX_BODY_BYTES).decode("utf-8", errors="replace")
    except (HTTPException, OSError, TimeoutError):
        return ""


def body_detail(body: str, redactions: tuple[str, ...] = ()) -> str:
    compact = " ".join(body.split())[:240]
    for value in redactions:
        if value:
            compact = compact.replace(value, mask(value))
    return compact


def application_code(body: str) -> int | None:
    try:
        payload = json.loads(body)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    value = payload.get("code")
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def probe_http(
    name: str,
    url: str,
    method: str,
    timeout: float,
    *,
    data: bytes | None = None,
    content_type: str | None = None,
    redactions: tuple[str, ...] = (),
) -> Probe:
    start = time.perf_counter()
    headers = {"User-Agent": "aiflow-uiflow-diagnostic/1", "Accept": "*/*"}
    if content_type:
        headers["Content-Type"] = content_type
    request = Request(
        url,
        data=data,
        method=method,
        headers=headers,
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            body = read_body(response)
            status = int(response.status)
            app_code = application_code(body)
            detail = f"http={status}"
            if app_code is not None:
                detail += f" app_code={app_code}"
            if body:
                detail += " body=" + body_detail(body, redactions)
            return Probe(
                name,
                "http",
                True,
                elapsed(start),
                status=status,
                application_code=app_code,
                detail=detail,
            )
    except HTTPError as exc:
        body = read_error_body(exc)
        app_code = application_code(body)
        detail = f"http={exc.code}"
        if app_code is not None:
            detail += f" app_code={app_code}"
        if body:
            detail += " body=" + body_detail(body, redactions)
        return Probe(
            name,
            "http",
            True,
            elapsed(start),
            status=int(exc.code),
            application_code=app_code,
            detail=detail,
        )
    except TimeoutError as exc:
        return Probe(name, "http", False, elapsed(start), detail=f"timeout: {exc or 'timed out'}")
    except URLError as exc:
        reason = exc.reason
        return Probe(name, "http", False, elapsed(start), detail=f"{type(reason).__name__}: {reason}")
    except HTTPException as exc:
        return Probe(name, "http", False, elapsed(start), detail=f"{type(exc).__name__}: {exc}")
    except (OSError, ValueError) as exc:
        return Probe(name, "http", False, elapsed(start), detail=f"{type(exc).__name__}: {exc}")


def make_conclusion(code: str, language: str = "zh", **values: object) -> dict[str, str]:
    messages = {
        "CLIENT_NETWORK_FAILURE": {
            "zh": "当前测试机的 DNS、TCP 或 TLS 失败，尚未建立可靠的 HTTP 通信。",
            "en": "DNS, TCP, or TLS failed on the test host before a reliable HTTP request.",
        },
        "UIFLOW_BASE_GATEWAY_ERROR": {
            "zh": "UIFlow 基础路径返回 HTTP {status}，说明网关到该路径的上游存在异常；本次尚未验证推送 POST。",
            "en": "The UIFlow base path returned HTTP {status}; its gateway/upstream is abnormal, but the push POST was not tested.",
        },
        "ROUTE_REACHABLE_POST_UNTESTED": {
            "zh": "只读 HTTP 路由可达；本次没有发送 POST，不能证明设备推送成功或失败。",
            "en": "Read-only HTTP routes answered; no POST was sent, so device push remains unproven.",
        },
        "UIFLOW_POST_TIMEOUT": {
            "zh": "推送 POST 在超时时间内没有收到完整 HTTP 响应。",
            "en": "The push POST received no complete HTTP response before the client timeout.",
        },
        "CLIENT_POST_TRANSPORT_FAILURE": {
            "zh": "推送 POST 在当前测试机的传输层失败，没有收到 HTTP 状态码。",
            "en": "The push POST failed at the test host's transport layer before an HTTP status was received.",
        },
        "POST_ACCEPTED": {
            "zh": "UIFlow 接受了推送 POST；这只证明服务端接收，不代表设备已执行或返回 ACK。",
            "en": "UIFlow accepted the push POST; device execution or ACK is still unproven.",
        },
        "DEVICE_OFFLINE_REPORTED": {
            "zh": "UIFlow 已进入业务逻辑，并明确返回设备离线。",
            "en": "UIFlow reached business logic and reported that the device is offline.",
        },
        "UIFLOW_SERVER_ERROR": {
            "zh": "UIFlow 对推送 POST 返回服务端错误（HTTP {status}，应用码 {app_code}）。",
            "en": "UIFlow returned a server error for the push POST (HTTP {status}, application code {app_code}).",
        },
        "UIFLOW_REJECTED_REQUEST": {
            "zh": "UIFlow 拒绝了推送请求（HTTP {status}，应用码 {app_code}），请检查设备 ID 和请求契约。",
            "en": "UIFlow rejected the push request (HTTP {status}, application code {app_code}); inspect the device ID and request contract.",
        },
    }
    next_steps = {
        "CLIENT_NETWORK_FAILURE": {
            "zh": "下一步：检查当前测试机的 DNS、出口网络、防火墙和代理。",
            "en": "Next: inspect DNS, egress networking, firewall, and proxy settings on the test host.",
        },
        "UIFLOW_BASE_GATEWAY_ERROR": {
            "zh": "下一步：请 UIFlow 运维检查 Nginx error.log、上游服务健康状态和网关转发配置；再做一次受控 POST。",
            "en": "Next: have UIFlow operations inspect Nginx error.log, upstream health, and gateway routing, then run one controlled POST.",
        },
        "ROUTE_REACHABLE_POST_UNTESTED": {
            "zh": "下一步：如需判断真实推送，再使用测试设备执行一次 --execute。",
            "en": "Next: use one --execute run with a test device if the real push must be verified.",
        },
        "UIFLOW_POST_TIMEOUT": {
            "zh": "下一步：让客户端和 UIFlow 服务端同时执行一次；两边都超时则优先查 UIFlow 网关和上游日志。",
            "en": "Next: run once from both client and UIFlow host; if both time out, inspect UIFlow gateway and upstream logs first.",
        },
        "CLIENT_POST_TRANSPORT_FAILURE": {
            "zh": "下一步：检查当前测试机的出口网络、代理和 TLS 中间设备。",
            "en": "Next: inspect egress networking, proxy, and TLS middleboxes on the test host.",
        },
        "POST_ACCEPTED": {
            "zh": "下一步：检查设备屏幕/串口或设备 ACK，确认代码是否真正执行。",
            "en": "Next: inspect the device display/serial output or ACK to confirm execution.",
        },
        "DEVICE_OFFLINE_REPORTED": {
            "zh": "下一步：检查设备供电、联网、绑定关系和 UIFlow 在线状态。",
            "en": "Next: inspect device power, connectivity, binding, and UIFlow online state.",
        },
        "UIFLOW_SERVER_ERROR": {
            "zh": "下一步：把本次输出、时间戳和设备端脱敏标识交给 UIFlow 运维查服务端日志。",
            "en": "Next: provide this output, timestamp, and redacted device identifier to UIFlow operations for server-log correlation.",
        },
        "UIFLOW_REJECTED_REQUEST": {
            "zh": "下一步：检查设备 ID、Content-Type、代码内容和接口版本。",
            "en": "Next: inspect the device ID, Content-Type, code body, and API version.",
        },
    }
    selected_language = language if language in {"zh", "en"} else "zh"
    format_values = {"status": "未知", "app_code": "无", **values}
    return {
        "code": code,
        "message": messages[code][selected_language].format(**format_values),
        "next_step": next_steps[code][selected_language],
    }


def classify_report(report: dict[str, Any], language: str = "zh") -> dict[str, str]:
    probes = report["probes"]
    network_failures = [
        item for item in probes if item["kind"] == "network" and not item["ok"]
    ]
    if network_failures:
        return make_conclusion("CLIENT_NETWORK_FAILURE", language)

    post = next((item for item in probes if item["name"] == "http_post_push_code"), None)
    if post is None:
        base_statuses = [
            int(item["status"])
            for item in probes
            if item["name"] == "http_get_base" and item["status"] is not None
        ]
        gateway_status = next((status for status in base_statuses if status in {502, 503, 504}), None)
        if gateway_status is not None:
            return make_conclusion("UIFLOW_BASE_GATEWAY_ERROR", language, status=gateway_status)
        return make_conclusion("ROUTE_REACHABLE_POST_UNTESTED", language)

    if not post["ok"]:
        if "timeout" in post["detail"].lower():
            return make_conclusion("UIFLOW_POST_TIMEOUT", language)
        return make_conclusion("CLIENT_POST_TRANSPORT_FAILURE", language)

    status = int(post["status"] or 0)
    app_code = int(post.get("application_code") or 0)
    detail = post["detail"].lower()
    if app_code == 409 and "offline" in detail:
        return make_conclusion("DEVICE_OFFLINE_REPORTED", language)
    if app_code >= 500:
        return make_conclusion("UIFLOW_SERVER_ERROR", language, status=status, app_code=app_code)
    if app_code >= 400:
        return make_conclusion("UIFLOW_REJECTED_REQUEST", language, status=status, app_code=app_code)
    if 200 <= status < 300:
        return make_conclusion("POST_ACCEPTED", language, status=status, app_code=app_code)
    if status == 409 and "offline" in detail:
        return make_conclusion("DEVICE_OFFLINE_REPORTED", language, status=status, app_code=app_code)
    if status >= 500:
        return make_conclusion("UIFLOW_SERVER_ERROR", language, status=status, app_code=app_code)
    return make_conclusion("UIFLOW_REJECTED_REQUEST", language, status=status, app_code=app_code)


def load_code(code_file: str | None) -> bytes:
    if not code_file:
        return DIAGNOSTIC_CODE.encode("utf-8")
    path = Path(code_file).expanduser()
    if not path.is_file():
        raise ValueError("--code-file must point to a regular file")
    try:
        data = path.read_bytes()
        data.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError("--code-file must be readable UTF-8 text") from exc
    if not data.strip():
        raise ValueError("--code-file must not be empty or whitespace-only")
    if len(data) > MAX_CODE_BYTES:
        raise ValueError("--code-file must be at most 64 KiB")
    return data


def run_once(
    base_url: str,
    host: str,
    port: int,
    device_id: str,
    timeout: float,
    *,
    role: str,
    execute: bool,
    code: bytes,
    language: str = "zh",
) -> dict[str, Any]:
    probes = [probe_dns(host, port, timeout), probe_tcp(host, port, timeout)]
    if urlsplit(base_url).scheme == "https":
        probes.append(probe_tls(host, port, timeout))

    push_path = "/api/v1/device/push-code/" + quote(device_id, safe="")
    probes.extend(
        [
            probe_http("http_get_base", base_url, "GET", timeout),
            probe_http("http_options_push_code", endpoint_url(base_url, push_path), "OPTIONS", timeout),
            probe_http("http_get_push_code", endpoint_url(base_url, push_path), "GET", timeout),
        ]
    )
    if execute:
        probes.append(
            probe_http(
                "http_post_push_code",
                endpoint_url(base_url, push_path),
                "POST",
                timeout,
                data=code,
                content_type="text/plain; charset=UTF-8",
                redactions=(device_id,),
            )
        )
    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "role": role,
        "base_url": base_url,
        "host": host,
        "port": port,
        "device_id": mask(device_id),
        "read_only": not execute,
        "post_requested": execute,
        "code_bytes": len(code) if execute else None,
        "language": language,
        "probes": [asdict(item) for item in probes],
    }
    report["conclusion"] = classify_report(report, language)
    return report


def print_report(report: dict[str, Any], run_number: int, language: str = "zh") -> None:
    if language == "en":
        print(f"Run {run_number} ({report['role']}) at {report['timestamp']}")
        print(f"  target: {report['base_url']} (device={report['device_id']})")
    else:
        role_label = ROLE_LABELS.get(report["role"], report["role"])
        print(f"第 {run_number} 次（{role_label}） 时间：{report['timestamp']}")
        print(f"  目标：{report['base_url']}（设备={report['device_id']}）")
    for item in report["probes"]:
        if not item["ok"]:
            state = "fail"
        elif item["kind"] == "http" and (
            (item["status"] is not None and item["status"] >= 400)
            or (item.get("application_code") is not None and item["application_code"] >= 400)
        ):
            state = "resp"
        else:
            state = "pass"
        if language == "en":
            state_label = {"pass": "PASS", "resp": "RESP", "fail": "FAIL"}[state]
            probe_label = item["name"]
            elapsed_label = "ms"
        else:
            state_label = STATUS_LABELS[state]
            probe_label = PROBE_LABELS.get(item["name"], item["name"])
            elapsed_label = "毫秒"
        suffix = f" {item['detail']}" if item["detail"] else ""
        print(f"  {state_label:<4} {probe_label:<26} {item['elapsed_ms']:>8.1f} {elapsed_label}{suffix}")
    conclusion = report["conclusion"]
    if language == "en":
        print(f"  CONCLUSION {conclusion['code']}: {conclusion['message']}")
        print(f"  {conclusion['next_step']}")
    else:
        print(f"  结论 {conclusion['code']}：{conclusion['message']}")
        print(f"  {conclusion['next_step']}")


def summarize_reports(reports: list[dict[str, Any]]) -> dict[str, Any]:
    codes = [str(report["conclusion"]["code"]) for report in reports]
    priority = (
        "CLIENT_NETWORK_FAILURE",
        "CLIENT_POST_TRANSPORT_FAILURE",
        "UIFLOW_POST_TIMEOUT",
        "POST_ACCEPTED",
        "DEVICE_OFFLINE_REPORTED",
        "UIFLOW_SERVER_ERROR",
        "UIFLOW_REJECTED_REQUEST",
        "UIFLOW_BASE_GATEWAY_ERROR",
        "ROUTE_REACHABLE_POST_UNTESTED",
    )
    code = next((candidate for candidate in priority if candidate in codes), codes[-1])
    message = next(
        report["conclusion"]["message"]
        for report in reports
        if report["conclusion"]["code"] == code
    )
    next_step = next(
        report["conclusion"]["next_step"]
        for report in reports
        if report["conclusion"]["code"] == code
    )
    return {"code": code, "message": message, "next_step": next_step, "runs": len(reports), "codes": codes}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="诊断 UIFlow 推送链路；默认只读，不发送 POST。"
    )
    parser.add_argument("--role", choices=("client", "uiflow"), default="client", help="运行测试的一方")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="UIFlow 基础地址")
    parser.add_argument(
        "--device-id",
        default=DEFAULT_DEVICE_ID,
        help="路由探测使用的设备 ID；--execute 时必须替换为测试设备 ID",
    )
    parser.add_argument("--execute", action="store_true", help="发送一次诊断 POST；可能改变指定设备代码")
    parser.add_argument("--code-file", help="POST 使用的 UTF-8 代码文件；默认使用内置短代码")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS, help="每个探测的超时时间（秒）")
    parser.add_argument("--repeat", type=int, default=1, help="探测轮数")
    parser.add_argument("--interval", type=float, default=0.0, help="探测轮次之间的间隔（秒）")
    parser.add_argument("--json", action="store_true", help="输出 JSON Lines")
    parser.add_argument("--language", choices=("zh", "en"), default="zh", help="输出语言，默认中文")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.timeout <= 0 or args.timeout > 120:
        parser.error("--timeout must be between 0 and 120 seconds")
    if args.repeat < 1 or args.repeat > 20:
        parser.error("--repeat must be between 1 and 20")
    if args.interval < 0 or args.interval > 300:
        parser.error("--interval must be between 0 and 300 seconds")
    if args.execute and args.repeat != 1:
        parser.error("--execute requires --repeat 1 to avoid repeated device writes")
    if args.execute and args.device_id == DEFAULT_DEVICE_ID:
        parser.error("--execute requires a real test device ID")

    try:
        base_url, host, port = normalize_base_url(args.base_url)
    except ValueError as exc:
        parser.error(str(exc))
    try:
        code = load_code(args.code_file)
    except ValueError as exc:
        parser.error(str(exc))

    reports: list[dict[str, Any]] = []
    for index in range(args.repeat):
        report = run_once(
            base_url,
            host,
            port,
            args.device_id,
            args.timeout,
            role=args.role,
            execute=args.execute,
            code=code,
            language=args.language,
        )
        reports.append(report)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
        else:
            print_report(report, index + 1, args.language)
        if index + 1 < args.repeat and args.interval:
            time.sleep(args.interval)

    summary = summarize_reports(reports)
    if args.json:
        print(json.dumps({"summary": summary}, ensure_ascii=False, separators=(",", ":")))
    else:
        if args.language == "en":
            print(f"SUMMARY {summary['code']}: {summary['message']}")
            print(f"NEXT: {summary['next_step']}")
            print("RESP means the HTTP service answered with a non-2xx or application-level error; inspect its status/body.")
            if args.execute:
                print("A diagnostic POST was sent; inspect the device and server logs before repeating it.")
            else:
                print("No POST or upload was sent; a successful route probe does not prove device execution.")
        else:
            print(f"汇总 {summary['code']}：{summary['message']}")
            print(summary["next_step"])
            print("说明：‘已响应’表示服务端返回了 HTTP 或应用层错误，不是客户端连接超时。")
            if args.execute:
                print("本次已发送诊断 POST；请结合设备和服务端日志，不要重复写入生产设备。")
            else:
                print("本次没有发送 POST 或上传资源；只读路由正常不代表设备已执行代码。")
    return 0 if summary["code"] in {"POST_ACCEPTED", "ROUTE_REACHABLE_POST_UNTESTED"} else 1


if __name__ == "__main__":
    sys.exit(main())
