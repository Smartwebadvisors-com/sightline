"""
status.py -- what the pipeline is doing. One command, no SQL to remember.

    python3 status.py              # the overview you want each morning
    python3 status.py --queue      # businesses waiting to be scanned
    python3 status.py --review     # cleared the gate, ranked by priority
    python3 status.py --skipped    # what was rejected, and why
    python3 status.py --errors     # recent scan failures

Read-only. Nothing here changes state.
"""

from __future__ import annotations

import argparse
import os
import sys
import textwrap

BAR = "─" * 74


def _conn():
    import psycopg
    from psycopg.rows import dict_row

    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL is not set -- run: set -a && . ./.env && set +a")
        sys.exit(1)
    return psycopg.connect(dsn, row_factory=dict_row, connect_timeout=10)


def _rows(cur, sql, params=None):
    cur.execute(sql, params or {})
    return cur.fetchall()


def _fmt(value, width, align="<"):
    text = "" if value is None else str(value)
    if len(text) > width:
        text = text[: width - 1] + "…"
    return f"{text:{align}{width}}"


# ---------------------------------------------------------------------------

def overview(cur) -> None:
    print(f"\n{BAR}\n  PIPELINE\n{BAR}")

    q = _rows(cur, """
        SELECT stage,
               count(*) FILTER (WHERE claimed_at IS NULL)     AS waiting,
               count(*) FILTER (WHERE claimed_at IS NOT NULL) AS claimed
        FROM sightline_queue GROUP BY stage ORDER BY stage;""")
    if q:
        for r in q:
            print(f"  queue [{r['stage']:<8}]  {r['waiting']:>5} waiting"
                  f"   {r['claimed']:>5} claimed")
    else:
        print("  queue          empty -- run: python3 sourcing.py --enqueue")

    scanned = _rows(cur, """
        SELECT count(*) AS scans, count(DISTINCT prospect_id) AS prospects,
               max(scanned_at) AS last
        FROM sightline_prospect_scans;""")[0]
    print(f"\n  scanned        {scanned['prospects'] or 0:>5} prospect(s), "
          f"{scanned['scans'] or 0} scan(s)")
    if scanned["last"]:
        print(f"  last scan      {scanned['last']:%Y-%m-%d %H:%M}")

    verdicts = _rows(cur, """
        SELECT gate_status, count(*) AS n FROM sightline_outreach_queue
        GROUP BY gate_status ORDER BY n DESC;""")
    if verdicts:
        line = "   ".join(f"{r['gate_status'].lower()}: {r['n']}" for r in verdicts)
        print(f"  verdicts       {line}")

    sends = _rows(cur, """
        SELECT
          count(*) FILTER (WHERE event='pushed_to_ghl') AS sent_total,
          count(*) FILTER (WHERE event='pushed_to_ghl'
                           AND created_at >= date_trunc('day', now())) AS sent_today,
          count(*) FILTER (WHERE event='send_failed')   AS failed
        FROM sightline_outreach_events;""")[0]
    suppressed = _rows(cur, "SELECT count(*) AS n FROM sightline_prospects "
                            "WHERE suppressed;")[0]["n"]
    print(f"\n  sent           {sends['sent_today']} today, "
          f"{sends['sent_total']} all time"
          + (f", {sends['failed']} failed" if sends["failed"] else ""))
    print(f"  suppressed     {suppressed} domain(s) never to contact")

    try:
        stuck = _rows(cur, "SELECT count(*) AS n FROM sightline_stuck_sends;")[0]["n"]
        if stuck:
            print(f"\n  !! {stuck} send(s) claimed but never resolved -- "
                  "see: python3 status.py --errors")
    except Exception:
        pass


def review_queue(cur, limit: int = 25) -> None:
    rows = _rows(cur, """
        SELECT gate_status, gate_priority, visibility_score, aeo_score,
               name, city, gate_rule, gate_hook, domain
        FROM   sightline_outreach_queue
        WHERE  gate_status = 'QUALIFIED'
        ORDER  BY gate_priority DESC, scanned_at DESC
        LIMIT  %(limit)s;""", {"limit": limit})

    print(f"\n{BAR}\n  QUALIFIED -- worth contacting, highest priority first\n{BAR}")
    if not rows:
        print("  nothing qualified yet.")
        print("  (a --dry-run decides but saves nothing; drop the flag to keep results)")
        return

    for r in rows:
        vis = "--" if r["visibility_score"] is None else r["visibility_score"]
        print(f"\n  p{r['gate_priority']:<3} vis={vis:<4} aeo={r['aeo_score'] or '--':<4}"
              f" {r['name']} ({r['city']})")
        print(f"       {r['domain']}   [{r['gate_rule']}]")
        if r["gate_hook"]:
            for line in textwrap.wrap(r["gate_hook"], width=66):
                print(f"       {line}")


def waiting_queue(cur, limit: int = 40) -> None:
    rows = _rows(cur, """
        SELECT payload->>'name' AS name, payload->>'domain' AS domain,
               payload->>'city' AS city, payload->>'category' AS category,
               (payload->>'review_count')::int AS reviews,
               payload->>'rating' AS rating
        FROM   sightline_queue
        WHERE  stage='prospect' AND claimed_at IS NULL
        ORDER  BY (payload->>'review_count')::int DESC NULLS LAST
        LIMIT  %(limit)s;""", {"limit": limit})

    total = _rows(cur, "SELECT count(*) AS n FROM sightline_queue "
                       "WHERE stage='prospect' AND claimed_at IS NULL;")[0]["n"]
    print(f"\n{BAR}\n  WAITING TO BE SCANNED -- {total} total\n{BAR}")
    for r in rows:
        print(f"  {_fmt(r['reviews'], 5, '>')} rev  {_fmt(r['rating'], 4)}"
              f"  {_fmt(r['domain'], 32)} {_fmt(r['name'], 30)} {r['city'] or ''}")
    if total > len(rows):
        print(f"  ... and {total - len(rows)} more")


def skipped(cur, limit: int = 30) -> None:
    counts = _rows(cur, """
        SELECT gate_rule, count(*) AS n FROM sightline_outreach_queue
        WHERE gate_status <> 'QUALIFIED'
        GROUP BY gate_rule ORDER BY n DESC;""")
    print(f"\n{BAR}\n  NOT QUALIFIED -- by reason\n{BAR}")
    if not counts:
        print("  nothing scanned yet.")
        return
    for r in counts:
        print(f"  {r['n']:>4}  {r['gate_rule']}")

    rows = _rows(cur, """
        SELECT name, city, gate_status, gate_rule, visibility_score, aeo_score
        FROM   sightline_outreach_queue
        WHERE  gate_status <> 'QUALIFIED'
        ORDER  BY scanned_at DESC LIMIT %(limit)s;""", {"limit": limit})
    print()
    for r in rows:
        vis = "--" if r["visibility_score"] is None else r["visibility_score"]
        print(f"  {_fmt(r['gate_status'], 9)} vis={_fmt(vis, 4)} "
              f"aeo={_fmt(r['aeo_score'], 4)} {_fmt(r['name'], 32)} {r['gate_rule']}")


def errors(cur, limit: int = 20) -> None:
    print(f"\n{BAR}\n  RECENT PROBLEMS\n{BAR}")

    rows = _rows(cur, """
        SELECT p.name, p.domain, s.scanned_at, s.scan_errors
        FROM   sightline_prospect_scans s
        JOIN   sightline_prospects p ON p.id = s.prospect_id
        WHERE  s.scan_errors IS NOT NULL
          AND  jsonb_array_length(s.scan_errors) > 0
        ORDER  BY s.scanned_at DESC LIMIT %(limit)s;""", {"limit": limit})
    if rows:
        for r in rows:
            print(f"\n  {r['scanned_at']:%m-%d %H:%M}  {r['name']} ({r['domain']})")
            for err in (r["scan_errors"] or [])[:3]:
                print(f"       {str(err)[:100]}")
    else:
        print("  no scan errors on record.")

    try:
        stuck = _rows(cur, "SELECT name, domain, created_at "
                           "FROM sightline_stuck_sends ORDER BY created_at LIMIT 10;")
        if stuck:
            print(f"\n  Sends claimed but never resolved ({len(stuck)}):")
            for r in stuck:
                print(f"    {r['created_at']:%m-%d %H:%M}  {r['name']} ({r['domain']})")
            print("  These prospects were NOT contacted. Nothing retries them.")
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Pipeline status")
    ap.add_argument("--queue", action="store_true", help="waiting to be scanned")
    ap.add_argument("--review", action="store_true", help="qualified, ranked")
    ap.add_argument("--skipped", action="store_true", help="not qualified, and why")
    ap.add_argument("--errors", action="store_true", help="recent failures")
    ap.add_argument("--limit", type=int, default=25)
    args = ap.parse_args(argv)

    specific = args.queue or args.review or args.skipped or args.errors

    with _conn() as conn, conn.cursor() as cur:
        if args.queue:
            waiting_queue(cur, args.limit)
        if args.review:
            review_queue(cur, args.limit)
        if args.skipped:
            skipped(cur, args.limit)
        if args.errors:
            errors(cur, args.limit)
        if not specific:
            overview(cur)
            review_queue(cur, 10)
            print(f"\n  more: --queue  --review  --skipped  --errors\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
