from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime
from pathlib import Path
from collections.abc import Iterable
from urllib.parse import urlencode

from jinja2 import BaseLoader, select_autoescape
from jinja2.sandbox import SandboxedEnvironment
from zoneinfo import ZoneInfo

from .email_service import EmailService
from .models import RenderConfig, ReportConfig
from .pdf_renderer import GrafanaPDFRenderer
from .settings import Settings
from .time_range import normalize_time_range

logger = logging.getLogger(__name__)

_plain_env = SandboxedEnvironment(loader=BaseLoader(), autoescape=False)
_html_env = SandboxedEnvironment(
    loader=BaseLoader(),
    autoescape=select_autoescape(["html", "xml"]),
)


def _render_template(tpl: str, ctx: dict, *, html: bool = False) -> str:
    env = _html_env if html else _plain_env
    return env.from_string(tpl).render(**ctx)


class ReportRunner:
    def __init__(self, settings: Settings, renderer: GrafanaPDFRenderer, mailer: EmailService):
        self.settings = settings
        self.renderer = renderer
        self.mailer = mailer

    def build_dashboard_url(
        self,
        dashboard_uid: str,
        vars_map: dict,
        t_from: str,
        t_to: str,
        *,
        theme: str | None = None,
        timezone: str | None = None,
    ) -> str:
        q = {
            "from": t_from,
            "to": t_to,
            "orgId": self.settings.grafana_org_id,
            "refresh": "off",
        }
        if self.settings.kiosk_mode not in ("0", "off", "false", ""):
            q["kiosk"] = self.settings.kiosk_mode
        effective_theme = theme or self.settings.pdf_theme
        if effective_theme and effective_theme not in ("0", "off", "false", ""):
            q["theme"] = effective_theme
        # Prefer explicit IANA tz so Grafana doesn't fall back to "browser" in headless Chrome
        if timezone:
            q["timezone"] = timezone
        for key, value in vars_map.items():
            q[f"var-{key}"] = value
        return f"{self.settings.grafana_url}/d/{dashboard_uid}?{urlencode(q, doseq=True)}"

    async def generate_pdf(
        self,
        report: ReportConfig,
        tz: ZoneInfo,
        time_from: str | None = None,
        time_to: str | None = None,
        send_mail: bool = True,
        render_overrides: RenderConfig | None = None,
    ) -> Path:
        logger.info("Generating PDF for %s", report.name)
        range_spec = normalize_time_range(time_from or report.time_from, time_to or report.time_to, tz)
        pdf_path = self._output_path(report)

        header_title = report.name
        header_period = (
            f"{range_spec.start.strftime('%Y-%m-%d %H:%M')} → "
            f"{range_spec.end.strftime('%Y-%m-%d %H:%M')} ({tz.key})"
        )
        header_generated = datetime.now(tz).strftime("%Y-%m-%d %H:%M")

        render = self._merge_render(report.render, render_overrides)
        url = self.build_dashboard_url(
            report.dashboard_uid,
            report.vars,
            range_spec.grafana_from,
            range_spec.grafana_to,
            theme=render.theme,
            timezone=tz.key,
        )
        logger.debug("Dashboard URL: %s", url)
        await self.renderer.render_url(
            url,
            pdf_path,
            header_title,
            header_period,
            header_generated,
            render=render,
        )

        if send_mail and self.settings.email_enabled and report.email.to:
            # Templates are rendered only when an email is actually sent; the
            # API download path must not depend on email configuration.
            ctx = {
                "name": report.name,
                "dashboard_uid": report.dashboard_uid,
                "vars": report.vars,
                "period_from": range_spec.start,
                "period_to": range_spec.end,
                "period_from_human": range_spec.start.strftime("%Y-%m-%d %H:%M"),
                "period_to_human": range_spec.end.strftime("%Y-%m-%d %H:%M"),
            }
            try:
                body = _render_template(report.email.body, ctx, html=False)
                body_html = (
                    _render_template(report.email.body_html, ctx, html=True)
                    if report.email.body_html
                    else None
                )
                email_ctx = {**ctx, "text_body": body, "html_body": body_html}
                subject = _render_template(report.email.subject, ctx, html=False)
                logger.info("Sending email to %s subject=%s", report.email.to, subject)
                await asyncio.to_thread(
                    self.mailer.send,
                    report.email.to,
                    subject,
                    body,
                    pdf_path,
                    context=email_ctx,
                    html_body=body_html,
                )
            finally:
                # Emailed (or failed-to-email) PDFs must not accumulate on disk
                try:
                    pdf_path.unlink(missing_ok=True)
                    logger.debug("Deleted emailed PDF %s", pdf_path)
                except Exception:
                    logger.warning("Failed to delete PDF after email: %s", pdf_path)
        elif send_mail:
            logger.debug(
                "Email not sent for %s: email_enabled=%s has_recipients=%s",
                report.name,
                self.settings.email_enabled,
                bool(report.email.to),
            )

        return pdf_path

    @staticmethod
    def _merge_render(base: RenderConfig, overrides: RenderConfig | None) -> RenderConfig:
        if overrides is None:
            return base
        return RenderConfig(
            width=overrides.width if overrides.width is not None else base.width,
            page_mode=overrides.page_mode if overrides.page_mode is not None else base.page_mode,
            page_height_px=(
                overrides.page_height_px if overrides.page_height_px is not None else base.page_height_px
            ),
            theme=overrides.theme if overrides.theme is not None else base.theme,
            scale_factor=overrides.scale_factor if overrides.scale_factor is not None else base.scale_factor,
        )

    def _output_path(self, report: ReportConfig) -> Path:
        timestamp = datetime.now(self.settings.zoneinfo()).strftime("%Y-%m-%d_%H%M")
        unique = uuid.uuid4().hex[:8]
        filename = f"{report.safe_filename_prefix}-{timestamp}-{unique}.pdf"
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
                logger.info("OK %s: pdf ready", r.name)
            except Exception as exc:
                logger.exception("ERROR %s: %s", r.name, exc)

    await asyncio.gather(*[_guard(r) for r in reports])
