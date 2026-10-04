"""Dependency health checks.

Each check returns a plain dictionary so the same result can be rendered as
JSON by the health endpoint and printed by management commands.

Checks never raise. A failing dependency produces ``status = "unavailable"``
plus a short reason; the underlying exception is logged rather than returned to
the caller, so internal host names and credentials cannot leak to clients.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from django.core.cache import cache
from django.db import connection

logger = logging.getLogger(__name__)

HEALTH_PROBE_KEY = "core:health-probe"


def check_database() -> dict[str, Any]:
    """Verify that PostgreSQL accepts a connection and answers a trivial query."""
    started = time.monotonic()
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            result = cursor.fetchone()
        if result is None or result[0] != 1:
            raise RuntimeError(f"Unexpected query result: {result!r}")
    except Exception as exc:
        logger.error("Database health check failed", exc_info=exc)
        return {
            "status": "unavailable",
            "reason": "The application database is not reachable.",
        }

    return {
        "status": "ok",
        "engine": connection.vendor,
        "latency_ms": round((time.monotonic() - started) * 1000, 2),
    }


def check_redis() -> dict[str, Any]:
    """Verify that Redis is reachable through the configured cache backend."""
    started = time.monotonic()
    try:
        cache.set(HEALTH_PROBE_KEY, "1", timeout=30)
        if cache.get(HEALTH_PROBE_KEY) != "1":
            raise RuntimeError("Cache round trip did not return the written value.")
    except Exception as exc:
        logger.error("Redis health check failed", exc_info=exc)
        return {
            "status": "unavailable",
            "reason": "The cache/Redis service is not reachable.",
        }

    return {
        "status": "ok",
        "latency_ms": round((time.monotonic() - started) * 1000, 2),
    }


def run_checks() -> dict[str, dict[str, Any]]:
    """Run every dependency check.

    Database and Redis are both required: the application cannot serve a live
    competition without them, so both influence the reported status.
    """
    return {
        "database": check_database(),
        "redis": check_redis(),
    }