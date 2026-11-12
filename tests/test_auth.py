from __future__ import annotations

import pytest
from fastapi import HTTPException

from reporting.auth import _extract_key, verify_api_key
from reporting.settings import Settings
from reporting.util import is_valid_dashboard_uid, resolve_timezone, sanitize_filename


def test_extract_key_bearer_and_header():
    assert _extract_key("Bearer secret", None) == "secret"
    assert _extract_key(None, "from-header") == "from-header"
    assert _extract_key("Bearer a", "b") == "b"  # X-API-Key wins
    assert _extract_key(None, None) is None


def test_verify_api_key_fail_closed():
    cfg = Settings(api_keys=[])
    with pytest.raises(HTTPException) as ei:
        verify_api_key(authorization=None, x_api_key=None, cfg=cfg)
    assert ei.value.status_code == 503


def test_verify_api_key_accepts_valid():
    cfg = Settings(api_keys=["good-key", "other"])
    assert verify_api_key(authorization="Bearer good-key", x_api_key=None, cfg=cfg) == "good-key"
    assert verify_api_key(authorization=None, x_api_key="other", cfg=cfg) == "other"


def test_verify_api_key_rejects_invalid():
    cfg = Settings(api_keys=["good-key"])
    with pytest.raises(HTTPException) as ei:
        verify_api_key(authorization="Bearer wrong", x_api_key=None, cfg=cfg)
    assert ei.value.status_code == 401


def test_uid_validation():
    assert is_valid_dashboard_uid("abc123")
    assert is_valid_dashboard_uid("a_b-C")
    assert not is_valid_dashboard_uid("")
    assert not is_valid_dashboard_uid("../etc")
    assert not is_valid_dashboard_uid("a" * 41)


def test_sanitize_filename():
    assert sanitize_filename("Hello World!") == "hello-world"
    assert sanitize_filename("../../../etc/passwd") == "etcpasswd"
    assert sanitize_filename("") == "report"


def test_resolve_timezone_grafana_aliases():
    assert resolve_timezone("browser", "Europe/Prague").key == "Europe/Prague"
    assert resolve_timezone("default", "Europe/Prague").key == "Europe/Prague"
    assert resolve_timezone("", "Europe/Prague").key == "Europe/Prague"
    assert resolve_timezone("utc", "Europe/Prague").key == "UTC"
    assert resolve_timezone("UTC", "Europe/Prague").key == "UTC"
    assert resolve_timezone("America/New_York", "Europe/Prague").key == "America/New_York"
    # Unknown → fallback (don't 400 on Grafana quirks)
    assert resolve_timezone("not-a-real-zone", "Europe/Prague").key == "Europe/Prague"
