from __future__ import annotations

import logging
import threading
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .config import Settings
from .database import Database
from .tls_client import TLSLogClient, milliseconds

LOGGER = logging.getLogger(__name__)
TOTAL_KEYS = (
    "fetched",
    "inserted",
    "duplicates",
    "assembled",
    "turns",
    "ignored",
    "errors",
)


def _empty_totals() -> dict[str, int]:
    return {key: 0 for key in TOTAL_KEYS}


def _merge_totals(target: dict[str, int], values: dict[str, int]) -> None:
    for key in TOTAL_KEYS:
        target[key] += int(values.get(key, 0))


class SyncService:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        tls_client: TLSLogClient,
    ) -> None:
        self.settings = settings
        self.database = database
        self.tls_client = tls_client
        self._sync_lock = threading.Lock()
        self._trigger_lock = threading.Lock()
        self._stop = threading.Event()
        self._periodic_thread: threading.Thread | None = None
        self._manual_thread: threading.Thread | None = None
        self._active: dict[str, Any] | None = None
        # The recent poll cursor advances only after a successful window import.
        # Keeping it in memory avoids rescanning a stalled latest event forever;
        # a restart still falls back to the database watermark plus the overlap.
        self._recent_sync_end_ms: int | None = None

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.settings.timezone)

    def _day_bounds(self, value: date) -> tuple[int, int]:
        start = datetime.combine(value, time.min, tzinfo=self.timezone)
        end = start + timedelta(days=1) - timedelta(milliseconds=1)
        return milliseconds(start), milliseconds(end)

    def _sync_window(
        self,
        start_ms: int,
        end_ms: int,
        *,
        start_label: str,
        end_label: str,
    ) -> dict[str, int]:
        if not self._sync_lock.acquire(blocking=False):
            raise RuntimeError("a log sync is already running")
        run_id = self.database.start_sync_run(start_label, end_label)
        totals = _empty_totals()
        self._active = {
            "run_id": run_id,
            "start": start_label,
            "end": end_label,
            "mode": "window",
        }
        try:
            logs = self.tls_client.search(start_ms, end_ms)
            result = self.database.insert_logs(logs)
            _merge_totals(totals, result)
            self.database.finish_sync_run(run_id, status="completed", totals=totals)
            return totals
        except Exception as exc:
            self.database.finish_sync_run(
                run_id,
                status="failed",
                totals=totals,
                error=f"{type(exc).__name__}: {exc}",
            )
            raise
        finally:
            self._active = None
            self._sync_lock.release()

    def sync_range(
        self,
        start_date: date,
        end_date: date,
        *,
        force: bool = False,
    ) -> dict[str, int]:
        if start_date > end_date:
            raise ValueError("start_date must be on or before end_date")
        if not self._sync_lock.acquire(blocking=False):
            raise RuntimeError("a log sync is already running")
        try:
            return self._sync_range_locked(start_date, end_date, force=force)
        finally:
            self._sync_lock.release()

    def _sync_range_locked(
        self,
        start_date: date,
        end_date: date,
        *,
        force: bool,
    ) -> dict[str, int]:
        run_id = self.database.start_sync_run(start_date.isoformat(), end_date.isoformat())
        totals = _empty_totals()
        self._active = {
            "run_id": run_id,
            "start": start_date.isoformat(),
            "end": end_date.isoformat(),
            "current_date": start_date.isoformat(),
            "mode": "range",
        }
        try:
            today = datetime.now(self.timezone).date()
            current = start_date
            while current <= end_date:
                self._active["current_date"] = current.isoformat()
                if (
                    current < today
                    and self.database.day_is_synced(current.isoformat())
                    and not force
                ):
                    current += timedelta(days=1)
                    continue
                start_ms, end_ms = self._day_bounds(current)
                logs = self.tls_client.search(start_ms, end_ms)
                result = self.database.insert_logs(logs)
                _merge_totals(totals, result)
                fallback_checked = bool(
                    getattr(self.tls_client, "last_search_used_fallback", False)
                )
                if current < today:
                    self.database.mark_day_synced(
                        current.isoformat(),
                        result,
                        fallback_checked=fallback_checked,
                    )
                LOGGER.info(
                    "Synced %s fetched=%d inserted=%d duplicates=%d "
                    "assembled=%d turns=%d errors=%d",
                    current,
                    result["fetched"],
                    result["inserted"],
                    result["duplicates"],
                    result["assembled"],
                    result["turns"],
                    result["errors"],
                )
                current += timedelta(days=1)
            self.database.finish_sync_run(run_id, status="completed", totals=totals)
            return totals
        except Exception as exc:
            self.database.finish_sync_run(
                run_id,
                status="failed",
                totals=totals,
                error=f"{type(exc).__name__}: {exc}",
            )
            raise
        finally:
            self._active = None

    def sync_recent(self) -> dict[str, int]:
        now = datetime.now(self.timezone)
        start_ms, end_ms = self._recent_window(now)
        result = self._sync_window(
            start_ms,
            end_ms,
            start_label=datetime.fromtimestamp(start_ms / 1000, tz=self.timezone).isoformat(),
            end_label=now.isoformat(),
        )
        self._recent_sync_end_ms = max(self._recent_sync_end_ms or end_ms, end_ms)
        return result

    def _recent_window(self, now: datetime) -> tuple[int, int]:
        """Return the next recent window while retaining the configured overlap."""
        configured_start = datetime.combine(
            date.fromisoformat(self.settings.analytics_start_date),
            time.min,
            tzinfo=self.timezone,
        )
        now_ms = milliseconds(now)
        configured_start_ms = milliseconds(configured_start)
        overlap_ms = self.settings.sync_overlap_minutes * 60_000
        if self._recent_sync_end_ms is not None:
            start_ms = max(configured_start_ms, self._recent_sync_end_ms - overlap_ms)
        else:
            latest_ms = self.database.latest_event_time_ms()
            if latest_ms is None:
                start_ms = max(configured_start_ms, now_ms - overlap_ms)
            else:
                start_ms = max(configured_start_ms, latest_ms - overlap_ms)
        return start_ms, now_ms

    def _sync_missing_history(self, start_date: date, today: date) -> None:
        """Retry only missing clean days before today, without touching completed days."""
        end_date = today - timedelta(days=1)
        if start_date > end_date:
            return
        if not self.database.historical_sync_needed(start_date.isoformat(), end_date.isoformat()):
            return
        self.sync_range(start_date, end_date)

    def _initial_and_periodic_loop(self) -> None:
        start_date = date.fromisoformat(self.settings.analytics_start_date)
        try:
            today = datetime.now(self.timezone).date()
            # The current day is intentionally included once at startup and is never
            # marked in sync_days, so every service restart refreshes today's window.
            self.sync_range(start_date, today)
            # The initial range already covers the current day. Start the periodic
            # cursor at startup completion instead of using an old latest event.
            self._recent_sync_end_ms = milliseconds(datetime.now(self.timezone))
        except Exception as exc:
            LOGGER.error("Initial analytics sync failed: %s: %s", type(exc).__name__, exc)
        while not self._stop.wait(self.settings.sync_interval_seconds):
            today = datetime.now(self.timezone).date()
            try:
                self._sync_missing_history(start_date, today)
            except Exception as exc:
                LOGGER.error(
                    "Historical analytics sync failed: %s: %s",
                    type(exc).__name__,
                    exc,
                )
            try:
                self.sync_recent()
            except Exception as exc:
                LOGGER.error("Periodic analytics sync failed: %s: %s", type(exc).__name__, exc)

    def start(self) -> None:
        if not self.settings.sync_on_startup or not self.tls_client.configured:
            return
        if self._periodic_thread and self._periodic_thread.is_alive():
            return
        self._stop.clear()
        self._periodic_thread = threading.Thread(
            target=self._initial_and_periodic_loop,
            name="aiflow-analytics-sync",
            daemon=True,
        )
        self._periodic_thread.start()

    def trigger(
        self,
        start_date: date,
        end_date: date,
        *,
        force: bool = False,
    ) -> bool:
        with self._trigger_lock:
            if self._manual_thread and self._manual_thread.is_alive():
                return False
            if not self._sync_lock.acquire(blocking=False):
                return False

            def run() -> None:
                try:
                    self._sync_range_locked(start_date, end_date, force=force)
                except Exception as exc:
                    LOGGER.error("Manual analytics sync failed: %s: %s", type(exc).__name__, exc)
                finally:
                    self._sync_lock.release()

            self._manual_thread = threading.Thread(
                target=run,
                name="aiflow-analytics-manual-sync",
                daemon=True,
            )
            try:
                self._manual_thread.start()
            except Exception:
                self._sync_lock.release()
                raise
            return True

    def shutdown(self) -> None:
        self._stop.set()
        if self._periodic_thread:
            self._periodic_thread.join(timeout=2)
        if self._manual_thread:
            self._manual_thread.join(timeout=2)

    def status(self) -> dict[str, Any]:
        start_date = date.fromisoformat(self.settings.analytics_start_date)
        today = datetime.now(self.timezone).date()
        historical_end = today - timedelta(days=1)
        return {
            "tls_configured": self.tls_client.configured,
            "periodic_running": bool(
                self._periodic_thread and self._periodic_thread.is_alive()
            ),
            "active": dict(self._active) if self._active else None,
            "recent_sync_end": (
                datetime.fromtimestamp(
                    self._recent_sync_end_ms / 1000,
                    tz=self.timezone,
                ).isoformat()
                if self._recent_sync_end_ms is not None
                else None
            ),
            "historical_sync_needed": self.database.historical_sync_needed(
                start_date.isoformat(), historical_end.isoformat()
            ),
            **self.database.sync_status(),
        }
