from __future__ import annotations

import base64
import mimetypes
import smtplib
from email.message import EmailMessage
from email.utils import parseaddr
from pathlib import Path
from typing import Dict, List, Optional

from jinja2 import BaseLoader, Environment

from .settings import Settings

try:  # Optional dependency, only needed for SendGrid backend
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore


class EmailService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._http_session = None
        self._string_env = Environment(loader=BaseLoader(), autoescape=False)

    def send(
        self,
        to_list: List[str],
        subject: str,
        text_body: str,
        pdf_file: Path,
        *,
        context: Optional[Dict] = None,
        html_body: Optional[str] = None,
    ) -> None:
        backend = self.settings.email_backend
        print(f"[DEBUG] EmailService.send called: backend={backend}, to={to_list}, pdf={pdf_file.name}")
        rendered_html = self._resolve_html_body(html_body, context)
        inline_images = self._load_inline_images()
        print(f"[DEBUG] Email prepared: html_body={bool(rendered_html)}, inline_images={len(inline_images)}")

        if backend == "sendgrid":
            self._send_via_sendgrid(to_list, subject, text_body, rendered_html, inline_images, pdf_file)
        else:
            self._send_via_smtp(to_list, subject, text_body, rendered_html, inline_images, pdf_file)
        print(f"[DEBUG] Email delivery completed via {backend}")

    def _resolve_html_body(self, provided_html: Optional[str], context: Optional[Dict]) -> Optional[str]:
        if provided_html:
            return self._string_env.from_string(provided_html).render(**(context or {}))

        if self.settings.email_template_mode != "html":
            return None

        template_path = self.settings.email_template_html_path
        if template_path and template_path.exists():
            source = template_path.read_text(encoding="utf-8")
            return self._string_env.from_string(source).render(**(context or {}))

        fallback = "<p>{{ text_body | default('') }}</p>"
        return self._string_env.from_string(fallback).render(**(context or {}))

    def _send_via_smtp(
        self,
        to_list: List[str],
        subject: str,
        text_body: str,
        html_body: Optional[str],
        inline_images: List[dict],
        pdf_file: Path,
    ) -> None:
        msg = EmailMessage()
        msg["From"] = self.settings.email_from
        msg["To"] = ", ".join(to_list)
        msg["Subject"] = subject
        msg.set_content(text_body)

        html_part = None
        if html_body:
            html_part = msg.add_alternative(html_body, subtype="html")
            for image in inline_images:
                (html_part or msg).add_related(
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

        with smtplib.SMTP(self.settings.smtp_host, self.settings.smtp_port) as server:
            try:
                server.starttls()
            except smtplib.SMTPException:
                pass
            if self.settings.smtp_user:
                server.login(self.settings.smtp_user, self.settings.smtp_pass)
            server.send_message(msg)

    def _send_via_sendgrid(
        self,
        to_list: List[str],
        subject: str,
        text_body: str,
        html_body: Optional[str],
        inline_images: List[dict],
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
        print(f"[DEBUG] Sending to SendGrid API: {len(to_list)} recipients, {len(attachments)} attachments")

        # Retry logic for SSL/connection errors
        max_retries = 3
        retry_delay = 2
        last_error = None

        for attempt in range(1, max_retries + 1):
            try:
                print(f"[DEBUG] SendGrid attempt {attempt}/{max_retries}")
                response = session.post(
                    "https://api.sendgrid.com/v3/mail/send",
                    headers={"Authorization": f"Bearer {self.settings.sendgrid_api_key}"},
                    json=payload,
                    timeout=30,
                )
                print(f"[DEBUG] SendGrid response: {response.status_code}")
                if response.status_code >= 400:
                    raise RuntimeError(f"SendGrid API error {response.status_code}: {response.text}")
                return  # Success
            except requests.exceptions.SSLError as e:
                last_error = e
                print(f"[WARN] SendGrid SSL error on attempt {attempt}/{max_retries}: {e}")
                if attempt < max_retries:
                    import time
                    wait_time = retry_delay * attempt
                    print(f"[DEBUG] Retrying in {wait_time} seconds...")
                    time.sleep(wait_time)
                    # Create a fresh session for retry
                    session = requests.Session()
                    self._http_session = session
            except requests.exceptions.RequestException as e:
                last_error = e
                print(f"[WARN] SendGrid request error on attempt {attempt}/{max_retries}: {e}")
                if attempt < max_retries:
                    import time
                    wait_time = retry_delay * attempt
                    print(f"[DEBUG] Retrying in {wait_time} seconds...")
                    time.sleep(wait_time)
                    session = requests.Session()
                    self._http_session = session

        # All retries failed
        raise RuntimeError(f"Failed to send email after {max_retries} attempts: {last_error}")

    def _load_inline_images(self) -> List[dict]:
        images_dir = self.settings.email_template_images_dir
        if not images_dir or not images_dir.exists():
            return []

        collected = []
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
    def _extract_plain_email(value: str) -> Optional[str]:
        _, email = parseaddr(value)
        return email or value
