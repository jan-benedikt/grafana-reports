from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional

from jinja2 import Environment, BaseLoader
from zoneinfo import ZoneInfo

from .email_service import EmailService
from .models import ReportConfig
from .pdf_renderer import GrafanaPDFRenderer
from .settings import Settings
from .time_range import TimeRangeSpec, normalize_time_range

_env = Environment(loader=BaseLoader(), autoescape=False)


def _render_template(tpl: str, ctx: dict) -> str:
    return _env.from_string(tpl).render(**ctx)


class ReportRunner:
    def __init__(self, settings: Settings, renderer: GrafanaPDFRenderer, mailer: EmailService):
        self.settings = settings
        self.renderer = renderer
        self.mailer = mailer

    def build_dashboard_url(self, dashboard_uid: str, vars_map: dict, t_from: str, t_to: str) -> str:
        from urllib.parse import urlencode

        q = {
            "from": t_from,
            "to": t_to,
            "orgId": self.settings.grafana_org_id,
            "refresh": "off",
        }
        if self.settings.kiosk_mode not in ("0", "off", "false", ""):
            q["kiosk"] = self.settings.kiosk_mode
        for key, value in vars_map.items():
            q[f"var-{key}"] = value
        return f"{self.settings.grafana_url}/d/{dashboard_uid}?{urlencode(q, doseq=True)}"

    async def generate_pdf(
        self,
        report: ReportConfig,
        tz: ZoneInfo,
        time_from: Optional[str] = None,
        time_to: Optional[str] = None,
        send_mail: bool = True,
    ) -> Path:
        print(f"[DEBUG] generate_pdf called for {report.name}")
        range_spec = normalize_time_range(time_from or report.time_from, time_to or report.time_to, tz)
        print(f"[DEBUG] Time range normalized: {range_spec.grafana_from} to {range_spec.grafana_to}")
        pdf_path = self._output_path(report)
        print(f"[DEBUG] Output path: {pdf_path}")

        header_title = report.name
        header_period = f"{range_spec.start.strftime('%Y-%m-%d %H:%M')} → {range_spec.end.strftime('%Y-%m-%d %H:%M')} ({tz.key})"
        header_generated = datetime.now(tz).strftime("%Y-%m-%d %H:%M")

        url = self.build_dashboard_url(report.dashboard_uid, report.vars, range_spec.grafana_from, range_spec.grafana_to)
        print(f"[DEBUG] Dashboard URL built: {url}")
        print(f"[DEBUG] Starting render_url for {report.name}")
        await self.renderer.render_url(url, pdf_path, header_title, header_period, header_generated)
        print(f"[DEBUG] render_url completed for {report.name}")

        print(f"[DEBUG] Preparing email context for {report.name}")
        ctx = {
            "name": report.name,
            "dashboard_uid": report.dashboard_uid,
            "vars": report.vars,
            "period_from": range_spec.start,
            "period_to": range_spec.end,
            "period_from_human": range_spec.start.strftime("%Y-%m-%d %H:%M"),
            "period_to_human": range_spec.end.strftime("%Y-%m-%d %H:%M"),
        }
        print(f"[DEBUG] Rendering email templates for {report.name}")
        body = _render_template(report.email.body, ctx)
        body_html = _render_template(report.email.body_html, ctx) if report.email.body_html else None
        email_ctx = {**ctx, "text_body": body, "html_body": body_html}
        print(f"[DEBUG] Email templates rendered for {report.name}")

        if send_mail and self.settings.email_enabled and report.email.to:
            subject = _render_template(report.email.subject, ctx)
            print(f"[DEBUG] Sending email to {report.email.to} with subject: {subject}")
            self.mailer.send(report.email.to, subject, body, pdf_path, context=email_ctx, html_body=body_html)
            print(f"[DEBUG] Email sent successfully for {report.name}")
        elif send_mail:
            print(f"[DEBUG] Email not sent for {report.name}: email_enabled={self.settings.email_enabled}, has_recipients={bool(report.email.to)}")

        return pdf_path

    def _output_path(self, report: ReportConfig) -> Path:
        timestamp = datetime.now(self.settings.zoneinfo()).strftime("%Y-%m-%d_%H%M")
        filename = f"{report.safe_filename_prefix}-{timestamp}.pdf"
        return self.settings.pdf_outdir / filename


async def run_all_reports(
    runner: ReportRunner,
    reports: Iterable[ReportConfig],
    tz: ZoneInfo,
    concurrency: int,
) -> None:
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _guard(r: ReportConfig):
        async with sem:
            try:
                await runner.generate_pdf(r, tz, send_mail=True)
                print(f"[OK] {r.name}: pdf ready")
            except Exception as exc:
                print(f"[ERROR] {r.name}: {exc}")

    await asyncio.gather(*[_guard(r) for r in reports])
