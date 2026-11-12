from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import pytest

from reporting.models import EmailConfig, RenderConfig, ReportConfig
from reporting.report_service import ReportRunner
from reporting.settings import Settings


@pytest.fixture()
def runner(tmp_path):
    settings = Settings(
        grafana_url="https://grafana.example.com",
        grafana_org_id="1",
        kiosk_mode="1",
        pdf_theme="light",
        pdf_outdir=tmp_path,
        email_enabled=False,
    )
    renderer = MagicMock()
    renderer.render_url = AsyncMock()
    mailer = MagicMock()
    return ReportRunner(settings, renderer, mailer)


def test_build_dashboard_url_includes_theme_timezone_and_vars(runner):
    url = runner.build_dashboard_url(
        "abc123",
        {"data_source": "prometheus", "env": "prd"},
        "now-30d",
        "now",
        theme="dark",
        timezone="Europe/Prague",
    )
    parsed = urlparse(url)
    assert parsed.path == "/d/abc123"
    q = parse_qs(parsed.query)
    assert q["from"] == ["now-30d"]
    assert q["to"] == ["now"]
    assert q["kiosk"] == ["1"]
    assert q["theme"] == ["dark"]
    assert q["timezone"] == ["Europe/Prague"]
    assert q["var-data_source"] == ["prometheus"]
    assert q["var-env"] == ["prd"]
    assert q["refresh"] == ["off"]


def test_build_dashboard_url_omits_disabled_kiosk(tmp_path):
    settings = Settings(
        grafana_url="https://g.example",
        kiosk_mode="off",
        pdf_theme="",
        pdf_outdir=tmp_path,
    )
    runner = ReportRunner(settings, MagicMock(), MagicMock())
    url = runner.build_dashboard_url("uid1", {}, "now-1d", "now")
    q = parse_qs(urlparse(url).query)
    assert "kiosk" not in q
    assert "theme" not in q


@pytest.mark.asyncio
async def test_generate_pdf_passes_render_and_unique_path(runner, tmp_path):
    report = ReportConfig(
        name="Sample",
        dashboard_uid="abc123",
        vars={"team": "windows"},
        time_from="now-7d",
        time_to="now",
        email=EmailConfig(to=[], filename_prefix="sample"),
        render=RenderConfig(page_mode="paged", width=1280),
    )
    tz = ZoneInfo("Europe/Prague")

    path = await runner.generate_pdf(report, tz, send_mail=False)
    assert path.parent == tmp_path
    assert path.name.startswith("sample-")
    assert path.suffix == ".pdf"
    # uuid suffix → unique even within same minute
    assert len(path.stem.split("-")[-1]) == 8

    runner.renderer.render_url.assert_awaited_once()
    args, kwargs = runner.renderer.render_url.await_args
    assert kwargs.get("render") is not None or (len(args) >= 6)
    # URL should include timezone + var
    called_url = args[0]
    q = parse_qs(urlparse(called_url).query)
    assert q["timezone"] == ["Europe/Prague"]
    assert q["var-team"] == ["windows"]


@pytest.mark.asyncio
async def test_generate_pdf_merges_render_overrides(runner):
    report = ReportConfig(
        name="Sample",
        dashboard_uid="abc123",
        email=EmailConfig(filename_prefix="x"),
        render=RenderConfig(page_mode="auto", width=1920),
    )
    overrides = RenderConfig(page_mode="paged", theme="dark")
    await runner.generate_pdf(
        report,
        ZoneInfo("UTC"),
        send_mail=False,
        render_overrides=overrides,
    )
    _, kwargs = runner.renderer.render_url.await_args
    render = kwargs["render"]
    assert render.page_mode == "paged"
    assert render.width == 1920  # from base
    assert render.theme == "dark"
