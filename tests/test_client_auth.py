from __future__ import annotations

import json
import secrets
import time
from dataclasses import replace

from fastapi.testclient import TestClient

from aiflow_server.agent import AgentRunResult
from aiflow_server.app import TOKEN_HEADER, create_app
from aiflow_server.config import load_settings
from aiflow_server.security import (
    CONTENT_HASH_HEADER,
    KEY_ID_HEADER,
    NONCE_HEADER,
    RESPONSE_SIGNATURE_HEADER,
    RESPONSE_TIMESTAMP_HEADER,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    body_hash,
    sign_request,
    sign_response,
)


KEY_ID = "official-test-client"
SECRET = bytes(range(32))


class ImmediateRunner:
    async def run(self, task_id, context, prompt, deploy_mode, emit, cancel_event):
        await emit("assistant_message", {"stage": "agent_working", "progress": 55, "text": "done"})
        return AgentRunResult(
            session_id="00000000-0000-4000-8000-000000000099",
            usage={"input_tokens": 1, "output_tokens": 1},
            total_cost_usd=0.001,
            duration_ms=1,
            num_turns=1,
            stop_reason="end_turn",
        )

    async def cancel(self, task_id):
        return None

    async def shutdown(self):
        return None


def signed_headers(method: str, target: str, body: bytes, *, nonce: str | None = None) -> tuple[dict[str, str], str]:
    timestamp = str(int(time.time()))
    request_nonce = nonce or secrets.token_urlsafe(18)
    digest = body_hash(body)
    return (
        {
            "Content-Type": "application/json",
            KEY_ID_HEADER: KEY_ID,
            TIMESTAMP_HEADER: timestamp,
            NONCE_HEADER: request_nonce,
            CONTENT_HASH_HEADER: digest,
            SIGNATURE_HEADER: sign_request(SECRET, method, target, timestamp, request_nonce, digest),
        },
        request_nonce,
    )


def signed_json(client: TestClient, method: str, target: str, payload: dict, extra_headers: dict | None = None):
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers, nonce = signed_headers(method, target, body)
    headers.update(extra_headers or {})
    response = client.request(method, target, content=body, headers=headers)
    return response, nonce


def assert_response_signature(response, nonce: str) -> None:
    timestamp = response.headers[RESPONSE_TIMESTAMP_HEADER]
    expected = sign_response(SECRET, nonce, response.status_code, timestamp)
    assert response.headers[RESPONSE_SIGNATURE_HEADER] == expected


def test_official_client_signature_replay_body_integrity_and_cost_guard(tmp_path):
    base = load_settings()
    settings = replace(
        base,
        data_dir=tmp_path / "data",
        client_auth_enabled=True,
        client_auth_keys=((KEY_ID, SECRET),),
        client_auth_requests_per_minute=20,
        max_ai_tasks_per_client_minute=1,
        max_ai_tasks_per_client_day=10,
        max_ai_tasks_global_day=10,
    )
    app = create_app(settings, runner=ImmediateRunner())

    with TestClient(app) as client:
        capabilities = client.get("/api/v3/capabilities")
        assert capabilities.status_code == 200
        assert capabilities.json()["client_auth"]["enabled"] is True

        payload = {
            "device": {
                "device_id": "device-signed-test",
                "client_id": "client-signed-test",
            }
        }
        unsigned = client.post("/api/v3/contexts", json=payload)
        assert unsigned.status_code == 401
        assert unsigned.json()["detail"]["code"] == "client_signature_required"

        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers, nonce = signed_headers("POST", "/api/v3/contexts", body)
        created = client.post("/api/v3/contexts", content=body, headers=headers)
        assert created.status_code == 201, created.text
        assert_response_signature(created, nonce)

        replayed = client.post("/api/v3/contexts", content=body, headers=headers)
        assert replayed.status_code == 409
        assert replayed.json()["detail"]["code"] == "client_request_replayed"

        tampered = body.replace(b"device-signed-test", b"device-tampered--")
        tampered_headers, _ = signed_headers("POST", "/api/v3/contexts", body)
        mismatch = client.post("/api/v3/contexts", content=tampered, headers=tampered_headers)
        assert mismatch.status_code == 401
        assert mismatch.json()["detail"]["code"] == "content_hash_mismatch"

        context_token = created.json()["access_token"]
        task_payload = {"prompt": "write code", "deploy_mode": "none", "attachments": []}
        first_task, first_nonce = signed_json(
            client,
            "POST",
            "/api/v3/tasks/coding",
            task_payload,
            {TOKEN_HEADER: context_token},
        )
        assert first_task.status_code == 202, first_task.text
        assert_response_signature(first_task, first_nonce)

        limited, limited_nonce = signed_json(
            client,
            "POST",
            "/api/v3/tasks/coding",
            task_payload,
            {TOKEN_HEADER: context_token},
        )
        assert limited.status_code == 429
        assert limited.json()["detail"]["code"] == "ai_task_limit_client_minute"
        assert_response_signature(limited, limited_nonce)

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if app.state.storage.get_task(first_task.json()["task_id"])["status"] == "completed":
                break
            time.sleep(0.01)
        assert app.state.storage.get_task(first_task.json()["task_id"])["status"] == "completed"
