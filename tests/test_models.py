from __future__ import annotations

import pytest

from reporting.models import ConfigError, ReportConfig, load_reports_config


def _write(tmp_path, body: str):
    path = tmp_path / "config.yml"
    path.write_text(body, encoding="utf-8")
    return path


def test_load_valid_config_with_render(tmp_path):
    path = _write(
        tmp_path,
        """
timezone: Europe/Prague
reports:
  - name: Monthly
    dashboard_uid: abc123
    from: now-1M/M
    to: now/M-1d
    cron: "0 7 1 * *"
    vars:
      env: prd
    render:
      width: 1600
      page_mode: paged
      theme: light
      scale_factor: 2
    email:
      to: ["ops@example.com"]
      filename_prefix: monthly-prd
""",
    )
    tz, reports = load_reports_config(path, "UTC")
    assert tz == "Europe/Prague"
    assert len(reports) == 1
    r = reports[0]
    assert isinstance(r, ReportConfig)
    assert r.dashboard_uid == "abc123"
    assert r.vars["env"] == "prd"
    assert r.render.width == 1600
    assert r.render.page_mode == "paged"
    assert r.render.theme == "light"
    assert r.render.scale_factor == 2.0
    assert r.safe_filename_prefix == "monthly-prd"


def test_safe_filename_sanitizes_prefix(tmp_path):
    path = _write(
        tmp_path,
        """
timezone: UTC
reports:
  - name: "Weird / Name!"
    dashboard_uid: uid1
    email:
      filename_prefix: "Foo Bar!!"
""",
    )
    _, reports = load_reports_config(path, "UTC")
    assert reports[0].safe_filename_prefix == "foo-bar"


def test_missing_name_raises(tmp_path):
    path = _write(
        tmp_path,
        """
timezone: UTC
reports:
  - dashboard_uid: abc123
""",
    )
    with pytest.raises(ConfigError, match="name"):
        load_reports_config(path, "UTC")


def test_invalid_uid_raises(tmp_path):
    path = _write(
        tmp_path,
        """
timezone: UTC
reports:
  - name: Bad
    dashboard_uid: "bad uid!"
""",
    )
    with pytest.raises(ConfigError, match="dashboard_uid"):
        load_reports_config(path, "UTC")


def test_invalid_cron_raises(tmp_path):
    path = _write(
        tmp_path,
        """
timezone: UTC
reports:
  - name: BadCron
    dashboard_uid: abc123
    cron: "not a cron"
""",
    )
    with pytest.raises(ConfigError, match="cron"):
        load_reports_config(path, "UTC")


def test_invalid_timezone_raises(tmp_path):
    path = _write(
        tmp_path,
        """
timezone: Not/AZone
reports: []
""",
    )
    with pytest.raises(ConfigError, match="timezone"):
        load_reports_config(path, "UTC")


def test_missing_file_raises(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_reports_config(tmp_path / "nope.yml", "UTC")


def test_default_timezone_from_argument(tmp_path):
    path = _write(
        tmp_path,
        """
reports:
  - name: One
    dashboard_uid: abc123
""",
    )
    tz, reports = load_reports_config(path, "Europe/Berlin")
    assert tz == "Europe/Berlin"
    assert len(reports) == 1
