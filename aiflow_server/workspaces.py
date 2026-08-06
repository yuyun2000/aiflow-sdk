from __future__ import annotations

import base64
import binascii
import hashlib
import json
import shutil
import stat
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .config import Settings


class WorkspaceError(ValueError):
    pass


class AttachmentError(WorkspaceError):
    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


ATTACHMENT_EXTENSIONS = {
    "image/png": frozenset({".png"}),
    "image/jpeg": frozenset({".jpg", ".jpeg"}),
    "image/bmp": frozenset({".bmp"}),
    "image/gif": frozenset({".gif"}),
    "image/webp": frozenset({".webp"}),
    "audio/wav": frozenset({".wav"}),
    "audio/x-wav": frozenset({".wav"}),
    "audio/mpeg": frozenset({".mp3"}),
    "audio/mp4": frozenset({".m4a", ".mp4"}),
    "audio/ogg": frozenset({".ogg"}),
    "audio/amr": frozenset({".amr"}),
}


def validate_attachment_name(name: str, mime_type: str, index: int) -> str:
    has_control_character = any(
        ord(character) < 32 or ord(character) == 127 for character in name
    )
    if name != name.strip() or has_control_character:
        raise AttachmentError(
            "invalid_attachment_name",
            f"attachment {index} name contains surrounding whitespace or control characters",
        )
    if name in {".", ".."} or "/" in name or "\\" in name:
        raise AttachmentError(
            "invalid_attachment_name",
            f"attachment {index} name must be a file name without a directory",
        )
    if len(name.encode("utf-8")) > 255:
        raise AttachmentError(
            "invalid_attachment_name",
            f"attachment {index} name exceeds the filesystem byte limit",
        )
    extension = Path(name).suffix.lower()
    if extension not in ATTACHMENT_EXTENSIONS[mime_type]:
        allowed = ", ".join(sorted(ATTACHMENT_EXTENSIONS[mime_type]))
        raise AttachmentError(
            "attachment_extension_mismatch",
            f"attachment {index} name must use an extension matching {mime_type}: {allowed}",
        )
    return name


class WorkspaceManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        settings.clients_dir.mkdir(parents=True, exist_ok=True)

    def workspace_for(self, context_id: str) -> Path:
        return self.settings.clients_dir / context_id / "workspace"

    def initialize(self, context_id: str, device: dict[str, Any]) -> Path:
        workspace = self.workspace_for(context_id)
        workspace.mkdir(parents=True, exist_ok=True)
        self.sync_skills(workspace)
        self.write_device_config(workspace, device)
        return workspace

    def sync_skills(
        self,
        workspace: Path,
        enabled_skills: list[str] | tuple[str, ...] | None = None,
    ) -> list[str]:
        source = self.settings.skills_dir
        destination = workspace / ".claude" / "skills"
        destination.mkdir(parents=True, exist_ok=True)
        requested = tuple(
            self.settings.enabled_skills if enabled_skills is None else enabled_skills
        )
        requested_set = set(requested)
        for existing in destination.iterdir():
            if existing.is_dir() and existing.name not in requested_set:
                shutil.rmtree(existing)
        names: list[str] = []
        for name in requested:
            skill_source = source / name
            if not (skill_source / "SKILL.md").is_file():
                continue
            skill_destination = destination / name
            if skill_destination.exists():
                shutil.rmtree(skill_destination)
            shutil.copytree(
                skill_source,
                skill_destination,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"),
            )
            scripts_dir = skill_destination / "scripts"
            if scripts_dir.is_dir():
                for script in scripts_dir.rglob("*"):
                    if not script.is_file():
                        continue
                    try:
                        with script.open("rb") as stream:
                            has_shebang = stream.read(2) == b"#!"
                    except OSError:
                        continue
                    if has_shebang:
                        script.chmod(script.stat().st_mode | stat.S_IXUSR)
            names.append(name)

        allowed_domains = ["mcp.m5stack.com"]
        push_host = urlsplit(self.settings.device_push_base_url).hostname
        if push_host and push_host not in allowed_domains:
            allowed_domains.append(push_host)
        settings_file = workspace / ".claude" / "settings.json"
        settings_file.write_text(
            json.dumps(
                {
                    "sandbox": {
                        "enabled": self.settings.claude_sandbox_enabled,
                        "autoAllowBashIfSandboxed": True,
                        "allowUnsandboxedCommands": False,
                        "network": {"allowedDomains": allowed_domains},
                    }
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return names

    def write_device_config(
        self,
        workspace: Path,
        device: dict[str, Any],
        *,
        expose_target: bool = False,
    ) -> None:
        config_dir = workspace / ".aiflow"
        config_dir.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "baseUrl": self.settings.device_push_base_url,
            "timeout": self.settings.device_push_timeout,
        }
        if expose_target:
            payload["defaultDeviceId"] = device["device_id"]
            payload["clientId"] = device["client_id"]
        (config_dir / "config.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def save_message_attachments(
        self,
        workspace: Path,
        conversation_id: str,
        task_id: str,
        attachments: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if len(attachments) > self.settings.max_attachments:
            raise AttachmentError(
                "too_many_attachments",
                f"at most {self.settings.max_attachments} attachments are allowed",
                413,
            )

        decoded: list[tuple[bytes, dict[str, Any], str]] = []
        used_names: set[str] = set()
        total_size = 0
        for index, item in enumerate(attachments, start=1):
            kind = str(item["kind"])
            mime_type = str(item["mime_type"]).strip().lower()
            expected_prefix = "image/" if kind == "image" else "audio/"
            if mime_type not in ATTACHMENT_EXTENSIONS or not mime_type.startswith(
                expected_prefix
            ):
                raise AttachmentError(
                    "unsupported_attachment_type",
                    f"unsupported {kind} MIME type: {mime_type}",
                )
            name = validate_attachment_name(str(item["name"]), mime_type, index)
            name_key = unicodedata.normalize("NFC", name).casefold()
            if name_key in used_names:
                raise AttachmentError(
                    "duplicate_attachment_name",
                    f"attachment {index} name duplicates another attachment in this message",
                )
            used_names.add(name_key)
            try:
                encoded = str(item["data_base64"]).encode("ascii")
                content = base64.b64decode(encoded, validate=True)
            except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
                raise AttachmentError(
                    "invalid_attachment_base64",
                    f"attachment {index} is not valid Base64",
                ) from exc
            if not content:
                raise AttachmentError("empty_attachment", f"attachment {index} is empty")
            if len(content) > self.settings.max_attachment_bytes:
                raise AttachmentError(
                    "attachment_too_large",
                    f"attachment {index} exceeds the per-file limit",
                    413,
                )
            total_size += len(content)
            if total_size > self.settings.max_attachment_total_bytes:
                raise AttachmentError(
                    "attachments_too_large",
                    "attachments exceed the total message limit",
                    413,
                )
            decoded.append((content, item, name))

        if not decoded:
            return []

        directory = workspace / "inputs" / conversation_id / task_id
        directory.mkdir(parents=True, exist_ok=False)
        saved: list[dict[str, Any]] = []
        try:
            for content, item, name in decoded:
                kind = str(item["kind"])
                path = directory / name
                path.write_bytes(content)
                saved.append(
                    {
                        "kind": kind,
                        "mime_type": str(item["mime_type"]).strip().lower(),
                        "path": path.relative_to(workspace).as_posix(),
                        "size": len(content),
                        "sha256": hashlib.sha256(content).hexdigest(),
                        "name": name,
                    }
                )
        except Exception:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        return saved

    def delete_task_inputs(self, workspace: Path, conversation_id: str, task_id: str) -> None:
        directory = workspace / "inputs" / conversation_id / task_id
        if directory.exists():
            shutil.rmtree(directory)

    def safe_path(self, workspace: Path, relative: str) -> Path:
        candidate = (workspace / relative).resolve()
        root = workspace.resolve()
        if candidate != root and root not in candidate.parents:
            raise WorkspaceError("path escapes the client workspace")
        if any(part in {".claude", ".aiflow", ".git"} for part in candidate.relative_to(root).parts):
            raise WorkspaceError("internal workspace files are not exposed")
        return candidate

    def list_files(self, workspace: Path) -> list[dict[str, Any]]:
        files = []
        for path in workspace.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(workspace)
            if relative.parts[0] in {".claude", ".aiflow", ".git"}:
                continue
            stat = path.stat()
            files.append(
                {
                    "path": relative.as_posix(),
                    "size": stat.st_size,
                    "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                }
            )
        return sorted(files, key=lambda item: item["path"])

    def clear_user_files(self, workspace: Path) -> None:
        for child in workspace.iterdir():
            if child.name in {".claude", ".aiflow"}:
                continue
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()

    def delete_context(self, context_id: str) -> None:
        directory = self.settings.clients_dir / context_id
        if directory.exists():
            shutil.rmtree(directory)

    def available_skills(self) -> list[str]:
        if not self.settings.skills_dir.is_dir():
            return []
        return sorted(
            path.name for path in self.settings.skills_dir.iterdir()
            if path.is_dir() and (path / "SKILL.md").is_file()
        )
