from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

from aiflow_server.tasks import (
    AGENT_ACTIVITY_WRITE_INTERVAL_SECONDS,
    TaskManager,
)


class RecordingStorage:
    def __init__(self) -> None:
        self.task = {"progress": 0}
        self.get_task_calls = 0
        self.updates: list[dict] = []
        self.events: list[tuple[str, str, dict]] = []
        self.append_threads: list[tuple[str, int]] = []
        self.read_threads: list[int] = []
        self.update_threads: list[int] = []

    def get_task(self, _task_id: str) -> dict:
        self.read_threads.append(threading.get_ident())
        self.get_task_calls += 1
        return dict(self.task)

    def update_task(self, _task_id: str, **values):
        self.update_threads.append(threading.get_ident())
        self.task.update(values)
        self.updates.append(values)
        return dict(self.task)

    def append_event(self, task_id: str, event_type: str, data: dict) -> dict:
        self.append_threads.append((event_type, threading.get_ident()))
        self.events.append((task_id, event_type, data))
        return {"sequence": len(self.events), "type": event_type, "data": data}


def test_agent_activity_timestamp_is_throttled_without_task_reads():
    async def exercise():
        event_loop_thread = threading.get_ident()
        storage = RecordingStorage()
        manager = TaskManager(
            SimpleNamespace(max_concurrent_tasks=1),
            storage,
            object(),
            object(),
            object(),
        )

        await manager._emit("task-1", "assistant_text_delta", {"text": "a"}, agent_event=True)
        await manager._emit("task-1", "assistant_text_delta", {"text": "b"}, agent_event=True)
        assert storage.get_task_calls == 0
        assert len(storage.updates) == 1
        assert len(storage.events) == 2

        manager._last_agent_activity_write_at["task-1"] -= AGENT_ACTIVITY_WRITE_INTERVAL_SECONDS
        await manager._emit("task-1", "assistant_text_delta", {"text": "c"}, agent_event=True)
        assert len(storage.updates) == 2

        manager._append_event("task-1", "task_completed", {"status": "completed"})
        assert "task-1" not in manager._last_agent_activity_write_at

        await manager._emit(
            "task-2",
            "task_started",
            {"stage": "coding", "progress": 10},
            agent_event=False,
        )
        assert storage.get_task_calls == 1
        assert storage.task["progress"] == 10
        async_append_threads = [
            thread_id
            for event_type, thread_id in storage.append_threads
            if event_type != "task_completed"
        ]
        assert async_append_threads
        assert all(thread_id != event_loop_thread for thread_id in async_append_threads)
        assert all(thread_id != event_loop_thread for thread_id in storage.update_threads)
        assert all(thread_id != event_loop_thread for thread_id in storage.read_threads)

    asyncio.run(exercise())
