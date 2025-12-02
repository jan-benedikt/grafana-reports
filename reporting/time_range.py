from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Tuple

from dateutil.relativedelta import relativedelta
from zoneinfo import ZoneInfo

RELATIVE_RE = re.compile(r"^now-(?P<value>\d+)(?P<unit>[smhdwMy])$", re.IGNORECASE)

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


def _evaluate_time_expression(expr: str, tz: ZoneInfo, reference: datetime | None = None) -> datetime:
    expr = expr.strip()
    lowered = expr.lower()
    base = reference or datetime.now(tz)

    if lowered == "now":
        return base

    match = RELATIVE_RE.match(lowered)
    if match:
        value = int(match.group("value"))
        unit_key = match.group("unit")
        delta_name, multiplier = UNIT_MAP[unit_key]
        kwargs = {delta_name: value * multiplier}
        return base - relativedelta(**kwargs)

    if lowered.isdigit():
        epoch = int(lowered)
        if epoch < 1_000_000_000_000:  # seconds
            epoch *= 1000
        return datetime.fromtimestamp(epoch / 1000, tz=timezone.utc).astimezone(tz)

    iso_value = expr.replace("Z", "+00:00")
    dt = datetime.fromisoformat(iso_value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(tz)


def _format_grafana_value(expr: str, tz: ZoneInfo, evaluated: datetime, reference_used: bool) -> str:
    lowered = expr.strip().lower()
    if lowered == "now" or lowered.startswith("now-"):
        return lowered

    if lowered.isdigit():
        epoch = int(lowered)
        if epoch < 1_000_000_000_000:
            epoch *= 1000
        return str(epoch)

    epoch_ms = int(evaluated.astimezone(timezone.utc).timestamp() * 1000)
    return str(epoch_ms)


def normalize_time_range(from_expr: str, to_expr: str, tz: ZoneInfo) -> TimeRangeSpec:
    reference = datetime.now(tz)
    start_dt = _evaluate_time_expression(from_expr, tz, reference)
    end_dt = _evaluate_time_expression(to_expr, tz, reference)

    graf_from = _format_grafana_value(from_expr, tz, start_dt, reference_used=True)
    graf_to = _format_grafana_value(to_expr, tz, end_dt, reference_used=True)

    return TimeRangeSpec(grafana_from=graf_from, grafana_to=graf_to, start=start_dt, end=end_dt)


def resolve_relative_range(from_expr: str, to_expr: str, tz: ZoneInfo) -> Tuple[datetime, datetime]:
    spec = normalize_time_range(from_expr, to_expr, tz)
    return spec.start, spec.end
