from __future__ import annotations

import asyncio
import hashlib
import json
import stat
from dataclasses import replace
from typing import get_args

import pytest

from aiflow_server.agent import (
    DEVICE_PUSH_SKILL,
    M5STACK_ASSISTANT_SKILL,
    M5STACK_MCP_TOOLS,
    SYSTEM_APPEND,
    UIFLOW_CODER_SKILL,
    AgentError,
    _StreamMessageTracker,
    _ToolResultDeduplicator,
    _agent_tools,
    _assistant_block_event,
    _assistant_block_tls_metadata,
    _agent_env,
    _block_image_read,
    _build_prompt,
    _model_capability_hooks,
    _partial_block_event,
    _initial_query_echo_tls_payload,
    _result_message_tls_payload,
    _result_tls_content,
    _sanitize_event,
    _sanitize_tls_event,
    _should_emit_system_message,
    _stream_event_payload,
    _run_skills,
)
from claude_agent_sdk import (
    AssistantMessage,
    RateLimitEvent,
    ResultMessage,
    ServerToolResultBlock,
    ServerToolUseBlock,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from claude_agent_sdk.types import ContentBlock, DeferredToolUse, Message
from aiflow_server.config import load_settings
from aiflow_server.telemetry import TLS_EVENT_DATA_KEY
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
    assert "configured turn limit" in SYSTEM_APPEND
    assert "do not exceed it with optional investigation" in SYSTEM_APPEND
    assert "Avoid repeating tool calls" in SYSTEM_APPEND
    assert "finish the smallest complete deliverable" in SYSTEM_APPEND


def test_system_prompt_uses_user_language_instead_of_tool_context_language():
    assert "user's own most recent identifiable natural-language text or speech" in SYSTEM_APPEND
    assert "explicit user request to use a particular language takes precedence" in SYSTEM_APPEND
    assert "Skills, MCP instructions or responses" in SYSTEM_APPEND
    assert "provide technical evidence, not a language preference" in SYSTEM_APPEND
    assert "Apply the selected language consistently to every public TextBlock" in SYSTEM_APPEND
    assert "Never switch languages merely because" in SYSTEM_APPEND
    assert "use Simplified Chinese as the client default" in SYSTEM_APPEND
    assert "Write for the person, not for the workflow" in SYSTEM_APPEND
    assert "avoid robotic translations" in SYSTEM_APPEND


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


def test_agent_env_sets_context_limit_and_keeps_device_target_scoped(tmp_path):
    settings = load_settings()
    device = {"device_id": "private-device-id", "client_id": "private-client-id"}

    regular_env = _agent_env(settings, tmp_path, device, False)
    assert regular_env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "258000"
    assert "AIFLOW_DEVICE_ID" not in regular_env
    assert "AIFLOW_CLIENT_ID" not in regular_env

    deploy_env = _agent_env(settings, tmp_path, device, True)
    assert deploy_env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "258000"
    assert deploy_env["AIFLOW_DEVICE_ID"] == "private-device-id"
    assert deploy_env["AIFLOW_CLIENT_ID"] == "private-client-id"


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


def test_attachment_only_prompt_does_not_fabricate_an_english_user_language():
    prompt = _build_prompt(
        "",
        {"product": "CoreS3", "device_id": "device-private", "client_id": "client-private"},
        "none",
        [
            {
                "kind": "audio",
                "path": "inputs/question.wav",
                "mime_type": "audio/wav",
                "size": 128,
            }
        ],
        True,
    )

    assert "User request:\n<no user-authored natural-language text in this request>" in prompt
    assert "without treating this English wrapper as the user's language" in prompt
    assert "Inspect the attached message files and respond to their content" not in prompt


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


def test_agent_events_expose_reasoning_but_redact_paths_identifiers_and_credentials(tmp_path):
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
            event={
                "type": "content_block_delta",
                "delta": {
                    "type": "thinking_delta",
                    "thinking": "公开思考 TOKEN=provider-secret-value for private-device-id",
                },
            },
        ),
        workspace,
        secrets,
    )
    assert event_type == "agent_reasoning"
    assert payload["finalized"] is False
    assert payload["thinking"] == "公开思考 TOKEN=<redacted> for <redacted>"
    assert "content_redacted" not in payload


def test_redundant_sdk_fragments_are_suppressed_but_all_thinking_deltas_are_kept(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tracker = _StreamMessageTracker()
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
    second = _stream_event_payload(thinking, workspace, [], tracker)
    assert second is not None and second[0] == "agent_reasoning"
    assert first[1]["thinking"] == second[1]["thinking"] == "hidden"

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


def test_identical_final_text_blocks_claim_distinct_stream_indexes(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tracker = _StreamMessageTracker()
    fixtures = [
        StreamEvent(
            uuid="start",
            session_id="session-1",
            event={"type": "message_start", "message": {"id": "msg-identical"}},
        ),
        StreamEvent(
            uuid="text-start-0",
            session_id="session-1",
            event={
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        ),
        StreamEvent(
            uuid="text-delta-0",
            session_id="session-1",
            event={
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "same"},
            },
        ),
        StreamEvent(
            uuid="text-start-1",
            session_id="session-1",
            event={
                "type": "content_block_start",
                "index": 1,
                "content_block": {"type": "text", "text": ""},
            },
        ),
        StreamEvent(
            uuid="text-delta-1",
            session_id="session-1",
            event={
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "text_delta", "text": "same"},
            },
        ),
    ]
    for message in fixtures:
        _stream_event_payload(message, workspace, [], tracker)

    final_message = AssistantMessage(
        content=[TextBlock(text="same"), TextBlock(text="same")],
        model="test-model",
        message_id="msg-identical",
        session_id="session-1",
    )
    response_id = tracker.assistant_response_id(final_message)
    assert [
        tracker.assistant_block_index(response_id, block, fallback)
        for fallback, block in enumerate(final_message.content)
    ] == [0, 1]


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


def test_final_thinking_is_public_and_keeps_complete_private_tls_payload(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    thinking = "完整隐藏思维 " + ("细节" * 20000) + " secret-device"
    signature = "opaque-provider-signature"
    public_metadata = {
        "source": "claude_sdk",
        "response_id": "msg-thinking",
        "session_id": "session-1",
    }

    event_type, payload = _assistant_block_event(
        ThinkingBlock(thinking=thinking, signature=signature),
        0,
        public_metadata,
        public_metadata,
        workspace,
        ["secret-device"],
    )

    tls_payload = payload.pop(TLS_EVENT_DATA_KEY)
    assert event_type == "agent_reasoning"
    assert payload["finalized"] is True
    assert payload["thinking"].startswith("完整隐藏思维")
    assert len(payload["thinking"]) > 32768
    assert "secret-device" not in payload["thinking"]
    assert signature not in json.dumps(payload)
    assert tls_payload["thinking"].startswith("完整隐藏思维")
    assert len(tls_payload["thinking"]) > 32768
    assert "secret-device" not in tls_payload["thinking"]
    assert signature not in json.dumps(tls_payload)
    assert tls_payload["signature_sha256"] == hashlib.sha256(
        signature.encode("utf-8")
    ).hexdigest()


def test_final_tool_input_keeps_full_private_copy_and_public_limit(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    long_command = "x" * 40000

    event_type, payload = _assistant_block_event(
        ToolUseBlock(
            id="tool-1",
            name="Bash",
            input={"command": long_command},
        ),
        1,
        {"response_id": "msg-tool"},
        {"response_id": "msg-tool"},
        workspace,
        [],
    )

    tls_payload = payload.pop(TLS_EVENT_DATA_KEY)
    assert event_type == "tool_started"
    assert payload["input"]["command"].endswith("characters>")
    assert tls_payload["input"]["command"] == long_command


def test_current_sdk_message_and_content_unions_are_fully_audited():
    assert set(get_args(ContentBlock)) == {
        TextBlock,
        ThinkingBlock,
        ToolUseBlock,
        ToolResultBlock,
        ServerToolUseBlock,
        ServerToolResultBlock,
    }
    assert set(get_args(Message)) == {
        UserMessage,
        AssistantMessage,
        SystemMessage,
        ResultMessage,
        StreamEvent,
        RateLimitEvent,
    }


@pytest.mark.parametrize(
    ("block", "expected_event_type", "expected_block_type", "expected_field"),
    [
        (TextBlock(text="reply"), "assistant_message", "text", "text"),
        (
            ThinkingBlock(thinking="reasoning", signature="signature"),
            "agent_reasoning",
            "thinking",
            "thinking",
        ),
        (
            ToolUseBlock(id="tool-1", name="Read", input={"file_path": "main.py"}),
            "tool_started",
            "tool_use",
            "input",
        ),
        (
            ToolResultBlock(tool_use_id="tool-1", content="done", is_error=False),
            "tool_finished",
            "tool_result",
            "content",
        ),
        (
            ServerToolUseBlock(id="server-1", name="advisor", input={"query": "q"}),
            "server_tool_started",
            "server_tool_use",
            "input",
        ),
        (
            ServerToolResultBlock(tool_use_id="server-1", content={"answer": "a"}),
            "server_tool_finished",
            "server_tool_result",
            "content",
        ),
    ],
)
def test_every_assistant_content_block_has_one_final_tls_record(
    tmp_path,
    block,
    expected_event_type,
    expected_block_type,
    expected_field,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    event_type, public_payload = _assistant_block_event(
        block,
        3,
        {"source": "claude_sdk", "response_id": "msg-matrix"},
        {"source": "claude_sdk", "response_id": "msg-matrix"},
        workspace,
        [],
    )
    tls_payload = public_payload.pop(TLS_EVENT_DATA_KEY)

    assert event_type == expected_event_type
    assert tls_payload["block_type"] == expected_block_type
    assert tls_payload["block_index"] == 3
    assert tls_payload["finalized"] is True
    assert expected_field in tls_payload


def test_complete_result_message_tls_payload_includes_deferred_and_error_fields(tmp_path):
    message = ResultMessage(
        subtype="error_max_turns",
        duration_ms=1000,
        duration_api_ms=900,
        is_error=True,
        num_turns=4,
        session_id="session-1",
        stop_reason="max_turns",
        total_cost_usd=0.25,
        usage={"input_tokens": 10, "output_tokens": 20},
        result="partial result",
        structured_output={"partial": True},
        model_usage={"model-1": {"outputTokens": 20}},
        permission_denials=[{"tool": "Bash"}],
        deferred_tool_use=DeferredToolUse(
            id="deferred-1",
            name="Bash",
            input={"command": "dangerous"},
        ),
        errors=["max turns reached"],
        api_error_status=429,
        uuid="result-uuid",
        terminal_reason="max_turns",
    )

    payload = _result_message_tls_payload(
        message,
        "failed",
        None,
        None,
        tmp_path,
        [],
    )

    assert payload["stage"] == "failed"
    assert payload["is_error"] is True
    assert payload["result"] == "partial result"
    assert payload["duration_api_ms"] == 900
    assert payload["usage"]["output_tokens"] == 20
    assert payload["deferred_tool_use"] == {
        "id": "deferred-1",
        "name": "Bash",
        "input": {"command": "dangerous"},
    }
    assert payload["errors"] == ["max turns reached"]
    assert payload["api_error_status"] == 429


def test_initial_sdk_query_echo_is_marked_as_a_tls_duplicate(tmp_path):
    prompt = "User request:\nwrite code\n"
    assert _initial_query_echo_tls_payload(prompt + "\n", prompt, tmp_path, []) == {
        "duplicate_of": {
            "event_type": "agent_connected",
            "field": "query",
        }
    }
    assert _initial_query_echo_tls_payload("different", prompt, tmp_path, []) is None


def test_stream_tracker_falls_back_to_one_private_partial_capture(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tracker = _StreamMessageTracker()
    messages = [
        StreamEvent(
            uuid="start",
            session_id="session-1",
            event={"type": "message_start", "message": {"id": "msg-partial"}},
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
            uuid="thinking-1",
            session_id="session-1",
            event={
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": "第一段"},
            },
        ),
        StreamEvent(
            uuid="thinking-2",
            session_id="session-1",
            event={
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": "第二段"},
            },
        ),
    ]
    for message in messages:
        _stream_event_payload(message, workspace, [], tracker)

    pending = tracker.pending_partial_blocks()
    assert len(pending) == 1
    assert pending[0]["thinking"] == "第一段第二段"
    event_type, public_payload = _partial_block_event(pending[0], workspace, [])
    private_payload = public_payload.pop(TLS_EVENT_DATA_KEY)
    assert event_type == "agent_partial_capture"
    assert public_payload["thinking"] == "第一段第二段"
    assert public_payload["partial"] is True
    assert public_payload["finalized"] is False
    assert "content_redacted" not in public_payload
    assert private_payload["thinking"] == "第一段第二段"

    tracker.mark_finalized("msg-partial", 0)
    assert tracker.pending_partial_blocks() == []


def test_private_sanitizer_keeps_thinking_but_never_raw_signature(tmp_path):
    sanitized = _sanitize_tls_event(
        {
            "thinking": "可审计思维",
            "signature": "provider-secret-signature",
            "signature_sha256": "abc123",
        },
        tmp_path,
        [],
    )

    assert sanitized["thinking"] == "可审计思维"
    assert sanitized["signature"] == "<redacted>"
    assert sanitized["signature_sha256"] == "abc123"


def test_sanitizer_keeps_usage_counts_but_redacts_credential_tokens(tmp_path):
    sanitized = _sanitize_tls_event(
        {
            "input_tokens": 10,
            "output_tokens": 20,
            "max_tokens": 30,
            "token_count": 40,
            "auth_token": "secret-auth-token",
            "authToken": "secret-camel-token",
            "accessKey": "secret-access-key",
            "stream_token": "secret-stream-token",
            "token": "secret-generic-token",
        },
        tmp_path,
        [],
    )

    assert sanitized["input_tokens"] == 10
    assert sanitized["output_tokens"] == 20
    assert sanitized["max_tokens"] == 30
    assert sanitized["token_count"] == 40
    assert sanitized["auth_token"] == "<redacted>"
    assert sanitized["authToken"] == "<redacted>"
    assert sanitized["accessKey"] == "<redacted>"
    assert sanitized["stream_token"] == "<redacted>"
    assert sanitized["token"] == "<redacted>"


def test_sanitizer_redacts_mac_from_public_payload(tmp_path):
    sanitized = _sanitize_tls_event(
        {"mac_address": "AA:BB:CC:DD:EE:FF", "message": "AA:BB:CC:DD:EE:FF"},
        tmp_path,
        ["AA:BB:CC:DD:EE:FF"],
    )

    assert sanitized["mac_address"] == "<redacted>"
    assert sanitized["message"] == "<redacted>"


def test_block_tls_metadata_does_not_repeat_message_summary_fields():
    metadata = {
        "source": "claude_sdk",
        "response_id": "msg-1",
        "message_id": "msg-1",
        "message_uuid": "uuid-1",
        "parent_tool_use_id": None,
        "model": "model-1",
        "session_id": "session-1",
        "stop_reason": "end_turn",
        "usage": {"output_tokens": 10},
    }

    assert _assistant_block_tls_metadata(metadata) == {
        "source": "claude_sdk",
        "response_id": "msg-1",
        "message_id": "msg-1",
        "message_uuid": "uuid-1",
        "parent_tool_use_id": None,
    }


def test_result_payload_references_identical_final_reply_instead_of_copying_it(tmp_path):
    duplicate = _result_tls_content(
        "最终回复\n",
        "最终回复",
        "msg-final",
        tmp_path,
        [],
    )
    distinct = _result_tls_content(
        "结构化结果",
        "最终回复",
        "msg-final",
        tmp_path,
        [],
    )

    assert "result" not in duplicate
    assert duplicate["result_duplicate_of"] == {
        "event_type": "assistant_message",
        "response_id": "msg-final",
    }
    assert len(duplicate["result_sha256"]) == 64
    assert distinct == {"result": "结构化结果"}


def test_tool_result_deduplicator_keeps_distinct_updates_only(tmp_path):
    deduplicator = _ToolResultDeduplicator()
    arguments = ("tool_result", "tool-1", {"content": "done"}, False, tmp_path, [])

    assert deduplicator.accept(*arguments) is True
    assert deduplicator.accept(*arguments) is False
    assert deduplicator.accept(
        "tool_result",
        "tool-1",
        {"content": "updated"},
        False,
        tmp_path,
        [],
    ) is True
