"""Async job queue with bounded concurrency for report generation."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)


class QueueFullError(RuntimeError):
    """Raised when the pending queue exceeds max_pending."""


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

    def __init__(
        self,
        concurrency: int = 2,
        max_pending: int = 10,
        job_timeout_s: float | None = None,
    ) -> None:
        self._concurrency = max(1, concurrency)
        self._max_pending = max(1, max_pending)
        self._job_timeout_s = job_timeout_s if job_timeout_s and job_timeout_s > 0 else None
        self._queue: asyncio.Queue[_Job] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []
        self._active: dict[int, _ActiveJob] = {}
        self._job_counter = 0
        self._total_completed = 0
        self._total_failed = 0

    async def start(self) -> None:
        if self._workers:
            return
        for wid in range(self._concurrency):
            task = asyncio.create_task(self._worker(wid), name=f"queue-worker-{wid}")
            self._workers.append(task)
        logger.info("Started %d worker(s), max_pending=%d", self._concurrency, self._max_pending)

    async def stop(self) -> None:
        for w in self._workers:
            w.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        logger.info("All workers stopped")

    async def submit(self, name: str, coro_factory: Callable[[], Awaitable[Any]]) -> Any:
        """Enqueue a job and wait for its result.

        *coro_factory* is a zero-arg callable that returns the coroutine to
        execute (e.g. ``lambda: runner.generate_pdf(...)``).  This avoids
        creating the coroutine before a worker is ready.
        """
        pending = self._queue.qsize()
        if pending >= self._max_pending:
            raise QueueFullError(
                f"Queue is full ({pending}/{self._max_pending} pending). Try again later."
            )

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

        active = len(self._active)
        if pending > 0 or active >= self._concurrency:
            logger.info(
                "'%s' queued at position %d (active: %d/%d)",
                name,
                pending + 1,
                active,
                self._concurrency,
            )
        else:
            logger.info("'%s' submitted (worker available)", name)

        await self._queue.put(job)
        return await future

    def status(self) -> dict:
        """Snapshot of queue state, suitable for the ``/queue`` API."""
        now = asyncio.get_running_loop().time()
        return {
            "concurrency": self._concurrency,
            "max_pending": self._max_pending,
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

    async def _worker(self, worker_id: int) -> None:
        while True:
            job = await self._queue.get()
            if job.future.done():
                # Caller gave up (e.g. client disconnected) while the job was
                # still queued — skip it instead of rendering for nobody.
                logger.info("Worker %d skipping '%s' (abandoned while queued)", worker_id, job.name)
                self._queue.task_done()
                continue
            loop = asyncio.get_running_loop()
            wait_s = loop.time() - job.enqueued_at
            if wait_s >= 0.5:
                logger.info(
                    "Worker %d picked up '%s' (waited %.1fs in queue)",
                    worker_id,
                    job.name,
                    wait_s,
                )
            else:
                logger.info("Worker %d starting '%s'", worker_id, job.name)

            active = _ActiveJob(id=job.id, name=job.name, worker_id=worker_id, started_at=loop.time())
            self._active[worker_id] = active
            try:
                coro = job.coro_factory()
                if self._job_timeout_s is not None:
                    result = await asyncio.wait_for(coro, timeout=self._job_timeout_s)
                else:
                    result = await coro
                if not job.future.done():
                    job.future.set_result(result)
                self._total_completed += 1
                elapsed = loop.time() - active.started_at
                logger.info("Worker %d finished '%s' (%.1fs)", worker_id, job.name, elapsed)
            except asyncio.CancelledError:
                if not job.future.done():
                    job.future.cancel()
                raise
            except Exception as exc:
                if not job.future.done():
                    job.future.set_exception(exc)
                self._total_failed += 1
                logger.exception("Worker %d failed '%s': %s", worker_id, job.name, exc)
            finally:
                self._active.pop(worker_id, None)
                self._queue.task_done()
