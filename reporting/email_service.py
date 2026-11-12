from __future__ import annotations

import base64
import logging
import mimetypes
import smtplib
import ssl
import time
from email.message import EmailMessage
from email.utils import parseaddr
from pathlib import Path
from typing import Any

from jinja2 import BaseLoader, select_autoescape
from jinja2.sandbox import SandboxedEnvironment

from .settings import Settings

logger = logging.getLogger(__name__)

try:  # Optional dependency, only needed for SendGrid backend
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore


class EmailService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._http_session = None
        self._plain_env = SandboxedEnvironment(loader=BaseLoader(), autoescape=False)
        self._html_env = SandboxedEnvironment(
            loader=BaseLoader(),
            autoescape=select_autoescape(["html", "xml"]),
        )

    def send(
        self,
        to_list: list[str],
        subject: str,
        text_body: str,
        pdf_file: Path,
        *,
        context: dict[str, Any] | None = None,
        html_body: str | None = None,
    ) -> None:
        backend = self.settings.email_backend
        logger.debug("EmailService.send: backend=%s to=%s pdf=%s", backend, to_list, pdf_file.name)
        rendered_html = self._resolve_html_body(html_body, context)
        inline_images = self._load_inline_images()
        logger.debug(
            "Email prepared: html_body=%s inline_images=%d",
            bool(rendered_html),
            len(inline_images),
        )

        if backend == "sendgrid":
            self._send_via_sendgrid(to_list, subject, text_body, rendered_html, inline_images, pdf_file)
        else:
            self._send_via_smtp(to_list, subject, text_body, rendered_html, inline_images, pdf_file)
        logger.info("Email delivered via %s to %s", backend, to_list)

    def _resolve_html_body(self, provided_html: str | None, context: dict[str, Any] | None) -> str | None:
        if provided_html:
            return self._html_env.from_string(provided_html).render(**(context or {}))

        if self.settings.email_template_mode != "html":
            return None

        template_path = self.settings.email_template_html_path
        if template_path and template_path.exists():
            source = template_path.read_text(encoding="utf-8")
            return self._html_env.from_string(source).render(**(context or {}))

        fallback = "<p>{{ text_body | default('') }}</p>"
        return self._html_env.from_string(fallback).render(**(context or {}))

    def _send_via_smtp(
        self,
        to_list: list[str],
        subject: str,
        text_body: str,
        html_body: str | None,
        inline_images: list[dict[str, Any]],
        pdf_file: Path,
    ) -> None:
        msg = EmailMessage()
        msg["From"] = self.settings.email_from
        msg["To"] = ", ".join(to_list)
        msg["Subject"] = subject
        msg.set_content(text_body)

        if html_body:
            html_part = msg.add_alternative(html_body, subtype="html")
            for image in inline_images:
                html_part.add_related(
                    image["data"],
                    maintype=image["maintype"],
                    subtype=image["subtype"],
                    cid=image["cid"],
                    filename=image["filename"],
                )

        with open(pdf_file, "rb") as handle:
            msg.add_attachment(
                handle.read(),
                maintype="application",
                subtype="pdf",
                filename=pdf_file.name,
            )

        tls_mode = self.settings.smtp_tls_mode
        if tls_mode not in {"starttls", "ssl", "none"}:
            raise RuntimeError(f"Invalid SMTP_TLS_MODE '{tls_mode}' (expected starttls|ssl|none)")

        context = ssl.create_default_context()
        if tls_mode == "ssl":
            with smtplib.SMTP_SSL(
                self.settings.smtp_host,
                self.settings.smtp_port,
                context=context,
            ) as server:
                if self.settings.smtp_user:
                    server.login(self.settings.smtp_user, self.settings.smtp_pass)
                server.send_message(msg)
            return

        with smtplib.SMTP(self.settings.smtp_host, self.settings.smtp_port) as server:
            if tls_mode == "starttls":
                try:
                    server.starttls(context=context)
                except smtplib.SMTPException as exc:
                    if self.settings.smtp_user:
                        raise RuntimeError(
                            "SMTP STARTTLS failed but SMTP_USER is set; refusing to send credentials in plaintext"
                        ) from exc
                    logger.warning("SMTP STARTTLS failed and no credentials configured; continuing unencrypted: %s", exc)
            if self.settings.smtp_user:
                if tls_mode == "none":
                    raise RuntimeError(
                        "SMTP_USER is set but SMTP_TLS_MODE=none; refusing to send credentials in plaintext"
                    )
                server.login(self.settings.smtp_user, self.settings.smtp_pass)
            server.send_message(msg)

    def _send_via_sendgrid(
        self,
        to_list: list[str],
        subject: str,
        text_body: str,
        html_body: str | None,
        inline_images: list[dict[str, Any]],
        pdf_file: Path,
    ) -> None:
        if not self.settings.sendgrid_api_key:
            raise RuntimeError("SENDGRID_API_KEY missing but email backend is set to sendgrid")
        if requests is None:
            raise RuntimeError("The 'requests' package is required for SendGrid support")

        from_email = self.settings.sendgrid_from or self._extract_plain_email(self.settings.email_from)
        if not from_email:
            raise RuntimeError("Unable to determine From email for SendGrid")

        reply_to = self.settings.sendgrid_reply_to or None

        with open(pdf_file, "rb") as handle:
            attachment_content = base64.b64encode(handle.read()).decode("ascii")

        contents = [
            {
                "type": "text/plain",
                "value": text_body,
            }
        ]
        if html_body:
            contents.append({"type": "text/html", "value": html_body})

        attachments = [
            {
                "content": attachment_content,
                "type": "application/pdf",
                "filename": pdf_file.name,
                "disposition": "attachment",
            }
        ]

        for image in inline_images:
            attachments.append(
                {
                    "content": base64.b64encode(image["data"]).decode("ascii"),
                    "type": f"{image['maintype']}/{image['subtype']}",
                    "filename": image["filename"],
                    "disposition": "inline",
                    "content_id": image["cid"],
                }
            )

        payload = {
            "personalizations": [
                {
                    "to": [{"email": addr} for addr in to_list],
                }
            ],
            "from": {"email": from_email},
            "subject": subject,
            "content": contents,
            "attachments": attachments,
        }

        if reply_to:
            payload["reply_to"] = {"email": reply_to}

        session = self._http_session or requests.Session()
        self._http_session = session
        logger.debug("Sending to SendGrid API: %d recipients, %d attachments", len(to_list), len(attachments))

        max_retries = 3
        retry_delay = 2
        last_error = None

        for attempt in range(1, max_retries + 1):
            try:
                logger.debug("SendGrid attempt %d/%d", attempt, max_retries)
                response = session.post(
                    "https://api.sendgrid.com/v3/mail/send",
                    headers={"Authorization": f"Bearer {self.settings.sendgrid_api_key}"},
                    json=payload,
                    timeout=30,
                )
                logger.debug("SendGrid response: %s", response.status_code)
                if response.status_code >= 400:
                    body = (response.text or "")[:300]
                    raise RuntimeError(f"SendGrid API error {response.status_code}: {body}")
                return
            except requests.exceptions.RequestException as e:
                last_error = e
                logger.warning("SendGrid request error on attempt %d/%d: %s", attempt, max_retries, e)
                if attempt < max_retries:
                    wait_time = retry_delay * attempt
                    logger.debug("Retrying in %d seconds...", wait_time)
                    time.sleep(wait_time)
                    session = requests.Session()
                    self._http_session = session

        raise RuntimeError(f"Failed to send email after {max_retries} attempts: {last_error}")

    def _load_inline_images(self) -> list[dict[str, Any]]:
        images_dir = self.settings.email_template_images_dir
        if not images_dir or not images_dir.exists():
            return []

        collected: list[dict[str, Any]] = []
        for path in images_dir.iterdir():
            if not path.is_file():
                continue
            mime, _ = mimetypes.guess_type(path.name)
            if not mime:
                continue
            maintype, subtype = mime.split("/", 1)
            with open(path, "rb") as handle:
                collected.append(
                    {
                        "cid": path.name,
                        "filename": path.name,
                        "maintype": maintype,
                        "subtype": subtype,
                        "data": handle.read(),
                    }
                )
        return collected

    @staticmethod
    def _extract_plain_email(value: str) -> str | None:
        _, email = parseaddr(value)
        return email or value
