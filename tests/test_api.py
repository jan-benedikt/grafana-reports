from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.testclient import TestClient

from reporting.auth import verify_api_key
from reporting.settings import Settings
from reporting.util import is_valid_dashboard_uid


def _build_test_app(settings: Settings) -> tuple[FastAPI, object]:
    """Minimal FastAPI app mirroring auth + route ordering of reporting.api."""
    app = FastAPI()
    runner = type("Runner", (), {})()
    runner.generate_pdf = AsyncMock()

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok", "reports_loaded": 1}

    @app.get("/queue")
    async def queue_status(
        authorization: str | None = Header(default=None),
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ):
        verify_api_key(authorization=authorization, x_api_key=x_api_key, cfg=settings)
        return {"status": "no queue (standalone API mode)", "concurrency": 1}

    @app.get("/{uid}")
    async def pdf_export(
        uid: str,
        background_tasks: BackgroundTasks,
        authorization: str | None = Header(default=None),
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
        from_: str = Query("now-7d", alias="from"),
        to: str = "now",
    ):
        verify_api_key(authorization=authorization, x_api_key=x_api_key, cfg=settings)
        if not is_valid_dashboard_uid(uid):
            raise HTTPException(status_code=400, detail="Invalid dashboard uid")
        pdf_path = await runner.generate_pdf()
        return FileResponse(
            pdf_path,
            media_type="application/pdf",
            filename=pdf_path.name,
            content_disposition_type="inline",
        )

    return app, runner


@pytest.fixture()
def api_client(tmp_path):
    settings = Settings(api_keys=["test-secret-key"], pdf_outdir=tmp_path / "pdfs")
    settings.pdf_outdir.mkdir(parents=True, exist_ok=True)
    app, runner = _build_test_app(settings)
    with TestClient(app) as client:
        yield client, runner, settings


def test_healthz_public(api_client):
    client, _, _ = api_client
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_queue_requires_auth(api_client):
    client, _, _ = api_client
    assert client.get("/queue").status_code == 401
    resp = client.get("/queue", headers={"X-API-Key": "test-secret-key"})
    assert resp.status_code == 200


def test_pdf_export_requires_auth(api_client):
    client, _, _ = api_client
    assert client.get("/abc123").status_code == 401


def test_pdf_export_rejects_bad_uid(api_client):
    client, runner, _ = api_client
    resp = client.get(
        "/bad$uid!",
        headers={"Authorization": "Bearer test-secret-key"},
    )
    assert resp.status_code == 400
    runner.generate_pdf.assert_not_awaited()


def test_pdf_export_success(api_client):
    client, runner, settings = api_client
    pdf = settings.pdf_outdir / "out.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")
    runner.generate_pdf = AsyncMock(return_value=pdf)

    resp = client.get(
        "/abc123?from=now-1d&to=now",
        headers={"X-API-Key": "test-secret-key"},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/pdf")
    runner.generate_pdf.assert_awaited()


def test_queue_route_not_shadowed_by_uid(api_client):
    client, runner, _ = api_client
    resp = client.get("/queue", headers={"X-API-Key": "test-secret-key"})
    assert resp.status_code == 200
    runner.generate_pdf.assert_not_awaited()


def test_pdf_export_accepts_grafana_browser_timezone(api_client):
    """Grafana 13.2+ sends timezone=browser — must not 400."""
    from reporting.util import resolve_timezone

    client, runner, settings = api_client
    pdf = settings.pdf_outdir / "out.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")
    runner.generate_pdf = AsyncMock(return_value=pdf)

    # Resolve the same way production API does
    tz = resolve_timezone("browser", "Europe/Prague")
    assert tz.key == "Europe/Prague"

    resp = client.get(
        "/abc123?from=now-30d&to=now&timezone=browser&var-data_source=prometheus",
        headers={"X-API-Key": "test-secret-key"},
    )
    assert resp.status_code == 200
