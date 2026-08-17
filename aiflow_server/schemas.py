from __future__ import annotations

from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class DeviceInfo(StrictModel):
    device_id: str = Field(
        min_length=1,
        max_length=256,
        validation_alias=AliasChoices("device_id", "deviceId"),
    )
    client_id: str | None = Field(
        default=None,
        max_length=256,
        validation_alias=AliasChoices("client_id", "clientId", "push_client_id"),
    )
    mac_address: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        validation_alias=AliasChoices("mac_address", "macAddress", "mac"),
    )
    product: str | None = Field(default=None, max_length=200)
    firmware_version: str | None = Field(default=None, max_length=100)
    capabilities: dict[str, Any] = Field(default_factory=dict)

    @field_validator("device_id", "client_id", "mac_address")
    @classmethod
    def validate_identifier(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized or any(ord(character) < 32 for character in normalized):
            raise ValueError("identifier must be non-empty and contain no control characters")
        return normalized


class ConnectDeviceInfo(DeviceInfo):
    client_id: str = Field(
        min_length=1,
        max_length=256,
        validation_alias=AliasChoices("client_id", "clientId", "push_client_id"),
    )


class CreateContextRequest(StrictModel):
    label: str = Field(default="Web UIFlow client", min_length=1, max_length=200)
    device: ConnectDeviceInfo


class UpdateDeviceRequest(StrictModel):
    mac_address: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        validation_alias=AliasChoices("mac_address", "macAddress", "mac"),
    )
    product: str | None = Field(default=None, max_length=200)
    firmware_version: str | None = Field(default=None, max_length=100)
    capabilities: dict[str, Any] | None = None


class Base64Attachment(StrictModel):
    kind: Literal["image", "audio"]
    mime_type: str = Field(min_length=1, max_length=100)
    data_base64: str = Field(min_length=1, max_length=30_000_000)
    name: str = Field(
        min_length=1,
        max_length=255,
        description="Client-provided file name used when the attachment is saved",
    )


class ContextResponse(StrictModel):
    context_id: str
    device_id: str
    client_id: str
    mac_address: str | None = None
    access_token: str
    conversation_id: str
    label: str
    device: DeviceInfo
    created_at: str
    model: str
    created: bool
    system_status: dict[str, Any]


class ContextInfoResponse(StrictModel):
    context_id: str
    device_id: str
    client_id: str | None
    mac_address: str | None = None
    conversation_id: str
    label: str
    device: DeviceInfo
    created_at: str
    updated_at: str
    model: str
    active_task_id: str | None = None


class CodingTaskRequest(StrictModel):
    prompt: str = Field(default="", max_length=30000)
    attachments: list[Base64Attachment] = Field(default_factory=list, max_length=20)
    deploy_mode: Literal["none", "server", "agent"] = "none"

    @model_validator(mode="after")
    def require_content(self) -> "CodingTaskRequest":
        if not self.prompt.strip() and not self.attachments:
            raise ValueError("prompt or at least one image/audio attachment is required")
        return self


class DirectRunRequest(StrictModel):
    code_path: str = Field(default="main.py", min_length=1, max_length=500)
    include_resources: bool = True


class ResetConversationRequest(StrictModel):
    keep_files: bool = True


class TaskCreatedResponse(StrictModel):
    task_id: str
    device_id: str
    kind: Literal["coding", "direct_deploy"]
    status: str
    status_url: str
    events_url: str
    stream_token: str
    queue_position: int | None
    system_status: dict[str, Any]


class TaskStatusResponse(StrictModel):
    task_id: str
    device_id: str
    kind: str
    status: str
    stage: str
    progress: int
    created_at: str
    updated_at: str
    started_at: str | None
    finished_at: str | None
    session_id: str | None
    result: dict[str, Any] | None
    error: dict[str, Any] | None
    cancel_requested: bool
    heartbeat_age_seconds: float | None
    agent_silence_seconds: float | None
    possibly_stalled: bool
    queue_position: int | None
    last_event: dict[str, Any] | None


class FileInfo(StrictModel):
    path: str
    size: int
    modified_at: str
