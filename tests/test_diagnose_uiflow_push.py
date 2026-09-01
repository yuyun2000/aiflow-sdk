from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from time import sleep

from scripts.diagnose_uiflow_push import (
    classify_report,
    endpoint_url,
    normalize_base_url,
    print_report,
    probe_http,
    run_once,
)


class DiagnosticHandler(BaseHTTPRequestHandler):
    def _response(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            # A client-side timeout can close the socket before the fake server replies.
            pass

    def do_GET(self) -> None:
        base_status = getattr(self.server, "base_status", 502)
        if self.path == "/" and base_status == 200:
            self._response(200, b'{"ok":true}')
        elif self.path == "/":
            self._response(base_status, b"upstream unavailable")
        else:
            self._response(405, b'{"message":"GET method not supported"}')

    def do_OPTIONS(self) -> None:
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        self.server.received_body = self.rfile.read(length)  # type: ignore[attr-defined]
        self.server.received_content_type = self.headers.get("Content-Type")  # type: ignore[attr-defined]
        delay = self.server.post_delay  # type: ignore[attr-defined]
        if delay:
            sleep(delay)
        self._response(self.server.post_status, self.server.post_body)  # type: ignore[attr-defined]

    def log_message(self, _format: str, *_args: object) -> None:
        return


class ApplicationErrorHandler(DiagnosticHandler):
    def do_GET(self) -> None:
        self._response(200, b'{"code":500,"msg":"operation failed"}')


@contextmanager
def diagnostic_server(
    *,
    base_status: int = 200,
    post_status: int = 200,
    post_body: bytes = b'{"deviceId":"device-test","chunkCount":1}',
    post_delay: float = 0.0,
) -> Iterator[ThreadingHTTPServer]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), DiagnosticHandler)
    server.base_status = base_status  # type: ignore[attr-defined]
    server.post_status = post_status  # type: ignore[attr-defined]
    server.post_body = post_body  # type: ignore[attr-defined]
    server.post_delay = post_delay  # type: ignore[attr-defined]
    server.received_body = b""  # type: ignore[attr-defined]
    server.received_content_type = None  # type: ignore[attr-defined]
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_normalize_base_url_and_endpoint_preserve_path_prefix() -> None:
    base, host, port = normalize_base_url("https://uiflow.example/m5stack/")

    assert (base, host, port) == ("https://uiflow.example/m5stack", "uiflow.example", 443)
    assert endpoint_url(base, "/api/v1/device/push-code/test") == (
        "https://uiflow.example/m5stack/api/v1/device/push-code/test"
    )


def test_http_error_is_reported_as_remote_response() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), DiagnosticHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        result = probe_http("http_get", f"http://127.0.0.1:{port}/", "GET", 2)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert result.ok is True
    assert result.status == 502
    assert "upstream unavailable" in result.detail


def test_http_application_error_code_is_retained() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), ApplicationErrorHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        result = probe_http("http_get", f"http://127.0.0.1:{port}/", "GET", 2)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert result.ok is True
    assert result.status == 200
    assert result.application_code == 500
    assert "app_code=500" in result.detail


def test_execute_post_accepted_and_body_is_recorded() -> None:
    with diagnostic_server() as server:
        port = server.server_address[1]
        report = run_once(
            f"http://127.0.0.1:{port}",
            "127.0.0.1",
            port,
            "device-test",
            2,
            role="client",
            execute=True,
            code=b'print("diagnostic")\n',
        )

    assert report["conclusion"]["code"] == "POST_ACCEPTED"
    assert server.received_body == b'print("diagnostic")\n'  # type: ignore[attr-defined]
    assert server.received_content_type == "text/plain; charset=UTF-8"  # type: ignore[attr-defined]


def test_execute_post_reports_device_offline() -> None:
    with diagnostic_server(
        post_status=409,
        post_body=b'{"message":"Device is offline. Please connect the device and try again."}',
    ) as server:
        port = server.server_address[1]
        report = run_once(
            f"http://127.0.0.1:{port}",
            "127.0.0.1",
            port,
            "device-test",
            2,
            role="client",
            execute=True,
            code=b"print(1)\n",
        )

    assert report["conclusion"]["code"] == "DEVICE_OFFLINE_REPORTED"


def test_execute_post_timeout_is_distinguished_from_http_error() -> None:
    with diagnostic_server(post_delay=0.2) as server:
        port = server.server_address[1]
        report = run_once(
            f"http://127.0.0.1:{port}",
            "127.0.0.1",
            port,
            "device-test",
            0.05,
            role="client",
            execute=True,
            code=b"print(1)\n",
        )

    post = next(item for item in report["probes"] if item["name"] == "http_post_push_code")
    assert report["conclusion"]["code"] == "UIFLOW_POST_TIMEOUT"
    assert post["ok"] is False
    assert "timeout" in post["detail"].lower()


def test_read_only_base_gateway_error_is_explicitly_unproven_for_post() -> None:
    with diagnostic_server(base_status=502) as server:
        port = server.server_address[1]
        report = run_once(
            f"http://127.0.0.1:{port}",
            "127.0.0.1",
            port,
            "device-test",
            2,
            role="client",
            execute=False,
            code=b"print(1)\n",
        )

    assert report["conclusion"]["code"] == "UIFLOW_BASE_GATEWAY_ERROR"
    assert "尚未验证推送 POST" in report["conclusion"]["message"]


def test_default_human_report_is_chinese(capsys) -> None:
    report = {
        "role": "client",
        "timestamp": "2026-09-01T00:00:00+00:00",
        "base_url": "http://127.0.0.1:8080/m5stack",
        "device_id": "****",
        "probes": [
            {"name": "dns", "kind": "network", "ok": True, "elapsed_ms": 1.0, "status": None, "detail": ""}
        ],
        "conclusion": {
            "code": "CLIENT_NETWORK_FAILURE",
            "message": "当前测试机的 DNS、TCP 或 TLS 失败。",
            "next_step": "下一步：检查网络。",
        },
    }

    print_report(report, 1)
    output = capsys.readouterr().out
    assert "第 1 次（客户端）" in output
    assert "DNS 解析" in output
    assert "结论 CLIENT_NETWORK_FAILURE" in output


def test_classify_http_500_as_uiflow_server_error() -> None:
    report = {
        "probes": [
            {"name": "dns", "kind": "network", "ok": True},
            {"name": "tcp", "kind": "network", "ok": True},
            {"name": "http_post_push_code", "kind": "http", "ok": True, "status": 500, "detail": "http=500"},
        ]
    }

    assert classify_report(report)["code"] == "UIFLOW_SERVER_ERROR"


def test_classify_application_500_as_uiflow_server_error() -> None:
    report = {
        "probes": [
            {"name": "dns", "kind": "network", "ok": True},
            {"name": "tcp", "kind": "network", "ok": True},
            {
                "name": "http_post_push_code",
                "kind": "http",
                "ok": True,
                "status": 200,
                "application_code": 500,
                "detail": "http=200 app_code=500",
            },
        ]
    }

    assert classify_report(report)["code"] == "UIFLOW_SERVER_ERROR"
