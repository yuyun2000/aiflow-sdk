from __future__ import annotations

import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

from fastapi.testclient import TestClient

from aiflow_server.agent import AgentRunResult
from aiflow_server.app import TOKEN_HEADER
from aiflow_server.config import load_settings
from aiflow_server.gateway import GatewayRateLimiter, WEB_SESSION_COOKIE, create_gateway_app
from aiflow_server.security import SIGNATURE_HEADER


ORIGIN_HEADERS = {"Origin": "http://testserver"}


class GatewayRunner:
    async def run(self, task_id, context, prompt, deploy_mode, emit, cancel_event):
        await emit(
            "assistant_message",
            {"stage": "agent_working", "progress": 55, "text": "gateway-safe output"},
        )
        return AgentRunResult(
            session_id="00000000-0000-4000-8000-000000000077",
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


def wait_terminal(client: TestClient, task_id: str, headers: dict[str, str]) -> dict:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        response = client.get(f"/api/v3/tasks/{task_id}", headers=headers)
        assert response.status_code == 200, response.text
        payload = response.json()
        if payload["status"] in {"completed", "failed", "cancelled"}:
            return payload
        time.sleep(0.01)
    raise AssertionError("gateway task did not finish")


def test_anonymous_gateway_keeps_core_secret_server_side_and_limits_ai(tmp_path):
    base = load_settings()
    settings = replace(
        base,
        data_dir=tmp_path / "data",
        web_require_same_origin=True,
        web_cookie_secure=False,
        web_requests_per_session_minute=50,
        web_requests_per_ip_minute=100,
        web_ai_tasks_per_session_minute=1,
        web_ai_tasks_per_session_day=10,
        web_ai_tasks_per_ip_day=10,
    )
    app = create_gateway_app(settings, runner=GatewayRunner())

    with TestClient(app) as client:
        capabilities = client.get("/api/v3/capabilities")
        assert capabilities.status_code == 200
        assert capabilities.json()["client_auth"] == {
            "enabled": False,
            "mode": "server_bff",
            "browser_holds_secret": False,
            "core_authenticated": True,
        }
        assert WEB_SESSION_COOKIE in client.cookies
        assert "HttpOnly" in capabilities.headers["set-cookie"]

        rejected = client.post(
            "/api/v3/contexts",
            json={"device": {"device_id": "gateway-device", "client_id": "gateway-client"}},
        )
        assert rejected.status_code == 403
        assert rejected.json()["detail"]["code"] == "cross_site_request_rejected"

        created = client.post(
            "/api/v3/contexts",
            json={"device": {"device_id": "gateway-device", "client_id": "gateway-client"}},
            headers={**ORIGIN_HEADERS, SIGNATURE_HEADER: "browser-forged-signature"},
        )
        assert created.status_code == 201, created.text
        assert "X-AIFlow-Response-Signature" not in created.headers
        token_headers = {TOKEN_HEADER: created.json()["access_token"]}

        task = client.post(
            "/api/v3/tasks/coding",
            json={"prompt": "write code", "deploy_mode": "none", "attachments": []},
            headers={**ORIGIN_HEADERS, **token_headers},
        )
        assert task.status_code == 202, task.text
        task_id = task.json()["task_id"]
        assert wait_terminal(client, task_id, token_headers)["status"] == "completed"

        history = client.get(
            f"/api/v3/tasks/{task_id}/events/history",
            headers=token_headers,
        )
        assert history.status_code == 200
        assert any(event["type"] == "assistant_message" for event in history.json()["events"])

        stream = client.get(
            task.json()["events_url"],
            params={"stream_token": task.json()["stream_token"]},
        )
        assert stream.status_code == 200
        assert "event: assistant_message\n" in stream.text
        assert "gateway-safe output" in stream.text

        limited = client.post(
            "/api/v3/tasks/coding",
            json={"prompt": "write more code", "deploy_mode": "none", "attachments": []},
            headers={**ORIGIN_HEADERS, **token_headers},
        )
        assert limited.status_code == 429
        assert limited.json()["detail"]["code"] == "web_rate_limit_ai_session_minute"
        assert limited.json()["detail"]["scope"] == "session"
        assert 1 <= limited.json()["detail"]["retry_after_seconds"] <= 60
        assert limited.headers["Retry-After"] == str(
            limited.json()["detail"]["retry_after_seconds"]
        )


def test_anonymous_session_request_limit_blocks_mechanical_refresh(tmp_path):
    base = load_settings()
    settings = replace(
        base,
        data_dir=tmp_path / "data",
        web_requests_per_session_minute=2,
        web_requests_per_ip_minute=100,
    )
    app = create_gateway_app(settings, runner=GatewayRunner())

    with TestClient(app) as client:
        assert client.get("/api/v3/capabilities").status_code == 200
        assert client.get("/api/v3/system/status").status_code == 200

        limited = client.get("/api/v3/system/status")

        assert limited.status_code == 429
        assert limited.json()["detail"]["code"] == "web_rate_limit_session_minute"
        assert limited.json()["detail"]["scope"] == "session"
        assert int(limited.headers["Retry-After"]) >= 1


def test_request_rate_counters_are_memory_only_and_thread_safe(tmp_path):
    limiter = GatewayRateLimiter(tmp_path / "gateway.sqlite3")
    now = 1785460000
    counters = [("session:test", "request-minute", now - now % 60, 5, "session_minute")]

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(lambda _: limiter.claim_requests(counters, now), range(20)))

    assert results.count("ok") == 5
    assert results.count("session_minute") == 15
    with sqlite3.connect(limiter.database_path) as db:
        assert db.execute("SELECT COUNT(*) FROM web_rate_counters").fetchone()[0] == 0


def test_ai_rate_counters_remain_persistent(tmp_path):
    limiter = GatewayRateLimiter(tmp_path / "gateway.sqlite3")
    now = int(time.time())
    counters = [("session:test", "ai-day", now - now % 86400, 1, "ai_session_day")]

    assert limiter.claim_persistent(counters) == "ok"
    assert limiter.claim_persistent(counters) == "ai_session_day"
    with sqlite3.connect(limiter.database_path) as db:
        row = db.execute(
            "SELECT counter_type, count FROM web_rate_counters"
        ).fetchone()
    with limiter.connect() as db:
        synchronous = db.execute("PRAGMA synchronous").fetchone()[0]
    assert row == ("ai-day", 1)
    assert synchronous == 1


def test_slow_ai_quota_storage_does_not_block_event_loop(tmp_path):
    base = load_settings()
    settings = replace(base, data_dir=tmp_path / "data")
    app = create_gateway_app(settings, runner=GatewayRunner())
    quota_started = threading.Event()

    def slow_claim(_counters):
        quota_started.set()
        time.sleep(0.4)
        return "ai_session_minute"

    app.state.rate_limiter.claim_persistent = slow_claim
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(
            client.post,
            "/api/v3/tasks/coding",
            json={"prompt": "blocked before submission", "deploy_mode": "none", "attachments": []},
            headers=ORIGIN_HEADERS,
        )
        assert quota_started.wait(timeout=1)
        started = time.monotonic()
        health = client.get("/health")
        elapsed = time.monotonic() - started
        limited = pending.result(timeout=2)

    assert health.status_code == 200
    assert elapsed < 0.2
    assert limited.status_code == 429
    assert limited.json()["detail"]["code"] == "web_rate_limit_ai_session_minute"
