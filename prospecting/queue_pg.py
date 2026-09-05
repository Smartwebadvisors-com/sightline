"""
queue_pg.py -- the work queue, in Postgres. No Redis.

This pipeline moves twenty-five prospects on a weekday morning. That is not a
queueing problem, and Redis was in the design only because the existing lead
engine already ran one on a different machine. Carrying it here would mean a
second service to install, monitor and keep alive on a box that otherwise has
no use for it -- a moving part bought with nothing.

Postgres is already a hard dependency: Sightline writes to it, the scans and
the send ledger live in it. So the queue lives there too, and the whole system
has exactly one thing that has to be running.

The one mechanism worth knowing: `SELECT ... FOR UPDATE SKIP LOCKED`. Two
workers draining at once each get different rows instead of fighting over the
same ones or blocking. It is the standard Postgres queue pattern and it is why
this does not need a queue server.

    enqueue(prospects)            # dedupes on domain, ignores re-adds
    drain(limit)                  # claims and returns up to `limit`
    depth()                       # how many are waiting
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict
from typing import Any, Iterable

LOG = logging.getLogger("aeo.queue")

STAGE_PROSPECT = "prospect"   # sourced, waiting to be scanned
STAGE_REVIEW = "review"       # qualified, waiting for a human to release


def _dsn(dsn: str | None = None) -> str:
    dsn = dsn or os.getenv("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL is not set")
    return dsn


ENQUEUE_SQL = """
INSERT INTO sightline_queue (stage, dedupe_key, payload)
VALUES (%(stage)s, %(key)s, %(payload)s)
ON CONFLICT (stage, dedupe_key) DO NOTHING;
"""

# SKIP LOCKED is what makes this safe for concurrent workers: a row already
# claimed by another transaction is passed over rather than waited on.
DRAIN_SQL = """
WITH claimed AS (
    SELECT id
    FROM   sightline_queue
    WHERE  stage = %(stage)s AND claimed_at IS NULL
    ORDER  BY created_at
    LIMIT  %(limit)s
    FOR UPDATE SKIP LOCKED
)
UPDATE sightline_queue q
SET    claimed_at = now()
FROM   claimed
WHERE  q.id = claimed.id
RETURNING q.id, q.payload;
"""

DEPTH_SQL = """
SELECT count(*) FROM sightline_queue
WHERE stage = %(stage)s AND claimed_at IS NULL;
"""


def enqueue(items: Iterable[Any], stage: str = STAGE_PROSPECT,
            dsn: str | None = None) -> int:
    """Add items to the queue. Re-adding something already queued is a no-op.

    Dedupe is on the domain for prospects, so a weekly sourcing run that
    returns the same businesses does not pile up duplicates.
    """
    import psycopg
    from psycopg.types.json import Jsonb

    added = 0
    with psycopg.connect(_dsn(dsn)) as conn, conn.cursor() as cur:
        for item in items:
            payload = item if isinstance(item, dict) else asdict(item)
            key = (payload.get("domain") or payload.get("prospect_id")
                   or json.dumps(payload, sort_keys=True))
            cur.execute(ENQUEUE_SQL, {
                "stage": stage, "key": str(key), "payload": Jsonb(payload),
            })
            added += cur.rowcount or 0
        conn.commit()
    LOG.info("queued %d new item(s) to %s", added, stage)
    return added


def drain(limit: int = 25, stage: str = STAGE_PROSPECT,
          dsn: str | None = None) -> list[dict[str, Any]]:
    """Claim up to `limit` items. Claimed rows are not handed out again."""
    import psycopg

    with psycopg.connect(_dsn(dsn)) as conn, conn.cursor() as cur:
        cur.execute(DRAIN_SQL, {"stage": stage, "limit": limit})
        rows = cur.fetchall()
        conn.commit()
    return [row[1] for row in rows]


def depth(stage: str = STAGE_PROSPECT, dsn: str | None = None) -> int:
    import psycopg

    with psycopg.connect(_dsn(dsn)) as conn, conn.cursor() as cur:
        cur.execute(DEPTH_SQL, {"stage": stage})
        return cur.fetchone()[0]


def release_claims(stage: str = STAGE_PROSPECT, older_than: str = "6 hours",
                   dsn: str | None = None) -> int:
    """Return items claimed by a run that died before finishing.

    Without this, a crash mid-run would strand those prospects as claimed
    forever. Six hours is well beyond any healthy run.
    """
    import psycopg

    with psycopg.connect(_dsn(dsn)) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE sightline_queue SET claimed_at = NULL "
            "WHERE stage = %(stage)s AND claimed_at IS NOT NULL "
            "AND claimed_at < now() - %(age)s::interval;",
            {"stage": stage, "age": older_than},
        )
        n = cur.rowcount or 0
        conn.commit()
    if n:
        LOG.info("released %d stranded claim(s) in %s", n, stage)
    return n


def purge_claimed(stage: str = STAGE_PROSPECT, older_than: str = "30 days",
                  dsn: str | None = None) -> int:
    """Housekeeping: drop long-processed rows so the table stays small."""
    import psycopg

    with psycopg.connect(_dsn(dsn)) as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM sightline_queue "
            "WHERE stage = %(stage)s AND claimed_at IS NOT NULL "
            "AND claimed_at < now() - %(age)s::interval;",
            {"stage": stage, "age": older_than},
        )
        n = cur.rowcount or 0
        conn.commit()
    return n
