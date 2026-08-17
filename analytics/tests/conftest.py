from __future__ import annotations

import pytest

from aiflow_analytics.config import Settings
from aiflow_analytics.database import Database


@pytest.fixture
def settings(tmp_path):
    return Settings(
        host="127.0.0.1",
        port=5090,
        timezone="Asia/Shanghai",
        data_dir=tmp_path,
        log_level="INFO",
        tls_region="cn-beijing",
        tls_endpoint="tls.example.test",
        tls_topic_id="topic-test",
        tls_access_key="fake-read-access-key",
        tls_secret_key="fake-read-secret-key",
        tls_query="event:aiflow_conversation_trace",
        tls_schema_version=2,
        tls_page_size=100,
        tls_max_pages=10,
        tls_timeout_seconds=1,
        analytics_start_date="2026-08-01",
        sync_on_startup=False,
        sync_interval_seconds=60,
        sync_overlap_minutes=15,
        auth_disabled=False,
        api_token="fake-api-token-with-32-characters",
        default_range_days=7,
    )


@pytest.fixture
def database(settings):
    value = Database(settings.database_path, settings.tls_schema_version)
    value.initialize()
    return value
