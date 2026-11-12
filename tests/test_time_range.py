from __future__ import annotations

from calendar import monthrange
from datetime import datetime
from zoneinfo import ZoneInfo

from dateutil.relativedelta import relativedelta

from reporting.time_range import normalize_time_range

TZ = ZoneInfo("Europe/Prague")


def test_simple_relative_passthrough():
    spec = normalize_time_range("now-30d", "now", TZ)
    assert spec.grafana_from == "now-30d"
    assert spec.grafana_to == "now"
    assert spec.start < spec.end


def test_previous_month_range_now_1M_M_to_now_M_1d():
    """Full previous month: from now-1M/M to now/M-1d."""
    now = datetime.now(TZ)
    expected_start = (now.replace(day=1) - relativedelta(months=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    expected_end = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0) - relativedelta(days=1)

    spec = normalize_time_range("now-1M/M", "now/M-1d", TZ)
    assert spec.grafana_from.isdigit()
    assert spec.grafana_to.isdigit()
    assert spec.start == expected_start
    assert spec.end == expected_end
    assert spec.start < spec.end
    # Previous month last day is valid for that month
    _, last = monthrange(expected_end.year, expected_end.month)
    assert expected_end.day == last


def test_month_round_start_of_current_month():
    now = datetime.now(TZ)
    expected = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    spec = normalize_time_range("now/M", "now", TZ)
    assert spec.start == expected
    assert spec.grafana_from.isdigit()  # /round → epoch for Grafana
    assert spec.grafana_to == "now"


def test_week_and_day_round():
    week = normalize_time_range("now/w", "now", TZ)
    assert week.start.weekday() == 0  # Monday
    assert week.start.hour == 0
    assert week.start.minute == 0

    now = datetime.now(TZ)
    day = normalize_time_range("now/d", "now", TZ)
    assert day.start == now.replace(hour=0, minute=0, second=0, microsecond=0)


def test_year_round_and_trailing_minus():
    now = datetime.now(TZ)
    year = normalize_time_range("now/y", "now", TZ)
    assert year.start == now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)

    end_prev = normalize_time_range("now-1d", "now/M-1d", TZ)
    expected_end = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0) - relativedelta(days=1)
    assert end_prev.end == expected_end


def test_iso_and_epoch():
    spec = normalize_time_range("2024-01-01T00:00:00+01:00", "1704067200000", TZ)
    assert spec.grafana_from.isdigit()
    assert int(spec.grafana_to) == 1704067200000


def test_epoch_seconds_normalized_to_ms():
    spec = normalize_time_range("1704067200", "now", TZ)
    assert int(spec.grafana_from) == 1704067200000


def test_relative_month_unit_preserved_in_passthrough():
    # now-1M without /round should pass through (Grafana understands it)
    spec = normalize_time_range("now-1M", "now", TZ)
    assert spec.grafana_from == "now-1M"


def test_hour_and_minute_round():
    now = datetime.now(TZ)
    hour = normalize_time_range("now/h", "now", TZ)
    assert hour.start == now.replace(minute=0, second=0, microsecond=0)

    minute = normalize_time_range("now/m", "now", TZ)
    assert minute.start == now.replace(second=0, microsecond=0)
