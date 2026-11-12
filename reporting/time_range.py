from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from dateutil.relativedelta import relativedelta
from zoneinfo import ZoneInfo

# now, now-30d, now-1M (unit: s,m,h,d,w,M,y)
RELATIVE_RE = re.compile(r"^now-(?P<value>\d+)(?P<unit>[smhdwMy])$", re.IGNORECASE)
# optional /round suffix: now/M, now-1M/M (round: M=month, d=day, w=week, y=year, h=hour, m=minute)
ROUND_SUFFIX_RE = re.compile(r"^(.+?)/([MdwymhDs])$", re.IGNORECASE)
# optional trailing minus: now/M-1d = start of current month minus 1 day = last day of previous month
MINUS_SUFFIX_RE = re.compile(r"-(?P<value>\d+)(?P<unit>[smhdwMy])$", re.IGNORECASE)

UNIT_MAP = {
    "s": ("seconds", 1),
    "m": ("minutes", 1),
    "h": ("hours", 1),
    "d": ("days", 1),
    "w": ("weeks", 1),
    "M": ("months", 1),
    "y": ("years", 1),
}


@dataclass(slots=True)
class TimeRangeSpec:
    grafana_from: str
    grafana_to: str
    start: datetime
    end: datetime


def _round_to_period(dt: datetime, round_unit: str) -> datetime:
    """Round datetime down to the start of the given period (in the datetime's tz)."""
    # Grafana: M = month, m = minute, d = day, w = week, y = year, h = hour, s = second
    if round_unit == "M":
        return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if round_unit == "m":
        return dt.replace(second=0, microsecond=0)
    if round_unit.lower() == "d":
        return dt.replace(hour=0, minute=0, second=0, microsecond=0)
    if round_unit.lower() == "w":
        # Monday as start of week
        weekday = dt.weekday()  # 0=Monday, 6=Sunday
        start = dt - relativedelta(days=weekday)
        return start.replace(hour=0, minute=0, second=0, microsecond=0)
    if round_unit.lower() == "y":
        return dt.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    if round_unit.lower() == "h":
        return dt.replace(minute=0, second=0, microsecond=0)
    if round_unit.lower() == "s":
        return dt.replace(microsecond=0)
    return dt


def _evaluate_base_expression(expr: str, tz: ZoneInfo, reference: datetime | None = None) -> datetime:
    """Evaluate a single time expression (no /round suffix)."""
    expr = expr.strip()
    lowered = expr.lower()
    base = reference or datetime.now(tz)

    if lowered == "now":
        return base

    match = RELATIVE_RE.match(lowered)
    if match:
        value = int(match.group("value"))
        # Preserve M (month) vs m (minute) from original expression
        unit_key = expr[match.start("unit") : match.end("unit")]
        delta_name, multiplier = UNIT_MAP[unit_key]
        kwargs = {delta_name: value * multiplier}
        return base - relativedelta(**kwargs)

    if lowered.isdigit():
        epoch = int(lowered)
        if epoch < 1_000_000_000_000:  # seconds
            epoch *= 1000
        return datetime.fromtimestamp(epoch / 1000, tz=UTC).astimezone(tz)

    iso_value = expr.replace("Z", "+00:00")
    dt = datetime.fromisoformat(iso_value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(tz)


def _evaluate_time_expression(expr: str, tz: ZoneInfo, reference: datetime | None = None) -> datetime:
    expr = expr.strip()
    # Optional trailing minus: now/M-1d = last day of previous month
    minus_match = MINUS_SUFFIX_RE.search(expr)
    if minus_match and minus_match.end() == len(expr) and minus_match.start() > 0:
        base_part = expr[: minus_match.start()].strip()
        value = int(minus_match.group("value"))
        unit_key = expr[minus_match.start("unit") : minus_match.end("unit")]
        dt = _evaluate_time_expression(base_part, tz, reference)
        delta_name, multiplier = UNIT_MAP[unit_key]
        return dt - relativedelta(**{delta_name: value * multiplier})

    round_match = ROUND_SUFFIX_RE.match(expr)
    if round_match:
        base_part = round_match.group(1).strip()
        round_part = round_match.group(2)
        dt = _evaluate_base_expression(base_part, tz, reference)
        return _round_to_period(dt, round_part)
    return _evaluate_base_expression(expr, tz, reference)


def _is_relative_expression(expr: str) -> bool:
    """True if expr is a relative time (now, now-30d, now/M, now-1M/M, etc.) to pass through to Grafana."""
    s = expr.strip()
    if not s:
        return False
    lowered = s.lower()
    return lowered == "now" or lowered.startswith("now-") or lowered.startswith("now/")


def _format_grafana_value(expr: str, tz: ZoneInfo, evaluated: datetime) -> str:
    s = expr.strip()
    lowered = s.lower()
    if _is_relative_expression(expr):
        # Grafana often misbehaves with now/M, now-1M/M (and other /round) in URL—use explicit epoch ms
        if "/" in s:
            return str(int(evaluated.astimezone(UTC).timestamp() * 1000))
        return s  # pass through simple now, now-30d, now-1M

    if lowered.isdigit():
        epoch = int(lowered)
        if epoch < 1_000_000_000_000:
            epoch *= 1000
        return str(epoch)

    epoch_ms = int(evaluated.astimezone(UTC).timestamp() * 1000)
    return str(epoch_ms)


def normalize_time_range(from_expr: str, to_expr: str, tz: ZoneInfo) -> TimeRangeSpec:
    reference = datetime.now(tz)
    start_dt = _evaluate_time_expression(from_expr, tz, reference)
    end_dt = _evaluate_time_expression(to_expr, tz, reference)

    graf_from = _format_grafana_value(from_expr, tz, start_dt)
    graf_to = _format_grafana_value(to_expr, tz, end_dt)

    return TimeRangeSpec(grafana_from=graf_from, grafana_to=graf_to, start=start_dt, end=end_dt)
