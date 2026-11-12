from __future__ import annotations

import asyncio

import pytest

from reporting.job_queue import JobQueue, QueueFullError


@pytest.mark.asyncio
async def test_submit_runs_and_returns_result():
    queue = JobQueue(concurrency=1, max_pending=5)
    await queue.start()
    try:
        result = await queue.submit("job", lambda: asyncio.sleep(0, result="ok"))
        assert result == "ok"
        status = queue.status()
        assert status["total_completed"] == 1
        assert status["total_failed"] == 0
        assert status["pending"] == 0
    finally:
        await queue.stop()


@pytest.mark.asyncio
async def test_submit_propagates_exception():
    queue = JobQueue(concurrency=1, max_pending=5)
    await queue.start()

    async def _boom():
        raise RuntimeError("explode")

    try:
        with pytest.raises(RuntimeError, match="explode"):
            await queue.submit("fail", _boom)
        assert queue.status()["total_failed"] == 1
    finally:
        await queue.stop()


@pytest.mark.asyncio
async def test_queue_full_raises():
    queue = JobQueue(concurrency=1, max_pending=1)
    await queue.start()
    gate = asyncio.Event()

    async def _block():
        await gate.wait()
        return "done"

    try:
        # Occupy the single worker
        first = asyncio.create_task(queue.submit("a", _block))
        await asyncio.sleep(0.05)

        # Fill the single pending slot
        second = asyncio.create_task(queue.submit("b", _block))
        await asyncio.sleep(0.05)

        with pytest.raises(QueueFullError):
            await queue.submit("c", _block)

        gate.set()
        assert await first == "done"
        assert await second == "done"
    finally:
        gate.set()
        await queue.stop()


@pytest.mark.asyncio
async def test_status_reports_active_job():
    queue = JobQueue(concurrency=1, max_pending=5)
    await queue.start()
    gate = asyncio.Event()

    async def _block():
        await gate.wait()
        return 1

    try:
        task = asyncio.create_task(queue.submit("slow", _block))
        await asyncio.sleep(0.05)
        status = queue.status()
        assert status["concurrency"] == 1
        assert len(status["active"]) == 1
        assert status["active"][0]["name"] == "slow"
        gate.set()
        await task
    finally:
        gate.set()
        await queue.stop()


@pytest.mark.asyncio
async def test_job_timeout_frees_worker():
    queue = JobQueue(concurrency=1, max_pending=5, job_timeout_s=0.1)

    async def _hang():
        await asyncio.sleep(30)

    await queue.start()
    try:
        with pytest.raises(TimeoutError):
            await queue.submit("hung", _hang)
        # Worker must be free again for the next job
        result = await asyncio.wait_for(
            queue.submit("next", lambda: asyncio.sleep(0, result="ok")), timeout=2
        )
        assert result == "ok"
        assert queue.status()["total_failed"] == 1
    finally:
        await queue.stop()


@pytest.mark.asyncio
async def test_abandoned_queued_job_is_skipped():
    """A job whose submitter was cancelled while queued must not run (regression)."""
    queue = JobQueue(concurrency=1, max_pending=5)
    await queue.start()
    gate = asyncio.Event()
    ran: list[str] = []

    async def _block():
        await gate.wait()
        return "done"

    async def _tracked():
        ran.append("tracked")
        return "late"

    try:
        first = asyncio.create_task(queue.submit("a", _block))
        await asyncio.sleep(0.05)
        second = asyncio.create_task(queue.submit("b", _tracked))
        await asyncio.sleep(0.05)

        second.cancel()  # client disconnects while job 'b' is still queued
        with pytest.raises(asyncio.CancelledError):
            await second

        gate.set()
        assert await first == "done"
        await asyncio.sleep(0.1)  # give the worker a chance to (not) run 'b'
        assert ran == []
    finally:
        gate.set()
        await queue.stop()
