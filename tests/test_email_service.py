from __future__ import annotations

import pytest
import requests as real_requests

from reporting import email_service as email_service_module
from reporting.email_service import EmailService
from reporting.settings import Settings


class FakeResponse:
    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text


class FakeSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append({"url": url, "json": json})
        resp = self._responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp


@pytest.fixture()
def sendgrid_service(tmp_path, monkeypatch):
    settings = Settings(
        email_backend="sendgrid",
        sendgrid_api_key="sg-test",
        sendgrid_from="Grafana Reports <noreply@example.com>",
        sendgrid_reply_to="Ops Team <ops@example.com>",
        pdf_outdir=tmp_path,
    )
    svc = EmailService(settings)
    monkeypatch.setattr(email_service_module.time, "sleep", lambda s: None)
    pdf = tmp_path / "r.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")

    def _with_session(session: FakeSession):
        svc._http_session = session
        # Prevent retry logic from creating a real requests.Session

        class _FakeModule:
            exceptions = real_requests.exceptions

            @staticmethod
            def Session():
                return session

        monkeypatch.setattr(email_service_module, "requests", _FakeModule)
        return svc, pdf

    return _with_session


def test_sendgrid_from_with_display_name_is_parsed(sendgrid_service):
    session = FakeSession([FakeResponse(202)])
    svc, pdf = sendgrid_service(session)

    svc.send(["to@example.com"], "subj", "body", pdf)

    payload = session.calls[0]["json"]
    assert payload["from"] == {"email": "noreply@example.com", "name": "Grafana Reports"}
    assert payload["reply_to"] == {"email": "ops@example.com"}


def test_sendgrid_client_error_is_not_retried(sendgrid_service):
    session = FakeSession([FakeResponse(400, "bad payload")])
    svc, pdf = sendgrid_service(session)

    with pytest.raises(RuntimeError, match="400"):
        svc.send(["to@example.com"], "subj", "body", pdf)
    assert len(session.calls) == 1


def test_sendgrid_server_error_is_retried(sendgrid_service):
    session = FakeSession([FakeResponse(500), FakeResponse(500), FakeResponse(202)])
    svc, pdf = sendgrid_service(session)

    svc.send(["to@example.com"], "subj", "body", pdf)
    assert len(session.calls) == 3
