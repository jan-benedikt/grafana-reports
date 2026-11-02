"""Async job queue with bounded concurrency for report generation."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional


@dataclass
class _Job:
    id: int
    name: str
    coro_factory: Callable[[], Awaitable[Any]]
    future: asyncio.Future
    enqueued_at: float


@dataclass
class _ActiveJob:
    id: int
    name: str
    worker_id: int
    started_at: float


class JobQueue:
    """FIFO queue with a fixed pool of async workers.

    Jobs are submitted via ``submit(name, coro_factory)`` and executed by
    one of *concurrency* worker tasks.  ``submit`` returns the coroutine
    result (or raises) once the job finishes, so callers can ``await`` it.
    """

    def __init__(self, concurrency: int = 2) -> None:
        self._concurrency = max(1, concurrency)
        self._queue: asyncio.Queue[_Job] = asyncio.Queue()
        self._workers: List[asyncio.Task] = []
        self._active: Dict[int, _ActiveJob] = {}
        self._job_counter = 0
        self._total_completed = 0
        self._total_failed = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._workers:
            return
        for wid in range(self._concurrency):
            task = asyncio.create_task(self._worker(wid), name=f"queue-worker-{wid}")
            self._workers.append(task)
        print(f"[QUEUE] Started {self._concurrency} worker(s)")

    async def stop(self) -> None:
        for w in self._workers:
            w.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        print("[QUEUE] All workers stopped")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def submit(self, name: str, coro_factory: Callable[[], Awaitable[Any]]) -> Any:
        """Enqueue a job and wait for its result.

        *coro_factory* is a zero-arg callable that returns the coroutine to
        execute (e.g. ``lambda: runner.generate_pdf(...)``).  This avoids
        creating the coroutine before a worker is ready.
        """
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self._job_counter += 1
        job = _Job(
            id=self._job_counter,
            name=name,
            coro_factory=coro_factory,
            future=future,
            enqueued_at=loop.time(),
        )

        pending = self._queue.qsize()
        active = len(self._active)
        if pending > 0 or active >= self._concurrency:
            print(
                f"[QUEUE] '{name}' queued at position {pending + 1} "
                f"(active: {active}/{self._concurrency})"
            )
        else:
            print(f"[QUEUE] '{name}' submitted (worker available)")

        await self._queue.put(job)
        return await future

    def status(self) -> dict:
        """Snapshot of queue state, suitable for the ``/queue`` API."""
        now = asyncio.get_event_loop().time()
        return {
            "concurrency": self._concurrency,
            "pending": self._queue.qsize(),
            "active": [
                {
                    "job_id": aj.id,
                    "name": aj.name,
                    "worker": aj.worker_id,
                    "running_for_s": round(now - aj.started_at, 1),
                }
                for aj in self._active.values()
            ],
            "total_completed": self._total_completed,
            "total_failed": self._total_failed,
        }

    # ------------------------------------------------------------------
    # Workers
    # ------------------------------------------------------------------

    async def _worker(self, worker_id: int) -> None:
        while True:
            job = await self._queue.get()
            loop = asyncio.get_running_loop()
            wait_s = loop.time() - job.enqueued_at
            if wait_s >= 0.5:
                print(f"[QUEUE] Worker {worker_id} picked up '{job.name}' (waited {wait_s:.1f}s in queue)")
            else:
                print(f"[QUEUE] Worker {worker_id} starting '{job.name}'")

            active = _ActiveJob(id=job.id, name=job.name, worker_id=worker_id, started_at=loop.time())
            self._active[worker_id] = active
            try:
                result = await job.coro_factory()
                if not job.future.done():
                    job.future.set_result(result)
                self._total_completed += 1
                elapsed = loop.time() - active.started_at
                print(f"[QUEUE] Worker {worker_id} finished '{job.name}' ({elapsed:.1f}s)")
            except asyncio.CancelledError:
                if not job.future.done():
                    job.future.cancel()
                raise
            except Exception as exc:
                if not job.future.done():
                    job.future.set_exception(exc)
                self._total_failed += 1
                print(f"[QUEUE] Worker {worker_id} failed '{job.name}': {exc}")
            finally:
                self._active.pop(worker_id, None)
                self._queue.task_done()
