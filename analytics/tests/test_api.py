from __future__ import annotations

from dataclasses import replace

from fastapi.testclient import TestClient

from aiflow_analytics.app import create_app

from .fixtures import complete_turn


class UnconfiguredTLSClient:
    configured = False


class PassiveSync:
    def start(self) -> None:
        pass

    def shutdown(self) -> None:
        pass

    def status(self):
        return {
            "active": None,
            "tls_configured": False,
            "historical_sync_needed": True,
        }


def test_health_auth_dashboard_and_turn_detail(settings, database) -> None:
    database.insert_logs(complete_turn("turn-api"))
    app = create_app(
        settings,
        database=database,
        tls_client=UnconfiguredTLSClient(),  # type: ignore[arg-type]
        sync_service=PassiveSync(),  # type: ignore[arg-type]
    )
    headers = {"Authorization": f"Bearer {settings.api_token}"}

    with TestClient(app) as client:
        root = client.get("/")
        assert root.status_code == 200
        assert "text/html" in root.headers["content-type"]
        assert "AIFlow 对话日志监控" in root.text
        assert client.get("/assets/app.css").status_code == 200
        assert client.get("/health").status_code == 200
        assert client.get("/ready").json()["tls_configured"] is False
        status_payload = client.get("/api/v1/status", headers=headers).json()
        status_config = status_payload["config"]
        assert status_config["tls_page_size"] == 100
        assert status_payload["sync"]["historical_sync_needed"] is True
        assert client.get("/api/v1/overview").status_code == 401
        overview = client.get(
            "/api/v1/overview?start_date=2026-08-06&end_date=2026-08-06",
            headers=headers,
        )
        assert overview.status_code == 200
        overview_payload = overview.json()
        assert overview_payload["volume"]["turns"] == 1
        assert overview_payload["usage"]["cache_read_input_tokens"] == 30
        assert overview_payload["cost"]["actual_usd"] == 0.000946
        assert overview_payload["cost"]["sdk_reported_usd"] == 0.12
        assert overview_payload["cost"]["estimated_breakdown_usd"]["output_usd"] == 0.0006
        conversations = client.get(
            "/api/v1/conversations?start_date=2026-08-06&end_date=2026-08-06",
            headers=headers,
        )
        assert conversations.status_code == 200
        assert conversations.json()["items"][0]["configured_actual_usd"] == 0.000946
        turns = client.get(
            "/api/v1/turns?start_date=2026-08-06&end_date=2026-08-06",
            headers=headers,
        )
        assert turns.status_code == 200
        assert turns.json()["items"][0]["configured_actual_usd"] == 0.000946
        detail = client.get("/api/v1/turns/turn-api", headers=headers)
        assert detail.status_code == 200
        assert detail.json()["turn"]["total_tokens"] == 140
        assert client.post(
            "/api/v1/sync",
            headers=headers,
            json={"start_date": "2026-08-01", "end_date": "2026-08-01"},
        ).status_code == 503


def test_auth_can_be_explicitly_disabled_for_isolated_local_use(settings, database) -> None:
    local_settings = replace(settings, auth_disabled=True, api_token="")
    app = create_app(
        local_settings,
        database=database,
        tls_client=UnconfiguredTLSClient(),  # type: ignore[arg-type]
        sync_service=PassiveSync(),  # type: ignore[arg-type]
    )
    with TestClient(app) as client:
        assert client.get("/api/v1/status").status_code == 200
