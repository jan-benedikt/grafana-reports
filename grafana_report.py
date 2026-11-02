#!/usr/bin/env python3
"""CLI entry point that wires the modular Grafana reporting stack."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from typing import Iterable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from zoneinfo import ZoneInfo

from reporting import settings
from reporting.email_service import EmailService
from reporting.job_queue import JobQueue
from reporting.models import ReportConfig, load_reports_config
from reporting.pdf_renderer import GrafanaPDFRenderer
from reporting.report_service import ReportRunner, run_all_reports


def _build_runner() -> ReportRunner:
    renderer = GrafanaPDFRenderer(settings)
    mailer = EmailService(settings)
    return ReportRunner(settings, renderer, mailer)


def _load_reports(config_path: Path) -> tuple[ZoneInfo, list[ReportConfig]]:
    tz_name, reports = load_reports_config(config_path, settings.default_tz)
    return ZoneInfo(tz_name), reports


async def _run_single_report(runner: ReportRunner, report: ReportConfig, tz: ZoneInfo) -> None:
    await runner.generate_pdf(report, tz, send_mail=True)
    print(f"[OK] {report.name} — pdf ready")


async def _scheduler_forever(
    runner: ReportRunner,
    reports: Iterable[ReportConfig],
    tz: ZoneInfo,
    queue: JobQueue,
) -> None:
    scheduler = AsyncIOScheduler(timezone=str(tz))

    async def _execute_report(report: ReportConfig) -> None:
        print(f"[DEBUG] Job triggered for {report.name} at {asyncio.get_event_loop().time()}")
        try:
            pdf_path = await queue.submit(
                report.name,
                lambda r=report: runner.generate_pdf(r, tz, send_mail=True),
            )
            print(f"[SENT] {report.name} — scheduled run complete, PDF: {pdf_path}")
        except Exception as exc:  # noqa: BLE001
            import traceback
            print(f"[ERROR] {report.name}: {exc}")
            print(f"[ERROR] Traceback:\n{traceback.format_exc()}")

    for report in reports:
        trigger = CronTrigger.from_crontab(report.cron, timezone=str(tz))
        scheduler.add_job(
            _execute_report,
            trigger=trigger,
            args=[report],
            name=report.name,
            misfire_grace_time=3600,
        )
        print(f"[SCHEDULED] {report.name} @ {report.cron} ({tz.key})")

    scheduler.start()

    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        print("[STOPPING] Scheduler shutting down…")
        scheduler.shutdown(wait=False)
        raise
    except (KeyboardInterrupt, SystemExit):
        print("[STOPPING] Scheduler shutting down…")
        scheduler.shutdown(wait=False)
        raise


async def _run_api_server(host: str, port: int) -> None:
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise SystemExit("Uvicorn is required for daemon mode. Install it via 'pip install uvicorn[standard]'.") from exc

    from reporting.api import app  # Local import to avoid unneeded FastAPI dependency for CLI-only use

    config = uvicorn.Config(app, host=host, port=port, loop="asyncio", log_level="info")
    server = uvicorn.Server(config)
    print(f"[API] FastAPI server listening on http://{host}:{port}")

    try:
        await server.serve()
    except asyncio.CancelledError:
        server.should_exit = True
        raise


async def _run_daemon(runner: ReportRunner, reports: list[ReportConfig], tz: ZoneInfo, host: str, port: int) -> None:
    queue = JobQueue(concurrency=runner.settings.run_all_concurrency)
    await queue.start()

    # Share the queue with the API module so HTTP-triggered exports
    # go through the same concurrency-limited pipeline.
    from reporting import api as api_module
    api_module.set_job_queue(queue)

    scheduler_task = asyncio.create_task(_scheduler_forever(runner, reports, tz, queue))
    api_task = asyncio.create_task(_run_api_server(host, port))
    tasks = {scheduler_task, api_task}
    print("[DAEMON] Scheduler + API running. Ctrl+C to stop both.")

    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        for task in done:
            exc = task.exception()
            if exc:
                raise exc
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await queue.stop()


async def main() -> None:
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
        await run_all_reports(runner, reports, tz, concurrency=settings.run_all_concurrency)
        return

    if args.daemon:
        await _run_daemon(runner, reports, tz, args.api_host, args.api_port)
        return

    queue = JobQueue(concurrency=settings.run_all_concurrency)
    await queue.start()
    print("[RUNNING] Scheduler active. Ctrl+C to stop.")
    try:
        await _scheduler_forever(runner, reports, tz, queue)
    finally:
        await queue.stop()


if __name__ == "__main__":
    asyncio.run(main())
