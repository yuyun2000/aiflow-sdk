from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import replace
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import parse_qs, urlsplit

import pytest

from aiflow_server.config import load_settings
from aiflow_server.device_push import DeploymentError, DevicePusher
from aiflow_server.workspaces import WorkspaceManager


class RecordingPushHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self.server.requests.append(
            {
                "path": self.path,
                "headers": dict(self.headers.items()),
                "body": body,
            }
        )

        parsed = urlsplit(self.path)
        if self.server.fail_path and parsed.path == self.server.fail_path:
            self._json_response(503, {"message": "temporary failure for device-test-123"})
            return
        if parsed.path == "/api/v1/localFiles/upload-resource-batch-and-push":
            self._json_response(
                200,
                {
                    "batchId": "batch-test",
                    "pushResult": {
                        "deviceId": "device-test-123",
                        "fileCount": 2,
                        "totalSize": 9,
                    },
                },
            )
            return
        if parsed.path == "/api/v1/device/push-code/device-test-123":
            self._json_response(200, {"deviceId": "device-test-123", "chunkCount": 2})
            return
        self._json_response(404, {"message": "not found"})

    def _json_response(self, status: int, payload: dict) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, _format: str, *_args: object) -> None:
        return


@contextmanager
def push_server(*, fail_path: str | None = None):
    server = ThreadingHTTPServer(("127.0.0.1", 0), RecordingPushHandler)
    server.requests = []
    server.fail_path = fail_path
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield server, f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def multipart_parts(request: dict) -> list[dict[str, object]]:
    content_type = request["headers"]["Content-Type"]
    message = BytesParser(policy=default).parsebytes(
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode()
        + request["body"]
    )
    assert message.is_multipart()
    return [
        {
            "name": part.get_param("name", header="content-disposition"),
            "filename": part.get_filename(),
            "body": part.get_payload(decode=True),
        }
        for part in message.iter_parts()
    ]


def make_pusher(tmp_path: Path, base_url: str, *, with_resources: bool = True):
    base = load_settings()
    settings = replace(
        base,
        data_dir=tmp_path / "data",
        device_push_base_url=base_url,
        device_push_timeout=5,
    )
    workspaces = WorkspaceManager(settings)
    context_id = "ctx_http_test"
    device = {
        "device_id": "device-test-123",
        "client_id": "client-test-456",
    }
    workspace = workspaces.initialize(context_id, device)
    code_bytes = 'print("你好，UIFlow2")\n'.encode("utf-8")
    workspace.joinpath("main.py").write_bytes(code_bytes)

    if with_resources:
        assets = workspace / "assets"
        assets.mkdir()
        assets.joinpath("logo.bin").write_bytes(b"logo")
        assets.joinpath("tone.wav").write_bytes(b"audio")
        workspace.joinpath(".aiflow", "deploy.json").write_text(
            json.dumps(
                {
                    "resources": [
                        {"file": "assets/logo.bin", "devicePath": "custom/data"},
                        {"file": "assets/tone.wav"},
                    ]
                }
            ),
            encoding="utf-8",
        )

    context = {"context_id": context_id, "device": device}
    return DevicePusher(settings, workspaces), context, code_bytes


def test_device_pusher_preserves_http_contract_and_order(tmp_path):
    with push_server() as (server, base_url):
        pusher, context, code_bytes = make_pusher(tmp_path, base_url)
        result = asyncio.run(pusher.deploy(context))

    assert [urlsplit(item["path"]).path for item in server.requests] == [
        "/api/v1/localFiles/upload-resource-batch-and-push",
        "/api/v1/device/push-code/device-test-123",
    ]
    resource_request, code_request = server.requests
    resource_query = parse_qs(urlsplit(resource_request["path"]).query)
    assert resource_query == {
        "deviceId": ["device-test-123"],
        "clientId": ["client-test-456"],
    }

    parts = multipart_parts(resource_request)
    assert [(part["name"], part["filename"], part["body"]) for part in parts] == [
        ("files", "logo.bin", b"logo"),
        ("filePaths", None, b"custom/data/"),
        ("files", "tone.wav", b"audio"),
        ("filePaths", None, b""),
    ]
    assert code_request["headers"]["Content-Type"] == "text/plain; charset=UTF-8"
    assert code_request["body"] == code_bytes
    assert [step["action"] for step in result["steps"]] == ["push-resources", "push-code"]


def test_device_pusher_ignores_deployment_code_mislisted_as_resource(tmp_path):
    with push_server() as (server, base_url):
        pusher, context, code_bytes = make_pusher(tmp_path, base_url, with_resources=False)
        workspace = pusher.workspaces.workspace_for(context["context_id"])
        workspace.joinpath(".aiflow", "deploy.json").write_text(
            json.dumps(
                {
                    "resources": [
                        {"file": "main.py", "devicePath": "main.py"},
                    ]
                }
            ),
            encoding="utf-8",
        )

        plan = asyncio.run(pusher.plan(context, "main.py", True))
        result = asyncio.run(pusher.deploy(context, "main.py", True))

    assert plan["resources"] == []
    assert len(server.requests) == 1
    assert urlsplit(server.requests[0]["path"]).path == "/api/v1/device/push-code/device-test-123"
    assert server.requests[0]["body"] == code_bytes
    assert [step["action"] for step in result["steps"]] == ["push-code"]


def test_device_pusher_does_not_retry_or_continue_after_http_failure(tmp_path):
    resource_path = "/api/v1/localFiles/upload-resource-batch-and-push"
    with push_server(fail_path=resource_path) as (server, base_url):
        pusher, context, _ = make_pusher(tmp_path, base_url)
        with pytest.raises(DeploymentError) as caught:
            asyncio.run(pusher.deploy(context))

    assert caught.value.code == "device_push_failed"
    assert caught.value.retryable is False
    assert "device-test-123" not in str(caught.value)
    assert [urlsplit(item["path"]).path for item in server.requests] == [resource_path]


def test_device_pusher_requires_device_and_client_ids(tmp_path):
    base = load_settings()
    settings = replace(base, data_dir=tmp_path / "data")
    workspaces = WorkspaceManager(settings)
    context_id = "ctx_device_id_test"
    device = {"device_id": "device-test-123", "client_id": "client-test-456"}
    workspace = workspaces.initialize(context_id, device)
    workspace.joinpath("main.py").write_text("print('targeted')\n", encoding="utf-8")
    pusher = DevicePusher(settings, workspaces)

    plan = asyncio.run(pusher.plan({"context_id": context_id, "device": device}, "main.py", False))
    assert plan["target"]["deviceId"] == "de***23"
    assert plan["target"]["clientId"] == "cl***56"

    with pytest.raises(DeploymentError) as caught:
        asyncio.run(
            pusher.plan(
                {"context_id": context_id, "device": {}},
                "main.py",
                False,
            )
        )
    assert caught.value.code == "device_target_missing"

    with pytest.raises(DeploymentError) as caught:
        asyncio.run(
            pusher.plan(
                {
                    "context_id": context_id,
                    "device": {"device_id": "device-test-123"},
                },
                "main.py",
                False,
            )
        )
    assert caught.value.code == "client_target_missing"


def test_skill_cli_plan_requires_and_masks_both_client_targets(tmp_path):
    script = Path(__file__).parents[1] / "skills" / "aiflow-device-push" / "scripts" / "aiflow_push.py"
    code = Path(__file__).resolve()
    base_command = [
        sys.executable,
        str(script),
        "plan",
        "--device-id",
        "device-cli-123",
        "--code",
        str(code),
    ]

    missing = subprocess.run(
        base_command,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert missing.returncode == 2
    assert "client ID is required" in missing.stderr

    planned = subprocess.run(
        [*base_command, "--client-id", "client-cli-456"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert planned.returncode == 0, planned.stderr
    payload = json.loads(planned.stdout)
    assert payload["target"]["deviceId"] == "de***23"
    assert payload["target"]["clientId"] == "cl***56"
    assert "device-cli-123" not in planned.stdout
    assert "client-cli-456" not in planned.stdout
