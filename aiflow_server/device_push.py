from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

from .config import Settings
from .workspaces import WorkspaceError, WorkspaceManager


class DeploymentError(RuntimeError):
    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class DevicePusher:
    def __init__(self, settings: Settings, workspaces: WorkspaceManager):
        self.settings = settings
        self.workspaces = workspaces
        self._module = self._load_skill_module()

    def _load_skill_module(self) -> ModuleType:
        path = self.settings.skills_dir / "aiflow-device-push" / "scripts" / "aiflow_push.py"
        if not path.is_file():
            raise DeploymentError("push_skill_missing", "aiflow-device-push script is not installed")
        spec = importlib.util.spec_from_file_location("aiflow_device_push_runtime", path)
        if spec is None or spec.loader is None:
            raise DeploymentError("push_skill_invalid", "cannot load aiflow-device-push script")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _resolve_device_id(self, device: dict[str, Any]) -> str:
        device_id = str(device.get("device_id") or "").strip()
        if not device_id:
            raise DeploymentError(
                "device_target_missing",
                "paired client did not provide the required platform deviceId",
            )
        return device_id

    def _resolve_client_id(self, device: dict[str, Any]) -> str:
        client_id = str(device.get("client_id") or "").strip()
        if not client_id:
            raise DeploymentError(
                "client_target_missing",
                "paired client did not provide the required clientId",
            )
        return client_id

    def _resources_from_manifest(self, workspace: Path, code_candidate: Path) -> list[str]:
        manifest_path = workspace / ".aiflow" / "deploy.json"
        if not manifest_path.exists():
            return []
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise DeploymentError("invalid_deploy_manifest", f"cannot read .aiflow/deploy.json: {exc}") from exc
        resources = payload.get("resources", []) if isinstance(payload, dict) else None
        if not isinstance(resources, list):
            raise DeploymentError("invalid_deploy_manifest", "deploy manifest resources must be an array")
        values: list[str] = []
        for entry in resources:
            if not isinstance(entry, dict) or not isinstance(entry.get("file"), str):
                raise DeploymentError("invalid_deploy_manifest", "each resource requires a file string")
            try:
                path = self.workspaces.safe_path(workspace, entry["file"])
            except WorkspaceError as exc:
                raise DeploymentError("invalid_deploy_manifest", str(exc)) from exc
            device_path = entry.get("devicePath", "")
            if not isinstance(device_path, str):
                raise DeploymentError("invalid_deploy_manifest", "resource devicePath must be a string")
            if path == code_candidate:
                continue
            values.append(str(path) + ("::" + device_path if device_path else ""))
        return values

    def _prepare(
        self,
        context: dict[str, Any],
        code_path: str,
        include_resources: bool,
    ) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
        workspace = self.workspaces.workspace_for(context["context_id"])
        try:
            code_candidate = self.workspaces.safe_path(workspace, code_path)
        except WorkspaceError as exc:
            raise DeploymentError("invalid_code_path", str(exc)) from exc
        try:
            code = self._module.validate_code(str(code_candidate))
            resources = self._module.validate_resources(
                self._resources_from_manifest(workspace, code_candidate) if include_resources else []
            )
        except self._module.PushError as exc:
            raise DeploymentError("deployment_validation_failed", str(exc)) from exc
        device = context["device"]
        settings = {
            "base_url": self.settings.device_push_base_url,
            "device_id": self._resolve_device_id(device),
            "client_id": self._resolve_client_id(device),
            "timeout": self.settings.device_push_timeout,
        }
        return settings, code, resources

    async def plan(self, context: dict[str, Any], code_path: str, include_resources: bool) -> dict[str, Any]:
        settings, code, resources = self._prepare(context, code_path, include_resources)
        return self._module.build_plan("direct_deploy", settings, code, resources)

    async def deploy(
        self,
        context: dict[str, Any],
        code_path: str = "main.py",
        include_resources: bool = True,
    ) -> dict[str, Any]:
        settings, code, resources = self._prepare(context, code_path, include_resources)

        def execute() -> dict[str, Any]:
            steps = []
            try:
                if resources:
                    steps.append(self._module.push_resources(settings, resources))
                steps.append(self._module.push_code(settings, code))
            except self._module.PushError as exc:
                raise DeploymentError("device_push_failed", str(exc), retryable=False) from exc
            return {"ok": True, "action": "direct_deploy", "steps": steps}

        return await asyncio.to_thread(execute)
