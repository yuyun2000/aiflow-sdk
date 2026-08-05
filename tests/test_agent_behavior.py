from __future__ import annotations

import asyncio
import json
import stat
from dataclasses import replace

import pytest

from aiflow_server.agent import (
    DEVICE_PUSH_SKILL,
    M5STACK_ASSISTANT_SKILL,
    M5STACK_MCP_TOOLS,
    SYSTEM_APPEND,
    UIFLOW_CODER_SKILL,
    AgentError,
    _StreamMessageTracker,
    _agent_tools,
    _block_image_read,
    _build_prompt,
    _model_capability_hooks,
    _sanitize_event,
    _should_emit_system_message,
    _stream_event_payload,
    _run_skills,
)
from claude_agent_sdk import AssistantMessage, StreamEvent, SystemMessage, TextBlock
from aiflow_server.config import load_settings
from aiflow_server.workspaces import WorkspaceManager


def test_system_prompt_keeps_domain_and_makes_skill_order_advisory():
    assert "only handles M5Stack UIFlow2/MicroPython programming" in SYSTEM_APPEND
    assert "Prefer consulting the uiflow2-coder Skill" in SYSTEM_APPEND
    assert "recommendation, not a tool-order gate" in SYSTEM_APPEND
    assert "It may be used before uiflow2-coder" in SYSTEM_APPEND
    assert "first tool call must invoke" not in SYSTEM_APPEND
    assert "knowledge_feedback" in SYSTEM_APPEND
    assert "receiving a feedback_id" in SYSTEM_APPEND
    assert "ordinary user-code bugs" in SYSTEM_APPEND
    assert "never list main.py, main_ota_temp.py" in SYSTEM_APPEND
    assert "final modifying stage" in SYSTEM_APPEND
    assert "plain-text assistant message as a public progress record" in SYSTEM_APPEND
    assert "MUST begin with a plain-text TextBlock before any ToolUseBlock" in SYSTEM_APPEND
    assert "Never begin a tool-using turn with a tool call" in SYSTEM_APPEND
    assert "fact or hypothesis being checked" in SYSTEM_APPEND
    assert "Never emit placeholders" in SYSTEM_APPEND


def test_skill_selection_exposes_device_push_only_for_agent_mode():
    configured = (M5STACK_ASSISTANT_SKILL, UIFLOW_CODER_SKILL, DEVICE_PUSH_SKILL, "other-skill")

    expected_coding = [UIFLOW_CODER_SKILL, M5STACK_ASSISTANT_SKILL]
    assert _run_skills(configured, "none") == expected_coding
    assert _run_skills(configured, "server") == expected_coding
    assert _run_skills(configured, "agent") == [*expected_coding, DEVICE_PUSH_SKILL]

    with pytest.raises(AgentError, match="aiflow-device-push") as caught:
        _run_skills((UIFLOW_CODER_SKILL, M5STACK_ASSISTANT_SKILL), "agent")
    assert caught.value.code == "required_skill_missing"


def test_runtime_tool_policy_enables_skill_and_official_mcp_tools():
    skills = [UIFLOW_CODER_SKILL, M5STACK_ASSISTANT_SKILL]
    tools, allowed = _agent_tools(("Read", "Write", "Bash", "Read"), skills, True)

    assert tools == ["Read", "Write", "Bash", "Skill"]
    assert allowed[:3] == ["Read", "Write", "Bash"]
    assert tuple(allowed[3:]) == M5STACK_MCP_TOOLS

    _, mcp_disabled = _agent_tools(("Read",), skills, False)
    assert all(tool not in mcp_disabled for tool in M5STACK_MCP_TOOLS)


def test_prompt_keeps_device_id_private_and_separates_deployment_modes():
    device = {
        "device_id": "private-device-id",
        "client_id": "private-client-id",
        "product": "CoreS3",
        "firmware_version": "2.3.1",
    }

    none_prompt = _build_prompt("写一个按钮程序", device, "none", [], True)
    server_prompt = _build_prompt("写一个按钮程序", device, "server", [], True)
    agent_prompt = _build_prompt("写一个按钮程序", device, "agent", [], True)

    assert "private-device-id" not in none_prompt + server_prompt + agent_prompt
    assert "private-client-id" not in none_prompt + server_prompt + agent_prompt
    assert '"paired": true' in agent_prompt
    assert '"client_paired": true' in agent_prompt
    assert "Do not push to a device" in none_prompt
    assert "service will deploy main.py" in server_prompt
    assert "run plan exactly once" in agent_prompt
    assert "one --execute deployment" in agent_prompt


def test_text_only_model_prompt_keeps_images_as_opaque_uiflow_resources():
    prompt = _build_prompt(
        "显示 logo.png",
        {"device_id": "private-device-id", "client_id": "private-client-id"},
        "server",
        [
            {
                "kind": "image",
                "path": "inputs/conversation/task/logo.png",
                "mime_type": "image/png",
                "size": 1234,
            }
        ],
        True,
        False,
    )

    assert "Image input is disabled" in prompt
    assert "never call Read" in prompt
    assert "reference that path from UIFlow2 code" in prompt
    assert "inputs/conversation/task/logo.png" in prompt


def test_text_only_model_hook_denies_only_image_reads():
    async def exercise():
        denied = await _block_image_read(
            {"tool_input": {"file_path": "inputs/task/screen.PNG"}},
            "tool-use-id",
            {},
        )
        allowed = await _block_image_read(
            {"tool_input": {"file_path": "skills/uiflow2-coder/docs/m5ui/image.md"}},
            "tool-use-id",
            {},
        )
        return denied, allowed

    denied, allowed = asyncio.run(exercise())

    decision = denied["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert "opaque UIFlow2 resource" in decision["permissionDecisionReason"]
    assert allowed == {}
    assert _model_capability_hooks(True) is None
    disabled_hooks = _model_capability_hooks(False)
    assert disabled_hooks is not None
    assert disabled_hooks["PreToolUse"][0].matcher == "Read"


def test_workspace_hides_target_and_prunes_push_skill_without_authorization(tmp_path):
    base = load_settings()
    settings = replace(base, data_dir=tmp_path / "data")
    workspaces = WorkspaceManager(settings)
    workspace = workspaces.initialize(
        "ctx_agent_policy",
        {"device_id": "private-device-id", "client_id": "private-client-id"},
    )

    hidden_config = json.loads(workspace.joinpath(".aiflow", "config.json").read_text())
    assert "defaultDeviceId" not in hidden_config
    assert "clientId" not in hidden_config

    selected = workspaces.sync_skills(
        workspace,
        [UIFLOW_CODER_SKILL, M5STACK_ASSISTANT_SKILL],
    )
    assert selected == [UIFLOW_CODER_SKILL, M5STACK_ASSISTANT_SKILL]
    assert not workspace.joinpath(".claude", "skills", DEVICE_PUSH_SKILL).exists()

    workspaces.write_device_config(
        workspace,
        {"device_id": "private-device-id", "client_id": "private-client-id"},
        expose_target=True,
    )
    exposed_config = json.loads(workspace.joinpath(".aiflow", "config.json").read_text())
    assert exposed_config["defaultDeviceId"] == "private-device-id"
    assert exposed_config["clientId"] == "private-client-id"


def test_workspace_sync_restores_shebang_script_execute_permission(tmp_path):
    skills_dir = tmp_path / "skills"
    skill = skills_dir / "test-skill"
    script = skill / "scripts" / "run.sh"
    script.parent.mkdir(parents=True)
    skill.joinpath("SKILL.md").write_text("---\nname: test-skill\n---\n", encoding="utf-8")
    script.write_text("#!/usr/bin/env bash\necho ok\n", encoding="utf-8")
    script.chmod(0o644)

    base = load_settings()
    settings = replace(
        base,
        data_dir=tmp_path / "data",
        skills_dir=skills_dir,
        enabled_skills=("test-skill",),
    )
    workspaces = WorkspaceManager(settings)
    workspace = workspaces.workspace_for("ctx-script-mode")
    workspace.mkdir(parents=True)

    assert workspaces.sync_skills(workspace) == ["test-skill"]
    copied = workspace / ".claude" / "skills" / "test-skill" / "scripts" / "run.sh"
    assert copied.stat().st_mode & stat.S_IXUSR


def test_agent_events_redact_reasoning_paths_identifiers_and_credentials(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    secrets = ["private-device-id", "private-client-id", "provider-secret-value"]

    sanitized = _sanitize_event(
        {
            "command": f"cd {workspace} && TOKEN=provider-secret-value run private-device-id",
            "api_key": "must-not-leak",
            "nested": {"clientId": "private-client-id"},
        },
        workspace,
        secrets,
    )
    encoded = json.dumps(sanitized)
    assert str(workspace) not in encoded
    assert "private-device-id" not in encoded
    assert "private-client-id" not in encoded
    assert "provider-secret-value" not in encoded
    assert "must-not-leak" not in encoded

    event_type, payload = _stream_event_payload(
        StreamEvent(
            uuid="message-1",
            session_id="session-1",
            event={"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "hidden chain"}},
        ),
        workspace,
        secrets,
    )
    assert event_type == "agent_reasoning"
    assert payload["content_redacted"] is True
    assert "hidden chain" not in json.dumps(payload)


def test_redundant_high_frequency_sdk_events_are_suppressed(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tracker = _StreamMessageTracker()
    monkeypatch.setattr("aiflow_server.agent.time.monotonic", lambda: 100.0)

    input_fragment = StreamEvent(
        uuid="input-fragment",
        session_id="session-1",
        event={
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "input_json_delta", "partial_json": '{"query":'},
        },
    )
    assert _stream_event_payload(input_fragment, workspace, [], tracker) is None

    thinking = StreamEvent(
        uuid="thinking-fragment",
        session_id="session-1",
        event={
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "hidden"},
        },
    )
    first = _stream_event_payload(thinking, workspace, [], tracker)
    assert first is not None and first[0] == "agent_reasoning"
    assert _stream_event_payload(thinking, workspace, [], tracker) is None

    signature = StreamEvent(
        uuid="signature-fragment",
        session_id="session-1",
        event={
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "signature_delta", "signature": "hidden"},
        },
    )
    assert _stream_event_payload(signature, workspace, [], tracker) is None
    assert not _should_emit_system_message(SystemMessage(subtype="thinking_tokens", data={}))
    assert _should_emit_system_message(SystemMessage(subtype="init", data={}))


def test_partial_and_final_assistant_messages_share_response_and_block_identity(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tracker = _StreamMessageTracker()

    stream_messages = [
        StreamEvent(
            uuid="stream-start-uuid",
            session_id="session-1",
            event={"type": "message_start", "message": {"id": "msg-api-1"}},
        ),
        StreamEvent(
            uuid="stream-delta-uuid-1",
            session_id="session-1",
            event={
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "第一段"},
            },
        ),
        StreamEvent(
            uuid="stream-delta-uuid-2",
            session_id="session-1",
            event={
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "第二段"},
            },
        ),
    ]

    parsed = [
        _stream_event_payload(message, workspace, [], tracker)
        for message in stream_messages
    ]
    assert [payload["response_id"] for _, payload in parsed] == ["msg-api-1"] * 3
    assert [payload["block_index"] for _, payload in parsed[1:]] == [0, 0]
    assert [payload["finalized"] for _, payload in parsed[1:]] == [False, False]

    final_message = AssistantMessage(
        content=[TextBlock(text="第一段第二段")],
        model="test-model",
        message_id="msg-api-1",
        session_id="session-1",
        uuid="different-final-uuid",
    )
    assert tracker.assistant_response_id(final_message) == "msg-api-1"


def test_final_text_uses_raw_index_after_thinking_block(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tracker = _StreamMessageTracker()
    stream_messages = [
        StreamEvent(
            uuid="start",
            session_id="session-1",
            event={"type": "message_start", "message": {"id": "msg-api-2"}},
        ),
        StreamEvent(
            uuid="thinking-start",
            session_id="session-1",
            event={
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "thinking", "thinking": ""},
            },
        ),
        StreamEvent(
            uuid="text-start",
            session_id="session-1",
            event={
                "type": "content_block_start",
                "index": 1,
                "content_block": {"type": "text", "text": ""},
            },
        ),
        StreamEvent(
            uuid="text-delta",
            session_id="session-1",
            event={
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "text_delta", "text": "最终回复"},
            },
        ),
    ]
    for message in stream_messages:
        _stream_event_payload(message, workspace, [], tracker)

    final_message = AssistantMessage(
        content=[TextBlock(text="最终回复")],
        model="test-model",
        message_id="msg-api-2",
        session_id="session-1",
        uuid="final",
    )
    response_id = tracker.assistant_response_id(final_message)
    assert tracker.assistant_block_index(response_id, final_message.content[0], 0) == 1


def test_stream_tracker_keeps_root_and_nested_tool_responses_separate(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tracker = _StreamMessageTracker()

    fixtures = [
        StreamEvent(
            uuid="root-start",
            session_id="session-1",
            event={"type": "message_start", "message": {"id": "msg-root"}},
        ),
        StreamEvent(
            uuid="nested-start",
            session_id="session-1",
            parent_tool_use_id="tool-parent-1",
            event={"type": "message_start", "message": {"id": "msg-nested"}},
        ),
        StreamEvent(
            uuid="root-delta",
            session_id="session-1",
            event={
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "root"},
            },
        ),
        StreamEvent(
            uuid="nested-delta",
            session_id="session-1",
            parent_tool_use_id="tool-parent-1",
            event={
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "nested"},
            },
        ),
    ]

    parsed = [
        _stream_event_payload(message, workspace, [], tracker)[1]
        for message in fixtures
    ]
    assert [payload["response_id"] for payload in parsed] == [
        "msg-root",
        "msg-nested",
        "msg-root",
        "msg-nested",
    ]
