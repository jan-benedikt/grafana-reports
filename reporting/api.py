from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from zoneinfo import ZoneInfo

from .auth import RequireApiKey
from .email_service import EmailService
from .job_queue import JobQueue, QueueFullError
from .logging_config import configure_logging
from .models import EmailConfig, RenderConfig, ReportConfig, load_reports_config
from .pdf_renderer import GrafanaPDFRenderer
from .report_service import ReportRunner
from .settings import settings
from .time_range import normalize_time_range
from .util import is_valid_dashboard_uid, resolve_timezone, sanitize_filename

try:
    import requests
except ImportError:
    requests = None  # type: ignore

configure_logging(settings.log_level)
logger = logging.getLogger(__name__)

app = FastAPI(title="Grafana PDF Export")

_timezone_name, _reports = load_reports_config(settings.config_path, settings.default_tz)
_default_tz = ZoneInfo(_timezone_name)
_report_by_uid = {r.dashboard_uid: r for r in _reports}

_renderer = GrafanaPDFRenderer(settings)
_mailer = EmailService(settings)
_runner = ReportRunner(settings, _renderer, _mailer)

_job_queue: JobQueue | None = None

_MAX_VARS = 50
_MAX_VAR_LEN = 512


def set_job_queue(queue: JobQueue) -> None:
    """Called by the daemon to share the queue with API handlers."""
    global _job_queue
    _job_queue = queue


def set_runner(runner: ReportRunner) -> None:
    """Share the daemon's ReportRunner (and its browser) with API handlers."""
    global _runner, _renderer, _mailer
    _runner = runner
    _renderer = runner.renderer
    _mailer = runner.mailer


def _cleanup_file(path):
    try:
        Path(path).unlink(missing_ok=True)
    except Exception:
        pass


def _fetch_dashboard_info_sync(uid: str) -> dict | None:
    """Fetch dashboard name and other metadata from Grafana API (blocking)."""
    headers = {}
    auth = None
    if settings.grafana_token:
        headers["Authorization"] = f"Bearer {settings.grafana_token}"
    elif settings.grafana_user and settings.grafana_password:
        auth = (settings.grafana_user, settings.grafana_password)
    else:
        logger.warning("No Grafana credentials configured, cannot fetch dashboard info for %s", uid)
        return None

    if requests is None:
        logger.warning("requests library not available, cannot fetch dashboard info for %s", uid)
        return None

    try:
        url = f"{settings.grafana_url}/api/dashboards/uid/{uid}"
        logger.debug("Fetching dashboard info from %s", url)
        response = requests.get(url, auth=auth, headers=headers, timeout=10)

        if response.status_code == 200:
            data = response.json()
            dashboard = data.get("dashboard", {})
            title = dashboard.get("title", "")
            return {
                "title": title,
                "uid": uid,
                "url": data.get("meta", {}).get("url", ""),
            }
        logger.warning("Failed to fetch dashboard %s: HTTP %s", uid, response.status_code)
        return None
    except Exception as e:
        logger.warning("Error fetching dashboard info for %s: %s", uid, e)
        return None


def _parse_vars(vars_raw: str) -> dict:
    vars_dict: dict = {}
    if not vars_raw:
        return vars_dict
    try:
        for pair in vars_raw.split(","):
            if not pair.strip():
                continue
            if "=" not in pair:
                raise ValueError(f"expected key=value, got '{pair}'")
            key, value = pair.split("=", 1)
            key, value = key.strip(), value.strip()
            if not key:
                raise ValueError("empty variable name")
            if len(key) > _MAX_VAR_LEN or len(value) > _MAX_VAR_LEN:
                raise ValueError("variable key/value too long")
            vars_dict[key] = value
            if len(vars_dict) > _MAX_VARS:
                raise ValueError(f"too many variables (max {_MAX_VARS})")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid vars format: {e}") from e
    return vars_dict


def _vars_from_request(request: Request, vars_raw: str) -> dict:
    """Merge compact ``vars=`` with Grafana-native ``var-*`` query params."""
    merged = _parse_vars(vars_raw)
    for key, value in request.query_params.multi_items():
        if not key.startswith("var-"):
            continue
        name = key[4:]
        if not name:
            continue
        if len(name) > _MAX_VAR_LEN or len(value) > _MAX_VAR_LEN:
            raise HTTPException(status_code=400, detail="variable key/value too long")
        # Last value wins for repeated keys (same as typical Grafana URL usage)
        merged[name] = value
        if len(merged) > _MAX_VARS:
            raise HTTPException(status_code=400, detail=f"too many variables (max {_MAX_VARS})")
    return merged


def _parse_render_overrides(
    page_mode: str | None,
    width: int | None,
    page_height_px: int | None,
    theme: str | None,
    scale_factor: float | None,
) -> RenderConfig | None:
    if all(v is None for v in (page_mode, width, page_height_px, theme, scale_factor)):
        return None
    if width is not None and not (320 <= width <= 7680):
        raise HTTPException(status_code=400, detail="width must be between 320 and 7680")
    if page_height_px is not None and not (200 <= page_height_px <= 20000):
        raise HTTPException(status_code=400, detail="page_height_px must be between 200 and 20000")
    if scale_factor is not None and not (0.5 <= scale_factor <= 4.0):
        raise HTTPException(status_code=400, detail="scale_factor must be between 0.5 and 4.0")
    if page_mode is not None and page_mode.lower() not in {"auto", "a4", "css", "paged"}:
        raise HTTPException(status_code=400, detail="page_mode must be one of: auto, a4, css, paged")
    return RenderConfig(
        width=width,
        page_mode=page_mode.lower() if page_mode else None,
        page_height_px=page_height_px,
        theme=theme.lower() if theme else None,
        scale_factor=scale_factor,
    )


@app.on_event("startup")
async def _startup():
    await _renderer.start()


@app.on_event("shutdown")
async def _shutdown():
    await _renderer.stop()


@app.get("/healthz", tags=["health"])
async def healthz():
    return {"status": "ok", "reports_loaded": len(_report_by_uid)}


@app.get("/readyz", tags=["health"])
async def readyz():
    config_exists = Path(settings.config_path).exists()
    ready = config_exists and bool(_report_by_uid)
    if not ready:
        raise HTTPException(status_code=503, detail="Configuration missing or no reports configured")
    return {"status": "ready"}


@app.get("/queue", tags=["health"])
async def queue_status(_: RequireApiKey):
    if _job_queue is None:
        return {"status": "no queue (standalone API mode)", "concurrency": 1}
    return _job_queue.status()


@app.get("/{uid}")
@app.get("/{uid}/")
async def pdf_export(
    uid: str,
    request: Request,
    background_tasks: BackgroundTasks,
    _: RequireApiKey,
    from_: str = Query("now-7d", alias="from"),
    to: str = "now",
    timezone: str = Query(
        "browser",
        description="IANA timezone, or Grafana aliases: browser/default/utc (browser→DEFAULT_TZ)",
    ),
    vars: str = Query("", description="Dashboard variables in format: key1=value1,key2=value2"),
    page_mode: str | None = Query(None, description="PDF size mode override: auto|a4|css|paged"),
    width: int | None = Query(None, description="Viewport/PDF width in CSS px"),
    page_height_px: int | None = Query(None, description="Page height in CSS px for paged mode"),
    theme: str | None = Query(None, description="Grafana theme override, e.g. light|dark"),
    scale_factor: float | None = Query(None, description="device_scale_factor for sharper canvases"),
):
    if not is_valid_dashboard_uid(uid):
        raise HTTPException(status_code=400, detail="Invalid dashboard uid")

    tz = resolve_timezone(timezone, _timezone_name)

    try:
        normalize_time_range(from_, to, tz)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid time range: {e}") from e

    render_overrides = _parse_render_overrides(page_mode, width, page_height_px, theme, scale_factor)
    vars_dict = _vars_from_request(request, vars)

    report = _report_by_uid.get(uid)

    if not report:
        logger.debug("Dashboard %s not in config, creating ad-hoc report", uid)
        dashboard_info = await asyncio.to_thread(_fetch_dashboard_info_sync, uid)
        dashboard_name = dashboard_info.get("title") if dashboard_info else None

        if not dashboard_name:
            dashboard_name = f"Dashboard {uid}"
            logger.debug("Could not fetch dashboard name, using default: %s", dashboard_name)

        filename_prefix = sanitize_filename(dashboard_name)

        report = ReportConfig(
            name=dashboard_name,
            dashboard_uid=uid,
            vars=vars_dict,
            time_from=from_,
            time_to=to,
            cron="",
            email=EmailConfig(
                to=[],
                subject="",
                body="",
                filename_prefix=filename_prefix,
            ),
        )
    elif vars_dict:
        # Allow API / Grafana var-* params to override configured ones for this request
        report = ReportConfig(
            name=report.name,
            dashboard_uid=report.dashboard_uid,
            vars={**report.vars, **vars_dict},
            time_from=report.time_from,
            time_to=report.time_to,
            cron=report.cron,
            email=report.email,
            render=report.render,
        )

    try:
        if _job_queue is not None:
            pdf_path = await _job_queue.submit(
                f"API: {report.name}",
                lambda r=report, t=tz, f=from_, tt=to, ro=render_overrides: _runner.generate_pdf(
                    r, t, time_from=f, time_to=tt, send_mail=False, render_overrides=ro
                ),
            )
        else:
            pdf_path = await _runner.generate_pdf(
                report,
                tz,
                time_from=from_,
                time_to=to,
                send_mail=False,
                render_overrides=render_overrides,
            )
    except QueueFullError as e:
        raise HTTPException(status_code=429, detail=str(e)) from e

    background_tasks.add_task(_cleanup_file, pdf_path)
    return FileResponse(
        pdf_path,
        media_type="application/pdf",
        filename=pdf_path.name,
        content_disposition_type="inline",
    )
