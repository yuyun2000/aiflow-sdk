"""Small executable client for AIFlow Web Agent Service V3."""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import mimetypes
import os
import secrets
import sys
import time
from pathlib import Path
from typing import Any, Iterator

import httpx


TOKEN_HEADER = "X-AIFlow-Context-Token"
AUTH_SCHEME = "AIFLOW-HMAC-SHA256-V1"
KEY_ID_HEADER = "X-AIFlow-Client-Key"
TIMESTAMP_HEADER = "X-AIFlow-Timestamp"
NONCE_HEADER = "X-AIFlow-Nonce"
CONTENT_HASH_HEADER = "X-AIFlow-Content-SHA256"
SIGNATURE_HEADER = "X-AIFlow-Signature"
RESPONSE_TIMESTAMP_HEADER = "X-AIFlow-Response-Timestamp"
RESPONSE_SIGNATURE_HEADER = "X-AIFlow-Response-Signature"


def decode_secret(value: str) -> bytes:
    return base64.urlsafe_b64decode(value.strip() + "=" * (-len(value.strip()) % 4))


def sign(secret: bytes, value: str) -> str:
    digest = hmac.new(secret, value.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def load_client_secret(key_id: str | None, secret_file: str | None) -> tuple[str | None, bytes | None]:
    encoded = os.environ.get("AIFLOW_CLIENT_SECRET")
    if secret_file:
        raw = Path(secret_file).read_text(encoding="utf-8").strip()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            clients = payload.get("clients", payload)
            if not key_id or not isinstance(clients, dict) or key_id not in clients:
                raise ValueError("client key ID is required and must exist in the JSON secret file")
            encoded = str(clients[key_id])
        else:
            encoded = raw
    if bool(key_id) != bool(encoded):
        raise ValueError("client key ID and client secret must be configured together")
    if not key_id:
        return None, None
    secret = decode_secret(encoded or "")
    if len(secret) < 32:
        raise ValueError("client secret must decode to at least 32 bytes")
    return key_id, secret


class AIFlowClient:
    def __init__(
        self,
        base_url: str,
        token: str | None = None,
        client_key_id: str | None = None,
        client_secret: bytes | None = None,
        origin: str | None = None,
    ):
        self.client = httpx.Client(base_url=base_url.rstrip("/"), timeout=30)
        self.token = token
        self.client_key_id = client_key_id
        self.client_secret = client_secret
        self.origin = (origin or base_url).rstrip("/")

    @property
    def headers(self) -> dict[str, str]:
        return {TOKEN_HEADER: self.token} if self.token else {}

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        authenticated: bool = True,
        signed: bool = True,
    ) -> httpx.Response:
        headers = self.headers if authenticated else {}
        if method.upper() not in {"GET", "HEAD", "OPTIONS"} and self.origin:
            headers = {**headers, "Origin": self.origin}
        content = None
        if json_body is not None:
            content = json.dumps(json_body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            headers = {**headers, "Content-Type": "application/json"}
        request = self.client.build_request(method, path, params=params, headers=headers, content=content)
        request_nonce = None
        if signed and self.client_key_id and self.client_secret:
            timestamp = str(int(time.time()))
            request_nonce = secrets.token_urlsafe(18)
            content_hash = hashlib.sha256(request.content).hexdigest()
            target = request.url.raw_path.decode("latin-1")
            canonical = "\n".join(
                (AUTH_SCHEME, request.method.upper(), target, timestamp, request_nonce, content_hash)
            )
            request.headers.update(
                {
                    KEY_ID_HEADER: self.client_key_id,
                    TIMESTAMP_HEADER: timestamp,
                    NONCE_HEADER: request_nonce,
                    CONTENT_HASH_HEADER: content_hash,
                    SIGNATURE_HEADER: sign(self.client_secret, canonical),
                }
            )
        response = self.client.send(request)
        if request_nonce and self.client_secret:
            response_timestamp = response.headers.get(RESPONSE_TIMESTAMP_HEADER, "")
            response_signature = response.headers.get(RESPONSE_SIGNATURE_HEADER, "")
            if not response_timestamp or not response_signature:
                if response.is_success:
                    raise httpx.HTTPError("signed request received an unsigned success response")
            else:
                canonical = "\n".join(
                    (AUTH_SCHEME + "-RESPONSE", request_nonce, str(response.status_code), response_timestamp)
                )
                expected = sign(self.client_secret, canonical)
                if not hmac.compare_digest(expected, response_signature):
                    raise httpx.HTTPError("server response signature verification failed")
        return response

    def request_json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        response = self.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json()

    def create_context(self, label: str, device: dict[str, Any]) -> dict[str, Any]:
        result = self.request_json(
            "POST",
            "/api/v3/contexts",
            json_body={"label": label, "device": device},
            authenticated=False,
        )
        self.token = result["access_token"]
        return result

    def start_coding(
        self,
        prompt: str,
        deploy_mode: str = "none",
        attachments: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        return self.request_json(
            "POST",
            "/api/v3/tasks/coding",
            json_body={
                "prompt": prompt,
                "deploy_mode": deploy_mode,
                "attachments": attachments or [],
            },
        )

    def direct_run(self, include_resources: bool = True) -> dict[str, Any]:
        return self.request_json(
            "POST",
            "/api/v3/tasks/direct-run",
            json_body={"code_path": "main.py", "include_resources": include_resources},
        )

    def task_status(self, task_id: str) -> dict[str, Any]:
        return self.request_json("GET", f"/api/v3/tasks/{task_id}")

    def events(self, task: dict[str, Any]) -> Iterator[dict[str, Any]]:
        with self.client.stream(
            "GET",
            task["events_url"],
            params={"stream_token": task["stream_token"]},
            timeout=None,
        ) as response:
            response.raise_for_status()
            event_type = "message"
            data_lines: list[str] = []
            for line in response.iter_lines():
                if line.startswith("event:"):
                    event_type = line[6:].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[5:].strip())
                elif line == "" and data_lines:
                    yield {"type": event_type, "data": json.loads("\n".join(data_lines))}
                    event_type = "message"
                    data_lines = []

    def wait(self, task_id: str, timeout: float = 300) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = self.task_status(task_id)
            if result["status"] in {"completed", "failed", "cancelled"}:
                return result
            time.sleep(1)
        raise TimeoutError(f"task {task_id} did not finish within {timeout} seconds")


def main() -> int:
    parser = argparse.ArgumentParser(description="Exercise AIFlow Web Agent Service V3")
    parser.add_argument("--base-url", default="http://127.0.0.1:8880")
    parser.add_argument("--token", help="existing context token")
    parser.add_argument(
        "--origin",
        help="Origin header for the anonymous web gateway; defaults to --base-url",
    )
    parser.add_argument("--client-key-id", default=os.environ.get("AIFLOW_CLIENT_KEY_ID"))
    parser.add_argument("--client-secret-file", default=os.environ.get("AIFLOW_CLIENT_SECRET_FILE"))
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create-context")
    create.add_argument("--label", default="CLI smoke client")
    create.add_argument("--device-id", required=True)
    create.add_argument("--client-id", required=True)
    create.add_argument("--product")

    code = subparsers.add_parser("code")
    code.add_argument("prompt", nargs="?", default="")
    code.add_argument("--deploy-mode", choices=["none", "server", "agent"], default="none")
    code.add_argument("--image", action="append", default=[], metavar="FILE")
    code.add_argument("--audio", action="append", default=[], metavar="FILE")

    rerun = subparsers.add_parser("rerun")
    rerun.add_argument("--code-only", action="store_true")

    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("task_id")
    subparsers.add_parser("system-status")
    subparsers.add_parser("project")

    args = parser.parse_args()
    try:
        key_id, client_secret = load_client_secret(args.client_key_id, args.client_secret_file)
        api = AIFlowClient(args.base_url, args.token, key_id, client_secret, args.origin)
        if args.command == "create-context":
            result = api.create_context(
                args.label,
                {
                    "device_id": args.device_id,
                    "client_id": args.client_id,
                    "product": args.product,
                },
            )
        elif args.command == "code":
            if not args.token:
                parser.error("code requires --token")
            attachments = []
            for kind, paths in (("image", args.image), ("audio", args.audio)):
                for raw_path in paths:
                    path = Path(raw_path)
                    mime_type = mimetypes.guess_type(path.name)[0]
                    if not mime_type or not mime_type.startswith(kind + "/"):
                        parser.error(f"cannot determine a supported {kind} MIME type for {path}")
                    attachments.append(
                        {
                            "kind": kind,
                            "mime_type": mime_type,
                            "name": path.name,
                            "data_base64": base64.b64encode(path.read_bytes()).decode("ascii"),
                        }
                    )
            task = api.start_coding(args.prompt, args.deploy_mode, attachments)
            for event in api.events(task):
                print(json.dumps(event, ensure_ascii=False))
            result = api.task_status(task["task_id"])
        elif args.command == "rerun":
            if not args.token:
                parser.error("rerun requires --token")
            task = api.direct_run(include_resources=not args.code_only)
            result = api.wait(task["task_id"])
        elif args.command == "status":
            if not args.token:
                parser.error("status requires --token")
            result = api.task_status(args.task_id)
        elif args.command == "project":
            if not args.token:
                parser.error("project requires --token")
            result = api.request_json("GET", "/api/v3/project")
        else:
            result = api.request_json(
                "GET",
                "/api/v3/system/status",
                authenticated=False,
                signed=False,
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (httpx.HTTPError, TimeoutError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
