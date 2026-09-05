"""
recheck.py -- re-run the gate over scans already in the database.

Nothing is re-scanned and nothing is re-probed. Every scan row already stores
the full SiteReport and ProbeResult it was judged on, so when a gate rule is
corrected the old verdicts can be recomputed for free -- and, crucially, before
anyone is emailed on the strength of one.

    python3 recheck.py                   # every scan whose verdict would change
    python3 recheck.py --rule AI_CRAWLERS_BLOCKED
    python3 recheck.py --all             # show unchanged ones too
    python3 recheck.py --apply           # write the corrected verdicts back

Without --apply this is read-only. With it, the gate_* columns of the affected
scan rows are rewritten in place and a note is appended to scan_errors so the
change is visible later.
"""

from __future__ import annotations

import argparse
import os
import sys
import textwrap

from aeo_gate import evaluate
from aeo_probe import ProbeResult, Prospect
from aeo_types import Finding, SiteReport

BAR = "-" * 74


def _conn():
    import psycopg
    from psycopg.rows import dict_row

    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL is not set -- run: set -a && . ./.env && set +a")
        sys.exit(1)
    return psycopg.connect(dsn, row_factory=dict_row, connect_timeout=10)


# The latest scan per prospect, which is what the outreach queue reads.
LATEST_SQL = """
SELECT DISTINCT ON (s.prospect_id)
       s.id AS scan_id, s.prospect_id, s.scanned_at,
       s.gate_status, s.gate_rule, s.gate_priority, s.gate_hook,
       s.probe_raw, s.site_raw,
       p.name, p.domain, p.category, p.city, p.state,
       p.review_count, p.rating, p.suppressed
FROM   sightline_prospect_scans s
JOIN   sightline_prospects p ON p.id = s.prospect_id
ORDER  BY s.prospect_id, s.scanned_at DESC;
"""

UPDATE_SQL = """
UPDATE sightline_prospect_scans
SET    gate_status = %(status)s,
       gate_rule = %(rule)s,
       gate_priority = %(priority)s,
       gate_hook = %(hook)s,
       gate_reasons = %(reasons)s,
       scan_errors = coalesce(scan_errors, '[]'::jsonb) || %(note)s
WHERE  id = %(scan_id)s;
"""


def _prospect(row: dict) -> Prospect:
    return Prospect(
        name=row["name"], domain=row["domain"], category=row["category"],
        city=row["city"], state=row["state"],
        review_count=row["review_count"],
        rating=float(row["rating"]) if row["rating"] is not None else None,
    )


def _probe(raw: dict | None, row: dict) -> ProbeResult:
    """Rebuild the ProbeResult. A missing one means the probe was skipped."""
    if not raw:
        return ProbeResult(domain=row["domain"], name=row["name"],
                           visibility_score=0, unbranded_asked=0,
                           unbranded_cited=0, unbranded_mentioned=0,
                           branded_found=False, probed=False)
    return ProbeResult(
        domain=raw.get("domain") or row["domain"],
        name=raw.get("name") or row["name"],
        visibility_score=raw.get("visibility_score") or 0,
        unbranded_asked=raw.get("unbranded_asked") or 0,
        unbranded_cited=raw.get("unbranded_cited") or 0,
        unbranded_mentioned=raw.get("unbranded_mentioned") or 0,
        branded_found=bool(raw.get("branded_found")),
        probed=raw.get("probed", True),
        trustworthy=raw.get("trustworthy", True),
        top_competitors=[tuple(c) for c in raw.get("top_competitors") or []],
        errors=raw.get("errors") or [],
    )


def _site(raw: dict) -> SiteReport:
    findings = [
        Finding(id=f.get("id", ""), pillar=f.get("pillar", ""),
                label=f.get("label", ""), passed=bool(f.get("passed")),
                severity=f.get("severity", "info"), weight=f.get("weight", 0),
                detail=f.get("detail", ""), evidence=f.get("evidence", ""))
        for f in raw.get("findings") or []
    ]
    return SiteReport(
        domain=raw.get("domain", ""), url=raw.get("url", ""),
        reachable=bool(raw.get("reachable")), status_code=raw.get("status_code"),
        aeo_score=raw.get("aeo_score") or 0,
        pillar_scores=raw.get("pillar_scores") or {},
        findings=findings,
        errors=raw.get("errors") or [],
        engine=raw.get("engine", "sightline"),
        geo_score=raw.get("geo_score"), seo_score=raw.get("seo_score"),
        external_scan_id=raw.get("external_scan_id"),
    )


def _wrap(text: str, indent: str = "      ") -> str:
    return textwrap.fill(text or "", 74, initial_indent=indent,
                         subsequent_indent=indent)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rule", help="only scans that fired this rule")
    ap.add_argument("--all", action="store_true",
                    help="show scans whose verdict is unchanged too")
    ap.add_argument("--apply", action="store_true",
                    help="write the corrected verdicts back to the database")
    args = ap.parse_args()

    from psycopg.types.json import Jsonb

    with _conn() as conn, conn.cursor() as cur:
        cur.execute(LATEST_SQL)
        rows = cur.fetchall()

        if args.rule:
            rows = [r for r in rows if r["gate_rule"] == args.rule]

        suppressed = {r["domain"] for r in rows if r["suppressed"]}

        changed, same, updates = [], 0, []
        for row in rows:
            if not row["site_raw"]:
                continue
            verdict = evaluate(_prospect(row), _probe(row["probe_raw"], row),
                               _site(row["site_raw"]),
                               suppressed_domains=suppressed)
            moved = (verdict.status != row["gate_status"]
                     or verdict.rule != row["gate_rule"]
                     or (verdict.hook or "") != (row["gate_hook"] or ""))
            if moved:
                changed.append((row, verdict))
                updates.append((row, verdict))
            else:
                same += 1

        print(f"\n{len(rows)} scan(s) examined"
              + (f" with rule {args.rule}" if args.rule else ""))
        print(f"{len(changed)} would change, {same} unchanged")

        for row, v in changed:
            print(f"\n{BAR}")
            print(f"  {row['name']}  ({row['domain']})")
            print(f"    was  {row['gate_status']:<9} {row['gate_rule']}"
                  f"  p{row['gate_priority']}")
            if row["gate_hook"]:
                print(_wrap(row["gate_hook"]))
            print(f"    now  {v.status:<9} {v.rule}  p{v.priority}")
            if v.hook:
                print(_wrap(v.hook))

        if args.all:
            for row in rows:
                if row not in [r for r, _ in changed]:
                    print(f"  unchanged  {row['gate_status']:<9} "
                          f"{row['gate_rule']:<28} {row['domain']}")

        if not updates:
            print("\nNothing to correct.\n")
            return 0

        if not args.apply:
            print(f"\n{BAR}")
            print("Read-only. Re-run with --apply to write these back.\n")
            return 0

        for row, v in updates:
            cur.execute(UPDATE_SQL, {
                "scan_id": row["scan_id"],
                "status": v.status, "rule": v.rule, "priority": v.priority,
                "hook": v.hook, "reasons": Jsonb(v.reasons),
                "note": Jsonb([f"recheck: {row['gate_rule']} -> {v.rule}"]),
            })
        conn.commit()
        print(f"\n{len(updates)} verdict(s) corrected in the database.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
