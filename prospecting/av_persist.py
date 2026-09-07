"""
Persist per-query probe evidence to sightline_av_observations.

The probe already produces everything this table wants -- aeo_probe.py fills
ProbeResult.queries with one QueryResult per question, carrying the answer
excerpt, the sources, the competitors named, and the citation rank.
scan_prospect.persist() writes the aggregate to sightline_prospect_scans and
lets that list fall on the floor.

That is why sightline_av_observations has zero rows while every scan carries a
visibility_score. The number is real; the evidence behind it is discarded.

This closes that gap without touching aeo_probe.py or the scoring path.
Nothing here can change a score.

WIRING: in scan_prospect.persist(), right after you get the new scan id:

    from av_persist import write_observations
    write_observations(cur, r.probe, p.registrable_domain(), scan_id)

It reuses the caller's cursor, so observations share the scan's transaction.

MODEL NAME: QueryResult has no `model` field, so set PROBE_MODEL in the
environment to whatever aeo_probe queries.
"""

from __future__ import annotations

import logging
import os

LOG = logging.getLogger("aeo.av_persist")

EXCERPT_LIMIT = 2000


def _model_name(explicit: str | None = None) -> str:
    return explicit or os.getenv("PROBE_MODEL") or "unknown"


def write_observations(cur, probe, domain: str, scan_id: int | None,
                       model: str | None = None) -> int:
    """
    Write one row per probe query. Returns the number written.

    Takes an open cursor rather than opening its own connection, so the rows
    share the scan's transaction.

    Failures are logged, never raised: a broken observation write must not
    take down a scan that otherwise succeeded.
    """
    if probe is None or not getattr(probe, "queries", None):
        return 0

    from psycopg.types.json import Jsonb

    engine = _model_name(model)
    rows = []

    for q in probe.queries:
        if getattr(q, "error", None):
            # Failed queries carry no evidence worth storing, but they do
            # explain a low score -- keep them, flagged, so the sample count
            # in av_summary matches what the probe actually attempted.
            meta = {"kind": getattr(q, "kind", None), "error": q.error,
                    "failed": True}
            rows.append((domain, scan_id, engine, q.query, False, "",
                         Jsonb(meta)))
            continue

        # brand_mentioned is the table's single boolean, but the probe
        # distinguishes cited (domain in sources) from mentioned (name in
        # text). Store the union here and keep the distinction in meta.
        mentioned = bool(getattr(q, "cited", False) or
                         getattr(q, "mentioned", False))

        meta = {
            "kind": getattr(q, "kind", None),
            "cited": bool(getattr(q, "cited", False)),
            "mentioned": bool(getattr(q, "mentioned", False)),
            "citation_rank": getattr(q, "citation_rank", None),
            "sources": list(getattr(q, "sources", []) or [])[:20],
            "competitor_domains": list(getattr(q, "competitor_domains", []) or [])[:20],
            "directory_domains": list(getattr(q, "directory_domains", []) or [])[:20],
            "credit": getattr(q, "credit", None),
        }

        excerpt = (getattr(q, "answer_excerpt", "") or "")[:EXCERPT_LIMIT]
        rows.append((domain, scan_id, engine, q.query, mentioned, excerpt,
                     Jsonb(meta)))

    if not rows:
        return 0

    try:
        # Wrap the insert in a savepoint. On the caller's shared connection a
        # failed statement aborts the whole transaction, which would take down
        # every later scan in the batch and the final commit. transaction()
        # issues a SAVEPOINT when already inside a transaction and rolls back
        # to it on error, so a bad observation write is undone in isolation and
        # the surrounding scan transaction stays usable.
        with cur.connection.transaction():
            cur.executemany(
                """INSERT INTO sightline_av_observations
                     (domain, scan_id, model, query, brand_mentioned,
                      response_excerpt, meta)
                   VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                rows,
            )
    except Exception as exc:  # noqa: BLE001
        LOG.warning("av observation write failed for %s: %s", domain, exc)
        return 0

    LOG.info("wrote %d av observations for %s (scan %s)",
             len(rows), domain, scan_id)
    return len(rows)
