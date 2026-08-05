#!/usr/bin/env python3
"""Validate and push code/resources through the AIFlow Local device API."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import quote, urlencode, urlsplit, urlunsplit


DEFAULT_BASE_URL = "https://ai-flow.m5stack.com/"
DEFAULT_TIMEOUT = 120.0
MAX_IMAGE_BYTES = 2 * 1024 * 1024
MAX_FILE_BYTES = 100 * 1024 * 1024
MAX_REQUEST_BYTES = 500 * 1024 * 1024
IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "bmp"}
FORBIDDEN_RESOURCE_NAMES = {"main.py", "main_ota_temp.py"}
STATUS_MARKER = "\n__AIFLOW_HTTP_STATUS__:"


class PushError(Exception):
    pass


def nonempty(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def config_value(config: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in config and config[key] is not None:
            return config[key]
    return None


def load_config(explicit_path: Optional[str]) -> Tuple[Dict[str, Any], Optional[Path]]:
    env_path = nonempty(os.environ.get("AIFLOW_CONFIG"))
    selected = nonempty(explicit_path) or env_path
    required = selected is not None
    path = Path(selected).expanduser() if selected else Path.cwd() / ".aiflow" / "config.json"

    if not path.exists():
        if required:
            raise PushError("AIFlow config file does not exist")
        return {}, None
    if not path.is_file():
        raise PushError("AIFlow config path is not a regular file")

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PushError("AIFlow config must be readable UTF-8 JSON: %s" % exc) from exc
    if not isinstance(data, dict):
        raise PushError("AIFlow config root must be a JSON object")
    return data, path.resolve()


def normalize_base_url(value: str) -> str:
    parts = urlsplit(value.strip())
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise PushError("base URL must be an absolute http(s) URL")
    if parts.username or parts.password:
        raise PushError("base URL must not contain credentials")
    if parts.query or parts.fragment:
        raise PushError("base URL must not contain a query or fragment")
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def parse_timeout(value: Any) -> float:
    try:
        timeout = float(value)
    except (TypeError, ValueError) as exc:
        raise PushError("timeout must be a number") from exc
    if timeout <= 0 or timeout > 3600:
        raise PushError("timeout must be greater than 0 and at most 3600 seconds")
    return timeout


def resolve_settings(args: argparse.Namespace, config: Dict[str, Any]) -> Dict[str, Any]:
    cli_device_id = nonempty(args.device_id)
    env_device_id = nonempty(os.environ.get("AIFLOW_DEVICE_ID"))
    device_id = (
        cli_device_id
        or env_device_id
        or nonempty(config_value(config, "defaultDeviceId", "default_device_id", "deviceId", "device_id"))
    )
    if not device_id:
        raise PushError("device ID is required; pass --device-id or set AIFLOW_DEVICE_ID")
    if any(ord(ch) < 32 for ch in device_id):
        raise PushError("device ID must not contain control characters")

    client_id = (
        nonempty(args.client_id)
        or nonempty(os.environ.get("AIFLOW_CLIENT_ID"))
        or nonempty(config_value(config, "clientId", "client_id"))
    )
    if not client_id:
        raise PushError("client ID is required; pass --client-id or set AIFLOW_CLIENT_ID")
    if any(ord(ch) < 32 for ch in client_id):
        raise PushError("client ID must not contain control characters")
    base_url = normalize_base_url(
        nonempty(args.base_url)
        or nonempty(os.environ.get("AIFLOW_BASE_URL"))
        or nonempty(config_value(config, "baseUrl", "base_url"))
        or DEFAULT_BASE_URL
    )
    timeout = parse_timeout(
        nonempty(args.timeout)
        or nonempty(os.environ.get("AIFLOW_TIMEOUT"))
        or config_value(config, "timeout")
        or DEFAULT_TIMEOUT
    )
    return {
        "base_url": base_url,
        "device_id": device_id,
        "client_id": client_id,
        "timeout": timeout,
    }


def validate_code(raw_path: Optional[str]) -> Optional[Dict[str, Any]]:
    if not raw_path:
        return None
    path = Path(raw_path).expanduser()
    if not path.is_file():
        raise PushError("code path is not a regular file")
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise PushError("code file must be readable UTF-8 text: %s" % exc) from exc
    if not content.strip():
        raise PushError("code file must not be empty or whitespace-only")
    return {"path": path.resolve(), "name": path.name, "size": path.stat().st_size}


def normalize_device_path(value: str) -> str:
    normalized = value.strip().replace("\\", "/")
    if not normalized:
        return ""
    if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        raise PushError("device directory must not contain control characters")

    flash_prefixes = ("file:///flash/", "file://flash/", "/flash/", "flash/")
    flash_relative = False
    for prefix in flash_prefixes:
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix):].lstrip("/")
            flash_relative = True
            break
    else:
        sd_prefixes = ("file:///sd/", "file://sd/")
        for prefix in sd_prefixes:
            if normalized.startswith(prefix):
                normalized = "/sd/" + normalized[len(prefix):].lstrip("/")
                break
        if ":" in normalized.split("/", 1)[0]:
            raise PushError("device directory must not use an unsupported URI scheme")

    raw_segments = normalized.split("/")
    segments = [segment for segment in raw_segments if segment]
    if any(segment in {".", ".."} for segment in segments):
        raise PushError("device directory must not contain . or .. path segments")
    if not segments:
        raise PushError("device directory must not be empty after normalization")
    leading_slash = normalized.startswith("/") and not flash_relative
    return ("/" if leading_slash else "") + "/".join(segments) + "/"


def validate_resources(values: Sequence[str]) -> List[Dict[str, Any]]:
    resources: List[Dict[str, Any]] = []
    names = set()
    total_size = 0

    for value in values:
        literal_path = Path(value).expanduser()
        if literal_path.is_file():
            local_value, device_value = value, ""
        else:
            local_value, separator, device_value = value.rpartition("::")
            if not separator:
                local_value, device_value = value, ""
        if not nonempty(local_value):
            raise PushError("resource path must not be blank")

        path = Path(local_value).expanduser()
        if not path.is_file():
            raise PushError("resource path is not a regular file: %s" % path.name)
        size = path.stat().st_size
        if size <= 0:
            raise PushError("resource file must not be empty: %s" % path.name)
        if size > MAX_FILE_BYTES:
            raise PushError("resource exceeds the 100 MiB per-file limit: %s" % path.name)

        resolved_path = path.resolve()
        if any(
            ord(character) < 32 or ord(character) == 127
            for character in str(resolved_path)
        ):
            raise PushError("resource path must not contain control characters: %s" % path.name)
        if "\\" in path.name:
            raise PushError("resource filename must not contain a backslash: %s" % path.name)

        name_key = unicodedata.normalize("NFC", path.name).casefold()
        if name_key in FORBIDDEN_RESOURCE_NAMES:
            raise PushError("send %s through the code endpoint, not as a resource" % path.name)
        if name_key in names:
            raise PushError("resource basenames must be unique: %s" % path.name)
        names.add(name_key)

        extension = path.suffix.lower().lstrip(".")
        if extension in IMAGE_EXTENSIONS and size > MAX_IMAGE_BYTES:
            raise PushError("image exceeds the 2 MiB limit: %s" % path.name)

        total_size += size
        if total_size > MAX_REQUEST_BYTES:
            raise PushError("resource batch exceeds the 500 MiB request limit")
        resources.append(
            {
                "path": resolved_path,
                "name": path.name,
                "size": size,
                "device_path": normalize_device_path(device_value),
            }
        )
    return resources


def mask(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    if len(value) <= 4:
        return "****"
    return value[:2] + "***" + value[-2:]


def build_plan(command: str, settings: Dict[str, Any], code: Any, resources: Any) -> Dict[str, Any]:
    return {
        "ok": True,
        "action": command,
        "executed": False,
        "target": {
            "deviceId": mask(settings["device_id"]),
            "clientId": mask(settings["client_id"]),
            "baseUrl": settings["base_url"],
        },
        "code": None if code is None else {"name": code["name"], "bytes": code["size"]},
        "resources": [
            {"name": item["name"], "bytes": item["size"], "devicePath": item["device_path"] or "auto"}
            for item in resources
        ],
    }


def run_curl(arguments: Sequence[str], timeout: float, settings: Dict[str, Any]) -> Tuple[int, str]:
    curl = shutil.which("curl")
    if not curl:
        raise PushError("curl is required but was not found")
    command = [
        curl,
        "--silent",
        "--show-error",
        "--request",
        "POST",
        "--connect-timeout",
        str(min(timeout, 15.0)),
        "--max-time",
        str(timeout),
        *arguments,
        "--write-out",
        STATUS_MARKER + "%{http_code}",
    ]
    completed = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    stdout = completed.stdout.decode("utf-8", errors="replace")
    stderr = completed.stderr.decode("utf-8", errors="replace").strip()
    body, marker, status_text = stdout.rpartition(STATUS_MARKER)
    status = int(status_text) if marker and status_text.isdigit() else 0

    if completed.returncode != 0:
        detail = stderr or "curl request failed"
        raise PushError(redact(detail, settings))
    if status < 200 or status >= 300:
        detail = response_message(body) or "HTTP request failed"
        raise PushError("HTTP %d: %s" % (status, redact(detail, settings)))
    return status, body


def response_message(body: str) -> str:
    text = body.strip()
    if not text:
        return ""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return text[:500]
    if isinstance(payload, dict):
        for key in ("message", "error", "detail"):
            if payload.get(key):
                return str(payload[key])[:500]
    return text[:500]


def redact(text: str, settings: Dict[str, Any]) -> str:
    for value in (settings.get("device_id"), settings.get("client_id")):
        if value:
            text = text.replace(value, mask(value) or "****")
    return text


def parse_json_body(body: str) -> Dict[str, Any]:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise PushError("server returned a non-JSON success response") from exc
    if not isinstance(payload, dict):
        raise PushError("server returned an unexpected success response")
    return payload


def push_code(settings: Dict[str, Any], code: Dict[str, Any]) -> Dict[str, Any]:
    url = "%s/api/v1/device/push-code/%s" % (
        settings["base_url"],
        quote(settings["device_id"], safe=""),
    )
    status, body = run_curl(
        [
            "--url",
            url,
            "--header",
            "Content-Type: text/plain; charset=UTF-8",
            "--data-binary",
            "@" + str(code["path"]),
        ],
        settings["timeout"],
        settings,
    )
    payload = parse_json_body(body)
    return {
        "ok": True,
        "action": "push-code",
        "executed": True,
        "httpStatus": status,
        "deviceId": mask(nonempty(payload.get("deviceId")) or settings["device_id"]),
        "chunkCount": payload.get("chunkCount"),
    }


def quote_curl_form_value(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def resource_form_value(resource: Dict[str, Any]) -> str:
    local_path = quote_curl_form_value(str(resource["path"]))
    filename = quote_curl_form_value(str(resource["name"]))
    return "files=@%s;filename=%s" % (local_path, filename)


def push_resources(settings: Dict[str, Any], resources: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    query = {
        "deviceId": settings["device_id"],
        "clientId": settings["client_id"],
    }
    url = "%s/api/v1/localFiles/upload-resource-batch-and-push?%s" % (
        settings["base_url"],
        urlencode(query),
    )
    arguments: List[str] = ["--url", url]
    if any('"' in str(item["path"]) or '"' in item["name"] for item in resources):
        arguments.append("--form-escape")
    include_paths = any(item["device_path"] for item in resources)
    for item in resources:
        arguments.extend(["--form", resource_form_value(item)])
        if include_paths:
            arguments.extend(["--form-string", "filePaths=" + item["device_path"]])

    status, body = run_curl(arguments, settings["timeout"], settings)
    payload = parse_json_body(body)
    push_result = payload.get("pushResult") if isinstance(payload.get("pushResult"), dict) else {}
    return {
        "ok": True,
        "action": "push-resources",
        "executed": True,
        "httpStatus": status,
        "batchId": payload.get("batchId"),
        "deviceId": mask(nonempty(push_result.get("deviceId")) or settings["device_id"]),
        "fileCount": push_result.get("fileCount", len(resources)),
        "totalSize": push_result.get("totalSize", sum(item["size"] for item in resources)),
    }


def add_target_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", help="JSON config path; defaults to AIFLOW_CONFIG or .aiflow/config.json")
    parser.add_argument("--base-url", help="AIFlow service base URL")
    parser.add_argument("--device-id", help="bound platform device ID")
    parser.add_argument("--client-id", help="required client-provided upload client ID")
    parser.add_argument("--timeout", help="request timeout in seconds")


def add_file_arguments(parser: argparse.ArgumentParser, code: bool, resources: bool) -> None:
    if code:
        parser.add_argument("--code", help="UTF-8 MicroPython source file")
    if resources:
        parser.add_argument(
            "--resource",
            action="append",
            default=[],
            metavar="FILE[::DEVICE_DIR]",
            help="resource file with optional device directory; repeat for multiple files",
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate and push code/resources through the AIFlow Local device API."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan = subparsers.add_parser("plan", help="validate target and files without network access")
    add_target_arguments(plan)
    add_file_arguments(plan, code=True, resources=True)

    code = subparsers.add_parser("push-code", help="push one UTF-8 source file")
    add_target_arguments(code)
    add_file_arguments(code, code=True, resources=False)
    code.add_argument("--execute", action="store_true", help="perform the state-changing HTTP request")

    resources = subparsers.add_parser("push-resources", help="upload and publish resource files")
    add_target_arguments(resources)
    add_file_arguments(resources, code=False, resources=True)
    resources.add_argument("--execute", action="store_true", help="perform the state-changing HTTP request")

    deploy = subparsers.add_parser("deploy", help="push resources first, then code")
    add_target_arguments(deploy)
    add_file_arguments(deploy, code=True, resources=True)
    deploy.add_argument("--execute", action="store_true", help="perform the state-changing HTTP requests")
    return parser


def execute(args: argparse.Namespace) -> Dict[str, Any]:
    config, _ = load_config(args.config)
    settings = resolve_settings(args, config)
    code = validate_code(getattr(args, "code", None))
    resources = validate_resources(getattr(args, "resource", []))

    if args.command == "push-code" and code is None:
        raise PushError("push-code requires --code")
    if args.command == "push-resources" and not resources:
        raise PushError("push-resources requires at least one --resource")
    if args.command == "deploy" and code is None and not resources:
        raise PushError("deploy requires --code, --resource, or both")

    plan = build_plan(args.command, settings, code, resources)
    if args.command == "plan" or not args.execute:
        if args.command != "plan":
            plan["message"] = "validated only; add --execute after explicit authorization"
        return plan

    if args.command == "push-code":
        return push_code(settings, code)
    if args.command == "push-resources":
        return push_resources(settings, resources)

    steps = []
    if resources:
        steps.append(push_resources(settings, resources))
    if code:
        steps.append(push_code(settings, code))
    return {"ok": True, "action": "deploy", "executed": True, "steps": steps}


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        result = execute(args)
    except PushError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
