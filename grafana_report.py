#!/usr/bin/env python3
"""CLI entry point that wires the modular Grafana reporting stack."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
from pathlib import Path
from collections.abc import Iterable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from zoneinfo import ZoneInfo

from reporting import settings
from reporting.email_service import EmailService
from reporting.job_queue import JobQueue
from reporting.logging_config import configure_logging
from reporting.models import ReportConfig, load_reports_config
from reporting.pdf_renderer import GrafanaPDFRenderer
from reporting.report_service import ReportRunner, run_all_reports
from reporting.util import pdf_sweeper_loop

logger = logging.getLogger(__name__)


def _build_runner() -> ReportRunner:
    renderer = GrafanaPDFRenderer(settings)
    mailer = EmailService(settings)
    return ReportRunner(settings, renderer, mailer)


def _load_reports(config_path: Path) -> tuple[ZoneInfo, list[ReportConfig]]:
    tz_name, reports = load_reports_config(config_path, settings.default_tz)
    return ZoneInfo(tz_name), reports


async def _run_single_report(runner: ReportRunner, report: ReportConfig, tz: ZoneInfo) -> None:
    await runner.renderer.start()
    try:
        await runner.generate_pdf(report, tz, send_mail=True)
        logger.info("OK %s — pdf ready", report.name)
    finally:
        await runner.renderer.stop()


async def _scheduler_forever(
    runner: ReportRunner,
    reports: Iterable[ReportConfig],
    tz: ZoneInfo,
    queue: JobQueue,
) -> None:
    scheduler = AsyncIOScheduler(timezone=str(tz))

    async def _execute_report(report: ReportConfig) -> None:
        logger.debug("Job triggered for %s", report.name)
        try:
            pdf_path = await queue.submit(
                report.name,
                lambda r=report: runner.generate_pdf(r, tz, send_mail=True),
            )
            logger.info("SENT %s — scheduled run complete, PDF: %s", report.name, pdf_path)
        except Exception as exc:  # noqa: BLE001
            logger.exception("ERROR %s: %s", report.name, exc)

    for report in reports:
        trigger = CronTrigger.from_crontab(report.cron, timezone=str(tz))
        scheduler.add_job(
            _execute_report,
            trigger=trigger,
            args=[report],
            name=report.name,
            misfire_grace_time=3600,
        )
        logger.info("SCHEDULED %s @ %s (%s)", report.name, report.cron, tz.key)

    scheduler.start()

    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        logger.info("Scheduler shutting down…")
        scheduler.shutdown(wait=False)
        raise
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler shutting down…")
        scheduler.shutdown(wait=False)
        raise


def _install_stop_handlers(loop: asyncio.AbstractEventLoop) -> tuple[asyncio.Event, callable]:
    """Install SIGTERM/SIGINT handlers that request a graceful shutdown.

    uvicorn captures these signals during ``serve()`` and *re-raises* them
    afterwards; with the default disposition that kills the process before any
    cleanup can run. Our handler absorbs the re-raise and sets a stop event.
    Returns the event and a restore callback.
    """
    stop_event = asyncio.Event()

    def _handler(signum, frame):  # noqa: ARG001
        logger.info("Received signal %s; initiating graceful shutdown", signum)
        loop.call_soon_threadsafe(stop_event.set)

    previous = {
        sig: signal.signal(sig, _handler)
        for sig in (signal.SIGTERM, signal.SIGINT)
    }

    def _restore() -> None:
        for sig, handler in previous.items():
            with contextlib.suppress(Exception):
                signal.signal(sig, handler)

    return stop_event, _restore


async def _run_daemon(
    runner: ReportRunner,
    reports: list[ReportConfig],
    tz: ZoneInfo,
    host: str,
    port: int,
) -> None:
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise SystemExit(
            "Uvicorn is required for daemon mode. Install it via 'pip install uvicorn[standard]'."
        ) from exc

    from reporting.api import app  # Local import to avoid unneeded FastAPI dependency for CLI-only use
    from reporting import api as api_module

    await runner.renderer.start()
    queue = JobQueue(
        concurrency=runner.settings.run_all_concurrency,
        max_pending=runner.settings.queue_max_pending,
        job_timeout_s=runner.settings.job_timeout_s,
    )
    await queue.start()

    # Share the queue with the API module so HTTP-triggered exports
    # go through the same concurrency-limited pipeline.
    api_module.set_runner(runner)
    api_module.set_job_queue(queue)

    loop = asyncio.get_running_loop()
    stop_event, restore_signals = _install_stop_handlers(loop)

    config = uvicorn.Config(app, host=host, port=port, loop="asyncio", log_level="info")
    server = uvicorn.Server(config)
    logger.info("FastAPI server listening on http://%s:%s", host, port)

    scheduler_task = asyncio.create_task(_scheduler_forever(runner, reports, tz, queue))
    api_task = asyncio.create_task(server.serve())
    stop_task = asyncio.create_task(stop_event.wait())
    logger.info("DAEMON Scheduler + API running. Ctrl+C to stop both.")

    try:
        done, _ = await asyncio.wait(
            {scheduler_task, api_task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in done & {scheduler_task, api_task}:
            exc = task.exception()
            if exc:
                raise exc
        if stop_task in done:
            logger.info("Shutdown requested; stopping scheduler and API…")
    finally:
        server.should_exit = True
        for task in (scheduler_task, stop_task):
            if not task.done():
                task.cancel()
        # Give uvicorn a bounded window to close connections gracefully
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(api_task), timeout=10)
        if not api_task.done():
            api_task.cancel()
        await asyncio.gather(scheduler_task, api_task, stop_task, return_exceptions=True)
        await queue.stop()
        await runner.renderer.stop()
        restore_signals()


async def main() -> None:
    configure_logging(settings.log_level)

    parser = argparse.ArgumentParser(description="Grafana PDF Reports")
    parser.add_argument("-c", "--config", default=str(settings.config_path), help="Path to reports YAML config")
    parser.add_argument("--run-once", help="Run just the report with the given name and exit")
    parser.add_argument("--run-all-now", action="store_true", help="Run all reports immediately and exit")
    parser.add_argument("--daemon", action="store_true", help="Run scheduler and FastAPI API server in one process")
    parser.add_argument("--api-host", default="0.0.0.0", help="FastAPI listen host (daemon mode)")
    parser.add_argument("--api-port", type=int, default=8080, help="FastAPI listen port (daemon mode)")
    args = parser.parse_args()

    config_path = Path(args.config).resolve()
    settings.config_path = config_path  # type: ignore[attr-defined]

    tz, reports = _load_reports(config_path)
    if not reports:
        raise SystemExit(f"No reports defined in {config_path}")

    runner = _build_runner()

    if args.daemon and (args.run_once or args.run_all_now):
        raise SystemExit("--daemon cannot be combined with --run-once or --run-all-now")

    if args.run_once:
        target = next((rep for rep in reports if rep.name == args.run_once), None)
        if not target:
            raise SystemExit(f"Report '{args.run_once}' not found in config")
        await _run_single_report(runner, target, tz)
        return

    if args.run_all_now:
        await runner.renderer.start()
        try:
            await run_all_reports(runner, reports, tz, concurrency=settings.run_all_concurrency)
        finally:
            await runner.renderer.stop()
        return

    if args.daemon:
        await _run_daemon(runner, reports, tz, args.api_host, args.api_port)
        return

    await runner.renderer.start()
    queue = JobQueue(
        concurrency=settings.run_all_concurrency,
        max_pending=settings.queue_max_pending,
        job_timeout_s=settings.job_timeout_s,
    )
    await queue.start()
    loop = asyncio.get_running_loop()
    stop_event, restore_signals = _install_stop_handlers(loop)
    scheduler_task = asyncio.create_task(_scheduler_forever(runner, reports, tz, queue))
    stop_task = asyncio.create_task(stop_event.wait())
    sweeper_task = (
        asyncio.create_task(pdf_sweeper_loop(settings.pdf_outdir, settings.pdf_retention_hours * 3600))
        if settings.pdf_retention_hours > 0
        else None
    )
    logger.info("RUNNING Scheduler active. Ctrl+C to stop.")
    try:
        done, _ = await asyncio.wait({scheduler_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        if scheduler_task in done:
            exc = scheduler_task.exception()
            if exc:
                raise exc
    finally:
        for task in (scheduler_task, stop_task, sweeper_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(t for t in (scheduler_task, stop_task, sweeper_task) if t is not None),
            return_exceptions=True,
        )
        await queue.stop()
        await runner.renderer.stop()
        restore_signals()


if __name__ == "__main__":
    asyncio.run(main())
