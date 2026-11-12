from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo


def _bool_env(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default).lower()).lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _path_env(name: str, default: str | None = None) -> Path | None:
    value = os.getenv(name, default)
    if not value:
        return None
    return Path(value).expanduser().resolve()


def _list_env(name: str, default: str = "") -> list[str]:
    raw = os.getenv(name, default)
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


@dataclass(slots=True)
class Settings:
    grafana_url: str = field(default_factory=lambda: os.getenv("GRAFANA_URL", "https://grafana.example.com").rstrip("/"))
    grafana_user: str = field(default_factory=lambda: os.getenv("GRAFANA_USER", "report-user"))
    grafana_password: str = field(default_factory=lambda: os.getenv("GRAFANA_PASSWORD", ""))
    grafana_token: str = field(default_factory=lambda: os.getenv("GRAFANA_TOKEN", ""))
    grafana_org_id: str = field(default_factory=lambda: os.getenv("GRAFANA_ORG_ID", "1"))

    api_keys: list[str] = field(default_factory=lambda: _list_env("API_KEYS"))

    email_backend: str = field(default_factory=lambda: os.getenv("EMAIL_BACKEND", "smtp").lower())
    email_template_mode: str = field(default_factory=lambda: os.getenv("EMAIL_TEMPLATE_MODE", "plain").lower())
    email_template_html_path: Path | None = field(default_factory=lambda: _path_env("EMAIL_TEMPLATE_HTML_PATH"))
    email_template_images_dir: Path | None = field(default_factory=lambda: _path_env("EMAIL_TEMPLATE_IMAGES_DIR"))
    smtp_host: str = field(default_factory=lambda: os.getenv("SMTP_HOST", "localhost"))
    smtp_port: int = field(default_factory=lambda: _int_env("SMTP_PORT", 25))
    smtp_user: str = field(default_factory=lambda: os.getenv("SMTP_USER", ""))
    smtp_pass: str = field(default_factory=lambda: os.getenv("SMTP_PASS", ""))
    smtp_tls_mode: str = field(default_factory=lambda: os.getenv("SMTP_TLS_MODE", "starttls").lower())
    sendgrid_api_key: str = field(default_factory=lambda: os.getenv("SENDGRID_API_KEY", ""))
    sendgrid_from: str = field(default_factory=lambda: os.getenv("SENDGRID_FROM", ""))
    sendgrid_reply_to: str = field(default_factory=lambda: os.getenv("SENDGRID_REPLY_TO", ""))
    email_from: str = field(default_factory=lambda: os.getenv("EMAIL_FROM", "Grafana Reports <reports@example.com>"))
    email_enabled: bool = field(default_factory=lambda: _bool_env("REPORTS_EMAIL_ENABLED", True))

    default_tz: str = field(default_factory=lambda: os.getenv("DEFAULT_TZ", "Europe/Prague"))
    pdf_outdir: Path = field(default_factory=lambda: Path(os.getenv("PDF_OUTDIR", "./")).resolve())
    viewport_width: int = field(default_factory=lambda: _int_env("VIEWPORT_WIDTH", 1920))
    viewport_height: int = field(default_factory=lambda: _int_env("VIEWPORT_HEIGHT", 1080))
    wait_extra_ms: int = field(default_factory=lambda: _int_env("WAIT_EXTRA_MS", 5000))
    pdf_landscape: bool = field(default_factory=lambda: _bool_env("PDF_LANDSCAPE", True))
    pdf_paper_format: str = field(default_factory=lambda: os.getenv("PDF_PAPER_FORMAT", "A4"))
    pdf_size_mode: str = field(default_factory=lambda: os.getenv("PDF_SIZE_MODE", "paged").lower())
    pdf_max_inch: float = field(default_factory=lambda: _float_env("PDF_MAX_INCH", 200.0))
    pdf_css_px_per_in: float = field(default_factory=lambda: _float_env("PDF_CSS_PX_PER_IN", 96.0))
    pdf_media: str = field(default_factory=lambda: os.getenv("PDF_MEDIA", "screen").lower())
    pdf_page_height_px: int = field(default_factory=lambda: _int_env("PDF_PAGE_HEIGHT_PX", 0))
    pdf_device_scale_factor: float = field(default_factory=lambda: _float_env("PDF_DEVICE_SCALE_FACTOR", 2.0))
    pdf_theme: str = field(default_factory=lambda: os.getenv("PDF_THEME", "light").lower())
    pdf_max_viewport_height: int = field(default_factory=lambda: _int_env("PDF_MAX_VIEWPORT_HEIGHT", 30000))

    pdf_fast_mode: bool = field(default_factory=lambda: _bool_env("PDF_FAST_MODE", True))
    ready_timeout_ms: int = field(default_factory=lambda: _int_env("READY_TIMEOUT_MS", 180000))
    network_idle_timeout_ms: int = field(default_factory=lambda: _int_env("NETWORK_IDLE_TIMEOUT_MS", 20000))
    wait_before_ready_ms: int = field(default_factory=lambda: _int_env("WAIT_BEFORE_READY_MS", 0))

    check_queries_to_complete: bool = field(default_factory=lambda: _bool_env("CHECK_QUERIES_TO_COMPLETE", False))
    check_queries_completion_timeout_ms: int = field(
        default_factory=lambda: _int_env("CHECK_QUERIES_TO_COMPLETE_QUERIES_COMPLETION_TIMEOUT", 60000)
    )
    check_queries_stuck_threshold_ms: int = field(
        default_factory=lambda: _int_env("CHECK_QUERIES_TO_COMPLETE_MAX_QUERY_COMPLETION_TIME", 30000)
    )
    check_queries_poll_interval_ms: int = field(
        default_factory=lambda: _int_env("CHECK_QUERIES_TO_COMPLETE_QUERIES_INTERVAL", 2000)
    )

    render_locale: str = field(default_factory=lambda: os.getenv("RENDER_LOCALE", "en-US"))
    render_timezone: str = field(default_factory=lambda: os.getenv("RENDER_TIMEZONE", "Europe/Prague"))
    debug_render: bool = field(default_factory=lambda: _bool_env("DEBUG_RENDER", False))
    debug_screenshot: bool = field(default_factory=lambda: _bool_env("DEBUG_SCREENSHOT", False))
    kiosk_mode: str = field(default_factory=lambda: os.getenv("KIOSK_MODE", "1"))
    force_kiosk_css: bool = field(default_factory=lambda: _bool_env("FORCE_KIOSK_CSS", True))
    run_all_concurrency: int = field(default_factory=lambda: _int_env("RUN_ALL_CONCURRENCY", 2))
    queue_max_pending: int = field(default_factory=lambda: _int_env("QUEUE_MAX_PENDING", 10))
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))

    config_path: Path = field(default_factory=lambda: Path(os.getenv("REPORTS_CONFIG_PATH", "config.yml")))

    def zoneinfo(self, name: str | None = None) -> ZoneInfo:
        return ZoneInfo(name or self.default_tz)

    def effective_page_height_px(self, width: int | None = None) -> int:
        if self.pdf_page_height_px > 0:
            return self.pdf_page_height_px
        w = width or self.viewport_width
        return max(1, int(w / 1.414))


settings = Settings()
settings.pdf_outdir.mkdir(parents=True, exist_ok=True)
