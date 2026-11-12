"""API key authentication for protected FastAPI endpoints."""

from __future__ import annotations

import hmac
import logging
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status

from .settings import Settings, settings as default_settings

logger = logging.getLogger(__name__)


def _extract_key(
    authorization: str | None,
    x_api_key: str | None,
) -> str | None:
    if x_api_key:
        return x_api_key.strip()
    if authorization:
        parts = authorization.strip().split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1].strip()
        # Accept raw key in Authorization as a convenience
        if len(parts) == 1:
            return parts[0].strip()
    return None


def verify_api_key(
    authorization: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
    cfg: Settings | None = None,
) -> str:
    """FastAPI dependency that enforces API key auth (fail-closed)."""
    cfg = cfg or default_settings
    configured = cfg.api_keys
    if not configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="API authentication is not configured (API_KEYS is empty)",
        )

    provided = _extract_key(authorization, x_api_key)
    if not provided:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API key. Provide Authorization: Bearer <key> or X-API-Key header.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    for candidate in configured:
        if hmac.compare_digest(provided, candidate):
            return provided

    logger.warning("Rejected request with invalid API key")
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid API key",
        headers={"WWW-Authenticate": "Bearer"},
    )


RequireApiKey = Annotated[str, Depends(verify_api_key)]
