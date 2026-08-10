from __future__ import annotations

import time
from datetime import date

from aiflow_analytics.sync import SyncService

from .fixtures import complete_turn


class FakeTLSClient:
    configured = True

    def __init__(self) -> None:
        self.calls = 0

    def search(self, _start_ms: int, _end_ms: int, query: str | None = None):
        self.calls += 1
        return complete_turn(f"turn-sync-{self.calls}")


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
