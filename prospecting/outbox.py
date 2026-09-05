"""
outbox.py -- the only place in this system with irreversible side effects.

Everything upstream can be re-run for free: a failed scan is retried, a bad
score is recomputed, a wrong verdict is re-evaluated. Sending cannot be undone.
A prospect who receives the same cold email three times because a worker
retried is a prospect lost, and enough of them is a burned sending domain.

So this module is built on one guarantee: AT MOST ONCE PER PROSPECT PER STAGE,
enforced by the database rather than by careful coding.

    idempotency_key = sha256(prospect_id | scan_id | stage)

That key has a UNIQUE constraint. Claiming it is an INSERT. If the insert
raises a unique violation, someone already claimed it -- another worker, or
this same worker before a crash -- and we do not send. The check and the claim
are the same operation, so there is no window between them.

The claim happens BEFORE the GHL call, not after. That ordering is deliberate:
if the process dies mid-send, we would rather leave a claimed row for a message
that never went out (a prospect we never contact) than an unclaimed row for one
that did (a prospect we contact twice). Fail closed, in the direction that
costs a lead rather than a reputation.

    claim = claim_send(conn, prospect_id, scan_id, "email")
    if claim.granted:
        try:
            push_to_ghl(...)
            mark_sent(conn, claim)
        except Exception as exc:
            mark_failed(conn, claim, exc)   # visible, and never auto-retried
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from typing import Any

LOG = logging.getLogger("aeo.outbox")

# Postgres SQLSTATE for unique_violation.
UNIQUE_VIOLATION = "23505"


@dataclass
class SendClaim:
    prospect_id: int
    scan_id: int | None
    stage: str
    key: str
    granted: bool
    reason: str = ""


def idempotency_key(prospect_id: int, scan_id: int | None, stage: str) -> str:
    """Deterministic: the same send computes the same key on every worker."""
    raw = f"{prospect_id}|{scan_id if scan_id is not None else '-'}|{stage}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


CLAIM_SQL = """
INSERT INTO sightline_outreach_events
    (prospect_id, scan_id, event, channel, idempotency_key, payload)
VALUES
    (%(prospect_id)s, %(scan_id)s, 'claimed', %(channel)s, %(key)s, %(payload)s)
ON CONFLICT (idempotency_key) DO NOTHING
RETURNING id;
"""

MARK_SQL = """
INSERT INTO sightline_outreach_events
    (prospect_id, scan_id, event, channel, idempotency_key, payload)
VALUES
    (%(prospect_id)s, %(scan_id)s, %(event)s, %(channel)s, %(key)s, %(payload)s)
ON CONFLICT (idempotency_key) DO NOTHING;
"""

ALREADY_CONTACTED_SQL = """
SELECT 1 FROM sightline_outreach_events
WHERE prospect_id = %(prospect_id)s
  AND event IN ('claimed', 'pushed_to_ghl')
LIMIT 1;
"""


def claim_send(conn, prospect_id: int, scan_id: int | None, channel: str,
               stage: str = "first_touch", payload: dict[str, Any] | None = None,
               once_per_prospect: bool = True) -> SendClaim:
    """Atomically reserve the right to contact this prospect. Commits on success.

    `once_per_prospect` is the outer guard: even a brand new scan of someone
    already contacted does not earn a second cold email. Turn it off only for
    a deliberate follow-up sequence with its own stage name.
    """
    from psycopg.types.json import Jsonb

    key = idempotency_key(prospect_id, scan_id, stage)

    with conn.cursor() as cur:
        if once_per_prospect:
            cur.execute(ALREADY_CONTACTED_SQL, {"prospect_id": prospect_id})
            if cur.fetchone():
                return SendClaim(prospect_id, scan_id, stage, key, False,
                                 "prospect already contacted")

        cur.execute(CLAIM_SQL, {
            "prospect_id": prospect_id,
            "scan_id": scan_id,
            "channel": channel,
            "key": key,
            "payload": Jsonb(payload or {}),
        })
        row = cur.fetchone()

    if row is None:
        conn.rollback()
        return SendClaim(prospect_id, scan_id, stage, key, False,
                         "already claimed (duplicate idempotency key)")

    conn.commit()
    LOG.info("claimed send for prospect %s (%s)", prospect_id, stage)
    return SendClaim(prospect_id, scan_id, stage, key, True)


def _record(conn, claim: SendClaim, event: str, channel: str,
            payload: dict[str, Any] | None = None) -> None:
    from psycopg.types.json import Jsonb

    with conn.cursor() as cur:
        cur.execute(MARK_SQL, {
            "prospect_id": claim.prospect_id,
            "scan_id": claim.scan_id,
            "event": event,
            "channel": channel,
            "key": f"{claim.key}:{event}",
            "payload": Jsonb(payload or {}),
        })
    conn.commit()


def mark_sent(conn, claim: SendClaim, channel: str = "email",
              payload: dict[str, Any] | None = None) -> None:
    _record(conn, claim, "pushed_to_ghl", channel, payload)


def mark_failed(conn, claim: SendClaim, error: Exception | str,
                channel: str = "email") -> None:
    """Record the failure and leave the claim standing.

    Not releasing the claim is the point. A send that failed halfway may or may
    not have reached the prospect, and there is no way to know from here. The
    row stays visible in `stuck_sends()` for a human to decide about; nothing
    retries it automatically.
    """
    _record(conn, claim, "send_failed", channel, {"error": str(error)})
    LOG.error("send failed for prospect %s: %s -- claim left in place, "
              "will not auto-retry", claim.prospect_id, error)


STUCK_SQL = """
SELECT c.prospect_id, c.scan_id, c.created_at, p.name, p.domain
FROM   sightline_outreach_events c
JOIN   sightline_prospects p ON p.id = c.prospect_id
WHERE  c.event = 'claimed'
  AND  c.created_at < now() - %(age)s::interval
  AND  NOT EXISTS (
         SELECT 1 FROM sightline_outreach_events d
         WHERE d.prospect_id = c.prospect_id
           AND d.event IN ('pushed_to_ghl', 'send_failed')
           AND d.created_at >= c.created_at
       )
ORDER BY c.created_at;
"""


def stuck_sends(conn, older_than: str = "1 hour") -> list[dict[str, Any]]:
    """Claims with no outcome: the process died between claiming and sending.

    Worth looking at after any crash. These prospects were never contacted and
    never will be without a decision, which is the safe side to be stuck on.
    """
    with conn.cursor() as cur:
        cur.execute(STUCK_SQL, {"age": older_than})
        cols = [c.name for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def daily_sent_count(conn) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM sightline_outreach_events "
            "WHERE event = 'pushed_to_ghl' "
            "AND created_at >= date_trunc('day', now());"
        )
        return cur.fetchone()[0]


def suppress(conn, prospect_id: int, note: str) -> None:
    """Never contact again. Replies, bounces, opt-outs and complaints land here."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE sightline_prospects SET suppressed = TRUE, "
            "suppressed_note = %(note)s, updated_at = now() WHERE id = %(id)s;",
            {"id": prospect_id, "note": note},
        )
    conn.commit()
    LOG.info("suppressed prospect %s: %s", prospect_id, note)
