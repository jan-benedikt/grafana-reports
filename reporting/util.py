"""Shared helpers used across the reporting package."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path
from zoneinfo import ZoneInfo

_UID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_UNSAFE_FILENAME_RE = re.compile(r"[^\w\s-]", re.UNICODE)
_MULTI_SEP_RE = re.compile(r"[-\s]+")

logger = logging.getLogger(__name__)

# Grafana special timezone tokens (not IANA names). Server-side we map them
# to the configured default — there is no real browser timezone in headless render.
_GRAFANA_TZ_ALIASES = {
    "": None,  # use fallback
    "browser": None,
    "default": None,
    "utc": "UTC",
    "gmt": "UTC",
}


def sanitize_filename(name: str) -> str:
    """Convert an arbitrary string into a safe lowercase filename prefix."""
    safe = _UNSAFE_FILENAME_RE.sub("", name or "")
    safe = _MULTI_SEP_RE.sub("-", safe)
    return safe.lower().strip("-") or "report"


def is_valid_dashboard_uid(uid: str) -> bool:
    return bool(uid and _UID_RE.match(uid))


def resolve_timezone(name: str | None, fallback: str) -> ZoneInfo:
    """Resolve a timezone string from Grafana/API into a ZoneInfo.

    Grafana 13.2+ often sends ``timezone=browser`` (or ``default`` / ``utc``).
    Those are not IANA names; map them to *fallback* (or UTC) so exports work.
    Unknown values also fall back instead of failing the request.
    """
    raw = (name or "").strip()
    if raw.lower() in _GRAFANA_TZ_ALIASES:
        mapped = _GRAFANA_TZ_ALIASES[raw.lower()]
        resolved = mapped if mapped is not None else fallback
        if mapped is None and raw:
            logger.info("Timezone %r mapped to configured default %r", raw, fallback)
        return ZoneInfo(resolved)

    try:
        return ZoneInfo(raw)
    except Exception:
        logger.warning("Unknown timezone %r, falling back to %r", raw, fallback)
        return ZoneInfo(fallback)


def sweep_old_pdfs(outdir: Path, max_age_s: float) -> int:
    """Delete generated artifacts (*.pdf, *.png) older than ``max_age_s``.

    Orphaned files accumulate when a response is never delivered (client
    disconnect) or a run fails after rendering; without a sweep they can
    exhaust the pod's emptyDir and trigger eviction.
    """
    if max_age_s <= 0:
        return 0
    cutoff = time.time() - max_age_s
    removed = 0
    for pattern in ("*.pdf", "*.png"):
        for path in outdir.glob(pattern):
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
                    removed += 1
            except OSError:
                logger.debug("Could not sweep %s", path, exc_info=True)
    if removed:
        logger.info("Swept %d stale artifact(s) from %s", removed, outdir)
    return removed


async def pdf_sweeper_loop(outdir: Path, max_age_s: float, interval_s: float = 900.0) -> None:
    """Periodically sweep stale PDFs until cancelled."""
    if max_age_s <= 0:
        return
    while True:
        try:
            sweep_old_pdfs(outdir, max_age_s)
        except Exception:
            logger.warning("PDF sweep failed", exc_info=True)
        await asyncio.sleep(interval_s)