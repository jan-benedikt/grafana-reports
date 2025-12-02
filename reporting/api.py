from __future__ import annotations

import re
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from zoneinfo import ZoneInfo

from .email_service import EmailService
from .models import ReportConfig, load_reports_config
from .pdf_renderer import GrafanaPDFRenderer
from .report_service import ReportRunner
from .settings import settings

try:
    import requests
except ImportError:
    requests = None  # type: ignore

app = FastAPI(title="Grafana PDF Export")

_timezone_name, _reports = load_reports_config(settings.config_path, settings.default_tz)
_default_tz = ZoneInfo(_timezone_name)
_report_by_uid = {r.dashboard_uid: r for r in _reports}

_renderer = GrafanaPDFRenderer(settings)
_mailer = EmailService(settings)
_runner = ReportRunner(settings, _renderer, _mailer)


def _cleanup_file(path):
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass


def _fetch_dashboard_info(uid: str) -> dict | None:
    """Fetch dashboard name and other metadata from Grafana API."""
    if not (settings.grafana_user and settings.grafana_password):
        print(f"[WARN] No GRAFANA_USER/GRAFANA_PASSWORD configured, cannot fetch dashboard info for {uid}")
        return None

    if requests is None:
        print(f"[WARN] requests library not available, cannot fetch dashboard info for {uid}")
        return None

    try:
        url = f"{settings.grafana_url}/api/dashboards/uid/{uid}"

        print(f"[DEBUG] Fetching dashboard info from {url}")
        response = requests.get(
            url,
            auth=(settings.grafana_user, settings.grafana_password),
            timeout=10
        )

        if response.status_code == 200:
            data = response.json()
            dashboard = data.get("dashboard", {})
            title = dashboard.get("title", "")
            print(f"[DEBUG] Dashboard {uid} title: {title}")
            return {
                "title": title,
                "uid": uid,
                "url": data.get("meta", {}).get("url", ""),
            }
        else:
            print(f"[WARN] Failed to fetch dashboard {uid}: HTTP {response.status_code}")
            return None
    except Exception as e:
        print(f"[WARN] Error fetching dashboard info for {uid}: {e}")
        return None


def _sanitize_filename(name: str) -> str:
    """Convert dashboard name to safe filename."""
    # Remove or replace unsafe characters
    safe = re.sub(r'[^\w\s-]', '', name)
    # Replace spaces with hyphens
    safe = re.sub(r'[-\s]+', '-', safe)
    return safe.lower().strip('-')


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


@app.get("/{uid}")
@app.get("/{uid}/")
async def pdf_export(
    uid: str,
    background_tasks: BackgroundTasks,
    from_: str = Query("now-7d", alias="from"),
    to: str = "now",
    timezone: str = "Europe/Prague",
    vars: str = Query("", description="Dashboard variables in format: key1=value1,key2=value2"),
):
    try:
        tz = ZoneInfo(timezone)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid timezone")

    # Try to get report from config first
    report = _report_by_uid.get(uid)

    # If not in config, create an ad-hoc report configuration
    if not report:
        print(f"[DEBUG] Dashboard {uid} not in config, creating ad-hoc report")

        # Fetch dashboard info from Grafana API
        dashboard_info = _fetch_dashboard_info(uid)
        dashboard_name = dashboard_info.get("title") if dashboard_info else None

        if not dashboard_name:
            dashboard_name = f"Dashboard {uid}"
            print(f"[DEBUG] Could not fetch dashboard name, using default: {dashboard_name}")

        filename_prefix = _sanitize_filename(dashboard_name)

        # Parse variables from query string
        vars_dict = {}
        if vars:
            try:
                for pair in vars.split(","):
                    if "=" in pair:
                        key, value = pair.split("=", 1)
                        vars_dict[key.strip()] = value.strip()
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"Invalid vars format: {e}")

        # Create temporary report config
        from .models import EmailConfig, ReportConfig
        report = ReportConfig(
            name=dashboard_name,
            dashboard_uid=uid,
            vars=vars_dict,
            time_from=from_,
            time_to=to,
            cron="",  # Not used for ad-hoc reports
            email=EmailConfig(
                to=[],
                subject="",
                body="",
                filename_prefix=filename_prefix,
            ),
        )

    pdf_path = await _runner.generate_pdf(report, tz, time_from=from_, time_to=to, send_mail=False)
    background_tasks.add_task(_cleanup_file, pdf_path)
    return FileResponse(pdf_path, media_type="application/pdf", filename=pdf_path.name)
