from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml


@dataclass(slots=True)
class EmailConfig:
    to: List[str] = field(default_factory=list)
    subject: str = "{{ name }} – {{ period_from_human }} → {{ period_to_human }}"
    body: str = "Report period: {{ period_from_human }} – {{ period_to_human }}."
    body_html: Optional[str] = None
    filename_prefix: Optional[str] = None


@dataclass(slots=True)
class ReportConfig:
    name: str
    dashboard_uid: str
    vars: Dict[str, Any] = field(default_factory=dict)
    time_from: str = "now-7d"
    time_to: str = "now"
    cron: str = "0 7 1 * *"
    email: EmailConfig = field(default_factory=EmailConfig)

    @property
    def safe_filename_prefix(self) -> str:
        return (self.email.filename_prefix or self.name).lower().replace(" ", "-")


def load_reports_config(path: Path, default_tz: str) -> Tuple[str, List[ReportConfig]]:
    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}

    timezone = (raw.get("timezone") or default_tz).strip()

    reports: List[ReportConfig] = []
    for item in raw.get("reports", []):
        email_raw = item.get("email", {}) or {}
        report = ReportConfig(
            name=item["name"],
            dashboard_uid=item["dashboard_uid"],
            vars=item.get("vars", {}) or {},
            time_from=item.get("from", "now-30d"),
            time_to=item.get("to", "now"),
            cron=item.get("cron", "0 7 1 * *"),
            email=EmailConfig(
                to=list(email_raw.get("to", [])),
                subject=email_raw.get("subject", EmailConfig.subject),
                body=email_raw.get("body", EmailConfig.body),
                body_html=email_raw.get("body_html"),
                filename_prefix=email_raw.get("filename_prefix"),
            ),
        )
        reports.append(report)

    return timezone, reports
