from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from reporting import api as api_mod
from reporting.util import resolve_timezone


def _request(query: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/abc",
            "headers": [],
            "query_string": query.encode("utf-8"),
        }
    )


def test_parse_vars_compact_form():
    assert api_mod._parse_vars("a=1,b=two") == {"a": "1", "b": "two"}
    assert api_mod._parse_vars("") == {}


def test_parse_vars_invalid_raises():
    with pytest.raises(HTTPException) as ei:
        api_mod._parse_vars("novalue")
    assert ei.value.status_code == 400


def test_vars_from_request_merges_grafana_native_params():
    req = _request("vars=env=prd&var-data_source=prometheus&var-team=windows&timezone=browser")
    merged = api_mod._vars_from_request(req, "env=prd")
    assert merged == {
        "env": "prd",
        "data_source": "prometheus",
        "team": "windows",
    }


def test_vars_from_request_var_overrides_compact():
    req = _request("vars=env=old&var-env=new")
    # compact parsed first, then var-* overwrites
    merged = api_mod._vars_from_request(req, "env=old")
    assert merged["env"] == "new"


def test_parse_render_overrides_none_when_empty():
    assert api_mod._parse_render_overrides(None, None, None, None, None) is None


def test_parse_render_overrides_valid():
    cfg = api_mod._parse_render_overrides("Paged", 1920, 1358, "Light", 2.0)
    assert cfg is not None
    assert cfg.page_mode == "paged"
    assert cfg.width == 1920
    assert cfg.page_height_px == 1358
    assert cfg.theme == "light"
    assert cfg.scale_factor == 2.0


def test_parse_render_overrides_rejects_bad_width():
    with pytest.raises(HTTPException) as ei:
        api_mod._parse_render_overrides(None, 10, None, None, None)
    assert ei.value.status_code == 400


def test_parse_render_overrides_rejects_bad_mode():
    with pytest.raises(HTTPException) as ei:
        api_mod._parse_render_overrides("poster", None, None, None, None)
    assert ei.value.status_code == 400


def test_timezone_browser_resolves_like_api():
    # Mirrors api.pdf_export resolution against config timezone name
    tz = resolve_timezone("browser", api_mod._timezone_name)
    assert tz.key == api_mod._timezone_name
