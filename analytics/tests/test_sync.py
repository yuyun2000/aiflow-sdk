from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from aiflow_analytics.sync import SyncService

from .fixtures import complete_turn


class FakeTLSClient:
    configured = True

    def __init__(self) -> None:
        self.calls = 0
        self.failures = 0

    def search(self, _start_ms: int, _end_ms: int, query: str | None = None):
        self.calls += 1
        if self.failures:
            self.failures -= 1
            raise RuntimeError("temporary TLS failure")
        return complete_turn(
            f"turn-sync-{self.calls}",
            conversation_id=f"conversation-sync-{self.calls}",
        )


def test_historical_sync_is_idempotent_by_completed_day(settings, database) -> None:
    client = FakeTLSClient()
    service = SyncService(settings, database, client)  # type: ignore[arg-type]
    target = date(2026, 8, 1)

    first = service.sync_range(target, target)
    second = service.sync_range(target, target)

    assert first["inserted"] > 0
    assert second == {
        "fetched": 0,
        "inserted": 0,
        "duplicates": 0,
        "assembled": 0,
        "turns": 0,
        "ignored": 0,
        "errors": 0,
    }
    assert client.calls == 1
    assert database.day_is_synced("2026-08-01")


def test_missing_history_is_retried_without_refetching_clean_days(settings, database) -> None:
    client = FakeTLSClient()
    service = SyncService(settings, database, client)  # type: ignore[arg-type]
    start = date(2026, 8, 1)

    client.failures = 1
    with pytest.raises(RuntimeError, match="temporary TLS failure"):
        service.sync_range(start, start)
    assert database.historical_sync_needed("2026-08-01", "2026-08-01") is True

    service._sync_missing_history(start, date(2026, 8, 2))
    assert client.calls == 2
    assert database.historical_sync_needed("2026-08-01", "2026-08-01") is False


def test_current_day_is_not_marked_so_startup_can_refresh_it(settings, database) -> None:
    client = FakeTLSClient()
    service = SyncService(settings, database, client)  # type: ignore[arg-type]
    today = datetime.now(ZoneInfo(settings.timezone)).date()

    service.sync_range(today - timedelta(days=1), today)

    assert database.day_is_synced((today - timedelta(days=1)).isoformat()) is True
    assert database.day_is_synced(today.isoformat()) is False


def test_manual_trigger_runs_in_background(settings, database) -> None:
    client = FakeTLSClient()
    service = SyncService(settings, database, client)  # type: ignore[arg-type]
    target = date(2026, 8, 2)

    assert service.trigger(target, target) is True
    deadline = time.time() + 2
    while service.status()["active"] is not None and time.time() < deadline:
        time.sleep(0.01)
    service.shutdown()

    assert client.calls == 1
    assert database.query_one("SELECT COUNT(*) AS count FROM turns")["count"] == 1


def test_manual_trigger_reserves_sync_before_returning(
    settings,
    database,
    monkeypatch,
) -> None:
    client = FakeTLSClient()
    service = SyncService(settings, database, client)  # type: ignore[arg-type]
    pending: list[object] = []

    class DeferredThread:
        def __init__(self, *, target, **_kwargs) -> None:
            self.target = target

        def start(self) -> None:
            pending.append(self.target)

        def is_alive(self) -> bool:
            return False

    monkeypatch.setattr("aiflow_analytics.sync.threading.Thread", DeferredThread)

    assert service.trigger(date(2026, 8, 2), date(2026, 8, 2)) is True
    assert service._sync_lock.locked()
    assert service.trigger(date(2026, 8, 2), date(2026, 8, 2)) is False

    pending[0]()  # type: ignore[operator]
    assert not service._sync_lock.locked()
