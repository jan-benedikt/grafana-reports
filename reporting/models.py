from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from apscheduler.triggers.cron import CronTrigger

from .time_range import normalize_time_range
from .util import is_valid_dashboard_uid, sanitize_filename

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class EmailConfig:
    to: list[str] = field(default_factory=list)
    subject: str = "{{ name }} – {{ period_from_human }} → {{ period_to_human }}"
    body: str = "Report period: {{ period_from_human }} – {{ period_to_human }}."
    body_html: str | None = None
    filename_prefix: str | None = None


@dataclass(slots=True)
class RenderConfig:
    """Optional per-report overrides for PDF rendering."""

    width: int | None = None
    page_mode: str | None = None
    page_height_px: int | None = None
    theme: str | None = None
    scale_factor: float | None = None


@dataclass(slots=True)
class ReportConfig:
    name: str
    dashboard_uid: str
    vars: dict[str, Any] = field(default_factory=dict)
    time_from: str = "now-7d"
    time_to: str = "now"
    cron: str = "0 7 1 * *"
    email: EmailConfig = field(default_factory=EmailConfig)
    render: RenderConfig = field(default_factory=RenderConfig)

    @property
    def safe_filename_prefix(self) -> str:
        return sanitize_filename(self.email.filename_prefix or self.name)


class ConfigError(ValueError):
    """Raised when reports config.yml is invalid."""


def _parse_render(raw: dict[str, Any] | None) -> RenderConfig:
    raw = raw or {}
    width = raw.get("width")
    page_height = raw.get("page_height_px")
    scale = raw.get("scale_factor")
    return RenderConfig(
        width=int(width) if width is not None else None,
        page_mode=(str(raw["page_mode"]).lower() if raw.get("page_mode") else None),
        page_height_px=int(page_height) if page_height is not None else None,
        theme=(str(raw["theme"]).lower() if raw.get("theme") else None),
        scale_factor=float(scale) if scale is not None else None,
    )


def load_reports_config(path: Path, default_tz: str) -> tuple[str, list[ReportConfig]]:
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")

    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}

    timezone = (raw.get("timezone") or default_tz).strip()
    if not timezone:
        raise ConfigError("timezone must not be empty")

    from zoneinfo import ZoneInfo

    try:
        tz = ZoneInfo(timezone)
    except Exception as exc:
        raise ConfigError(f"Invalid timezone '{timezone}': {exc}") from exc

    reports: list[ReportConfig] = []
    for idx, item in enumerate(raw.get("reports", []) or []):
        if not isinstance(item, dict):
            raise ConfigError(f"reports[{idx}] must be a mapping")
        name = item.get("name")
        uid = item.get("dashboard_uid")
        if not name:
            raise ConfigError(f"reports[{idx}] missing required field 'name'")
        if not uid:
            raise ConfigError(f"reports[{idx}] ('{name}') missing required field 'dashboard_uid'")
        if not is_valid_dashboard_uid(str(uid)):
            raise ConfigError(f"reports[{idx}] ('{name}') has invalid dashboard_uid '{uid}'")

        time_from = item.get("from", "now-30d")
        time_to = item.get("to", "now")
        try:
            normalize_time_range(str(time_from), str(time_to), tz)
        except Exception as exc:
            raise ConfigError(f"reports[{idx}] ('{name}') invalid time range: {exc}") from exc

        cron = item.get("cron", "0 7 1 * *")
        if cron:
            try:
                CronTrigger.from_crontab(str(cron), timezone=timezone)
            except Exception as exc:
                raise ConfigError(f"reports[{idx}] ('{name}') invalid cron '{cron}': {exc}") from exc

        email_raw = item.get("email", {}) or {}
        report = ReportConfig(
            name=str(name),
            dashboard_uid=str(uid),
            vars=item.get("vars", {}) or {},
            time_from=str(time_from),
            time_to=str(time_to),
            cron=str(cron),
            email=EmailConfig(
                to=list(email_raw.get("to", [])),
                subject=email_raw.get("subject", EmailConfig.subject),
                body=email_raw.get("body", EmailConfig.body),
                body_html=email_raw.get("body_html"),
                filename_prefix=email_raw.get("filename_prefix"),
            ),
            render=_parse_render(item.get("render")),
        )
        reports.append(report)

    logger.info("Loaded %d report(s) from %s (tz=%s)", len(reports), path, timezone)
    return timezone, reports
