"""One scan per address.

A second click joins the scan already running, or the one that finished in
the last few minutes, instead of starting another. A scan that has been
running long enough to have lost its worker is not joined.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

RUNNING_LIMIT = timedelta(minutes=8)
FRESH_LIMIT = timedelta(minutes=10)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def slot_decision(row: dict | None, now: datetime | None = None) -> str:
    """'reuse' joins that scan. 'abandon' is a running scan with no worker.
    'start' begins a new one."""
    if not row:
        return "start"
    now = _aware(now or datetime.now(timezone.utc))
    status = row.get("status")
    if status in ("running", "queued"):
        requested = row.get("requested_at")
        if isinstance(requested, datetime) and now - _aware(requested) <= RUNNING_LIMIT:
            return "reuse"
        return "abandon"
    if status == "complete":
        done = row.get("completed_at")
        if isinstance(done, datetime) and now - _aware(done) <= FRESH_LIMIT:
            return "reuse"
    return "start"
