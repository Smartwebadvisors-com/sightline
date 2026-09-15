#!/usr/bin/env python3
"""Re-run structured_data + entity_consistency on the scans that stored the
false "no Organization node" finding, so existing reports show what a rescan
would show.

Why not just delete the false rows: db.write_findings() only upserts, so a
finding a re-run no longer emits is never removed, and `sightline scan`
creates a NEW scan rather than updating the old one. Rescoring alone does
nothing either — compute_report reads the stored row, which still asserts the
absence.

Why not insert a `pass` row instead: on the 13 scans where
entity_consistency short-circuited (it returns early when it finds no
entity), the old run never produced name_match / phone_match / sameAs at
all. Filling in a pass for entity_present alone scored lehighvalleyelectric
at 96.0 on that dimension where a rescan gives 92.8 — it fills the
denominator slot while hiding the two real gaps (sameAs medium, phone_match
low) the fresh run finds, and it would have to invent the "Found entity 'X'
of type Y" sentence, since those rows never stored a name.

So this refetches the page and re-runs the two checks properly, upserting
what they now emit and deleting only what they no longer emit. That
reproduces a rescan by construction. It touches no paid API: rank.py and
pagespeed.py are not run.

Findings it rewrites are stamped evidence.rechecked_at, because they
describe the page TODAY while the rest of the scan is from its original
date — the same honesty the SEO backfill keeps with backfilled/measured_at.

    .venv/bin/python tools/recheck_organization_scans.py            # dry run
    .venv/bin/python tools/recheck_organization_scans.py --apply

Then:
    .venv/bin/python -m sightline.cli migrate-findings --refresh-profiles
    .venv/bin/python -m sightline.cli rescore all --version v2
"""
from __future__ import annotations

import argparse
import os
import pathlib
import sys
from datetime import datetime, timezone

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

# sightline.config reads env at import and does not load .env itself.
for _line in (_ROOT / ".env").read_text().splitlines():
    _line = _line.strip()
    if _line and not _line.startswith("#") and "=" in _line:
        _k, _v = _line.split("=", 1)
        os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

from sightline import db                                     # noqa: E402
from sightline import findings as findings_mod               # noqa: E402
from sightline.checks import entity_consistency              # noqa: E402
from sightline.checks import structured_data                 # noqa: E402
from sightline.checks.base import ScanContext                # noqa: E402
from sightline.checks.schema_org import is_organization      # noqa: E402
from sightline.fetch import discovery                        # noqa: E402
from sightline.fetch import http as fetch_http               # noqa: E402

CHECKS = (structured_data, entity_consistency)
CHECK_IDS = tuple(m.CHECK_ID for m in CHECKS)
GAP = ("low", "medium", "high", "critical")

# A scan is affected when it asserts one of these AND its own stored type
# list proves an Organization-family node was on the page.
FALSE_CLAIMS = (("structured_data", "missing:Organization"),
                ("entity_consistency", "entity_present"))

AFFECTED = """
SELECT s.id, s.url, s.domain,
       (SELECT p.evidence->'types'
          FROM sightline_findings p
         WHERE p.scan_id = s.id
           AND p.check_id = 'structured_data'
           AND p.item_key = 'presence') AS types
  FROM sightline_scans s
 WHERE EXISTS (SELECT 1 FROM sightline_findings f
                WHERE f.scan_id = s.id
                  AND (f.check_id, f.item_key) IN (('structured_data',
                                                    'missing:Organization'),
                                                   ('entity_consistency',
                                                    'entity_present'))
                  AND f.severity = ANY(%s))
 ORDER BY s.id
"""


def affected_scans() -> tuple[list[dict], list[dict]]:
    """(provably affected, skipped-because-unprovable)."""
    with db.conn() as c:
        rows = c.execute(AFFECTED, (list(GAP),)).fetchall()
    hit, skip = [], []
    for r in rows:
        if r["types"] and is_organization(r["types"]):
            hit.append(r)
        else:
            skip.append(r)
    return hit, skip


def recheck(scan: dict, apply: bool) -> str:
    """Re-run the two checks for one scan. Returns a one-line report."""
    r = fetch_http.fetch_as_browser(scan["url"])
    if not r.ok:
        return (f"  SKIP   scan {scan['id']:>4} {scan['domain'][:32]:<32} "
                f"fetch failed (status={r.status} {r.error or ''}); "
                "left untouched")

    final = r.final_url or scan["url"]
    ctx = ScanContext(url=final, origin=fetch_http.origin(final),
                      domain=fetch_http.domain(final))
    ctx.page_fetch = r
    ctx.page = discovery.parse_page(final, r.text)
    ctx.profile = findings_mod.profile_from_page(ctx.page, ctx.domain)

    stamp = datetime.now(timezone.utc).isoformat()
    observations = []
    for mod in CHECKS:
        for obs in mod.run(ctx):
            obs.evidence = {**(obs.evidence or {}), "rechecked_at": stamp}
            observations.append(obs)

    fresh = {(o.check_id, o.item_key) for o in observations}

    with db.conn() as c:
        stored = c.execute(
            """SELECT id, check_id, item_key, severity
                 FROM sightline_findings
                WHERE scan_id = %s AND check_id = ANY(%s)""",
            (scan["id"], list(CHECK_IDS))).fetchall()
    stale = [row for row in stored
             if (row["check_id"], row["item_key"]) not in fresh]

    if apply:
        # build_all is the only path from observation to stored finding, so
        # both copy blocks exist before anything is persisted (pipeline.py).
        db.write_findings(scan["id"],
                          findings_mod.build_all(observations, ctx.profile))
        if stale:
            with db.conn() as c:
                c.execute("DELETE FROM sightline_findings WHERE id = ANY(%s)",
                          ([row["id"] for row in stale],))
                c.commit()

    gone = ", ".join(f"{row['item_key']}({row['severity']})" for row in stale)
    return (f"  {'ok' if apply else 'would':<6} scan {scan['id']:>4} "
            f"{scan['domain'][:32]:<32} "
            f"{len(observations)} finding(s) rewritten, "
            f"{len(stale)} removed{': ' + gone if gone else ''}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="write (default is a dry run)")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    hit, skip = affected_scans()
    if args.limit:
        hit = hit[:args.limit]

    print(f"{len(hit)} scan(s) to recheck across "
          f"{len({s['domain'] for s in hit})} domain(s); "
          f"{len(skip)} left alone (no stored type list, or genuinely has no "
          f"Organization node)")
    print(f"one page fetch each, no DataForSEO or PageSpeed calls\n")

    for scan in hit:
        print(recheck(scan, args.apply))

    if not args.apply:
        print("\ndry run; nothing written. Re-run with --apply.")
        return 0
    print("\nnow run:")
    print("  .venv/bin/python -m sightline.cli migrate-findings "
          "--refresh-profiles")
    print("  .venv/bin/python -m sightline.cli rescore all --version v2")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
