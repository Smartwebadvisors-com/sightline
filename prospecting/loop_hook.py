"""
loop_hook.py -- how the scanner plugs into lead_engine_loop.py.

Keeps the existing sense-reason-act shape:

    SENSE   pull prospects (DataForSEO business listings) -> Redis queue
    REASON  this scanner -> verdict + scan row in Postgres        <- new
    ACT     render report, draft email, push to GHL               <- gated

The gate between REASON and ACT is deliberately dumb: a verdict of QUALIFIED
plus a priority above a floor plus a daily cap. Nothing here asks a model
whether to send.

Run it from node-cron / PM2 the way the rest of the loop runs:

    python3 loop_hook.py --limit 25          # scan the queue
    python3 loop_hook.py --act --limit 15    # hand QUALIFIED rows to the actor
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from typing import Any, Iterable

from aeo_gate import GateConfig
from aeo_probe import Prospect
from scan_prospect import ScanResult, load_suppressed, persist, scan_batch

LOG = logging.getLogger("aeo.loop")

# Act-stage gate. Deliberately conservative until the copy is proven.
MIN_PRIORITY_TO_ACT = int(os.getenv("AEO_MIN_PRIORITY", "55"))
DAILY_SEND_CAP = int(os.getenv("AEO_DAILY_CAP", "30"))
REQUIRE_HUMAN_APPROVAL = os.getenv("AEO_REQUIRE_APPROVAL", "1") == "1"


# --------------------------------------------------------------------------
# SENSE -> REASON
# --------------------------------------------------------------------------

def drain_sense_queue(limit: int) -> list[Prospect]:
    """Claim up to `limit` prospects from the queue."""
    import queue_pg

    queue_pg.release_claims()          # recover anything a dead run stranded
    allowed = set(Prospect.__dataclass_fields__)
    out: list[Prospect] = []
    for row in queue_pg.drain(limit, stage=queue_pg.STAGE_PROSPECT):
        try:
            out.append(Prospect(**{k: v for k, v in row.items() if k in allowed}))
        except Exception as exc:
            LOG.warning("bad prospect payload skipped: %s", exc)
    return out


def reason(limit: int = 25, config: GateConfig | None = None) -> list[ScanResult]:
    prospects = drain_sense_queue(limit)
    if not prospects:
        LOG.info("sense queue empty")
        return []
    LOG.info("scanning %d prospect(s)", len(prospects))
    results = scan_batch(prospects, config, suppressed_domains=load_suppressed())
    scan_ids = persist(results)
    for result, scan_id in zip(results, scan_ids):
        result.site.errors.append(f"scan_id={scan_id}")  # cheap back-reference
    LOG.info("persisted %d scan(s); %d qualified", len(scan_ids),
             sum(1 for r in results if r.verdict.status == "QUALIFIED"))
    return results


# --------------------------------------------------------------------------
# REASON -> ACT (gated)
# --------------------------------------------------------------------------

SELECT_ACTIONABLE = """
SELECT prospect_id, scan_id, name, domain, category, city, state,
       review_count, rating, visibility_score, aeo_score, gate_rule,
       gate_priority, gate_hook
FROM   sightline_outreach_queue
WHERE  gate_status = 'QUALIFIED'
  AND  gate_priority >= %(min_priority)s
  AND  report_url IS NULL
  AND  prospect_id NOT IN (
           SELECT prospect_id FROM sightline_outreach_events
           WHERE event IN ('pushed_to_ghl', 'suppressed')
       )
ORDER  BY gate_priority DESC, scanned_at DESC
LIMIT  %(limit)s;
"""

COUNT_SENT_TODAY = """
SELECT count(*) FROM sightline_outreach_events
WHERE event = 'pushed_to_ghl' AND created_at >= date_trunc('day', now());
"""


def actionable(limit: int = 15, dsn: str | None = None) -> list[dict[str, Any]]:
    """Rows that cleared the gate and have not been contacted, capped for the day."""
    import psycopg

    dsn = dsn or os.getenv("DATABASE_URL")
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(COUNT_SENT_TODAY)
        sent_today = cur.fetchone()[0]
        remaining = max(0, DAILY_SEND_CAP - sent_today)
        if remaining == 0:
            LOG.info("daily cap reached (%d) -- nothing released", DAILY_SEND_CAP)
            return []

        cur.execute(SELECT_ACTIONABLE, {
            "min_priority": MIN_PRIORITY_TO_ACT,
            "limit": min(limit, remaining),
        })
        cols = [c.name for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def release_to_act(limit: int = 15) -> int:
    """Push cleared rows onto the act queue.

    With REQUIRE_HUMAN_APPROVAL on (the default), rows land in a review list
    instead and only move to the act queue once you approve them -- same gated
    pattern as the rest of the loop.
    """
    rows = actionable(limit)
    if not rows:
        return 0

    import queue_pg

    # With approval required (the default) these land in the review stage for
    # a human to look at. Nothing here sends anything either way.
    stage = queue_pg.STAGE_REVIEW
    payloads = [{k: (str(v) if hasattr(v, "isoformat") else v)
                 for k, v in row.items()} for row in rows]
    n = queue_pg.enqueue(payloads, stage=stage)
    LOG.info("released %d row(s) to the %s queue%s", n, stage,
             "" if REQUIRE_HUMAN_APPROVAL else " (approval disabled)")
    return n


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="AEO scanner loop hook")
    ap.add_argument("--limit", type=int, default=25)
    ap.add_argument("--act", action="store_true",
                    help="release qualified rows instead of scanning")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s")

    if args.act:
        print(f"released {release_to_act(args.limit)} row(s)")
    else:
        results = reason(args.limit)
        for r in results:
            print(r.summary_line())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
