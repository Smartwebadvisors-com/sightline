"""
scan_prospect.py -- orchestrator: probe + audit + gate + persist.

This is the "reason" stage of the sense-reason-act loop. Sense hands it
prospects; it returns verdicts and never contacts anyone. Acting on a QUALIFIED
verdict is a separate, gated step (see loop_hook.py).

CLI:
    # one prospect, no database, JSON to stdout
    python3 scan_prospect.py --name "Otter Squad Plumbing" \
        --domain ottersquadplumbing.com --category plumber \
        --city Allentown --state PA

    # a batch from JSON, written to Postgres
    python3 scan_prospect.py --file prospects.json --persist

    # audit only, no Perplexity spend
    python3 scan_prospect.py --file prospects.json --no-probe

Env:
    PERPLEXITY_API_KEY   required unless --no-probe
    PSI_API_KEY          optional (PageSpeed pillar)
    DATABASE_URL         required with --persist
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

import requests

from aeo_gate import GateConfig, Verdict, evaluate
from aeo_probe import ProbeResult, Prospect, probe as run_probe
from aeo_types import SiteReport
from sightline_adapter import SightlineError, audit

LOG = logging.getLogger("aeo.scan")

PAUSE_BETWEEN_PROSPECTS = float(os.getenv("AEO_SCAN_PAUSE", "2.0"))


@dataclass
class ScanResult:
    prospect: Prospect
    probe: ProbeResult | None
    site: SiteReport
    verdict: Verdict
    duration_s: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "prospect": asdict(self.prospect),
            "probe": self.probe.to_dict() if self.probe else None,
            "site": self.site.to_dict(),
            "verdict": self.verdict.to_dict(),
            "duration_s": round(self.duration_s, 1),
        }

    def summary_line(self) -> str:
        vis = self.probe.visibility_score if self.probe else "--"
        return (
            f"{self.verdict.status:<9} p{self.verdict.priority:<3} "
            f"vis={vis:<3} aeo={self.site.aeo_score:<3} "
            f"{self.prospect.name}  [{self.verdict.rule}]"
        )


def _empty_probe(prospect: Prospect) -> ProbeResult:
    """Stand-in when --no-probe is used, so the gate still has a shape to read."""
    return ProbeResult(
        domain=prospect.registrable_domain(),
        name=prospect.name,
        visibility_score=0,
        unbranded_asked=0,
        unbranded_cited=0,
        unbranded_mentioned=0,
        branded_found=False,
        probed=False,
    )


DEFAULT_ENGINE = os.getenv("AEO_ENGINE", "sightline-cli")


def scan(
    prospect: Prospect,
    config: GateConfig | None = None,
    do_probe: bool = True,
    suppressed_domains: set[str] | None = None,
    session: requests.Session | None = None,
    engine: str = DEFAULT_ENGINE,
) -> ScanResult:
    started = time.time()
    own_session = session is None
    session = session or requests.Session()
    try:
        try:
            site = audit(prospect.domain, engine=engine, session=session)
        except SightlineError as exc:
            # Sightline unavailable or has no scan on record: fall back rather
            # than dropping the prospect, and record why.
            LOG.warning("%s: %s -- falling back to builtin audit", prospect.domain, exc)
            site = audit(prospect.domain, engine="builtin", session=session)
            site.errors.append(f"sightline_fallback: {exc}")

        # No point spending probe calls on a site we cannot audit.
        if do_probe and site.reachable:
            probe_result = run_probe(prospect, session=session)
        else:
            probe_result = _empty_probe(prospect)

        verdict = evaluate(prospect, probe_result, site, config, suppressed_domains)
        return ScanResult(
            prospect=prospect,
            probe=probe_result if do_probe and site.reachable else None,
            site=site,
            verdict=verdict,
            duration_s=time.time() - started,
        )
    finally:
        if own_session:
            session.close()


def scan_batch(
    prospects: Iterable[Prospect],
    config: GateConfig | None = None,
    do_probe: bool = True,
    suppressed_domains: set[str] | None = None,
    engine: str = DEFAULT_ENGINE,
) -> list[ScanResult]:
    results: list[ScanResult] = []
    session = requests.Session()
    try:
        for i, prospect in enumerate(prospects):
            if i:
                time.sleep(PAUSE_BETWEEN_PROSPECTS)
            try:
                result = scan(prospect, config, do_probe, suppressed_domains, session, engine)
                LOG.info(result.summary_line())
                results.append(result)
            except Exception as exc:
                LOG.exception("scan failed for %s: %s", prospect.domain, exc)
    finally:
        session.close()
    return sorted(results, key=lambda r: -r.verdict.priority)


# --------------------------------------------------------------------------
# persistence (psycopg 3; import is lazy so the module works without a DB)
# --------------------------------------------------------------------------

UPSERT_PROSPECT = """
INSERT INTO sightline_prospects (domain, name, category, city, state, place_id,
                       review_count, rating)
VALUES (%(domain)s, %(name)s, %(category)s, %(city)s, %(state)s, %(place_id)s,
        %(review_count)s, %(rating)s)
ON CONFLICT (domain) DO UPDATE SET
    name         = EXCLUDED.name,
    category     = EXCLUDED.category,
    city         = EXCLUDED.city,
    state        = EXCLUDED.state,
    place_id     = COALESCE(EXCLUDED.place_id, sightline_prospects.place_id),
    review_count = EXCLUDED.review_count,
    rating       = EXCLUDED.rating,
    updated_at   = now()
RETURNING id;
"""

INSERT_SCAN = """
INSERT INTO sightline_prospect_scans (
    prospect_id, visibility_score, unbranded_asked, unbranded_cited,
    unbranded_mentioned, branded_found, aeo_score, pillar_scores,
    psi_performance, psi_seo, findings_failed, gate_status, gate_rule,
    gate_priority, gate_hook, gate_reasons, probe_raw, site_raw, scan_errors,
    engine, geo_score, seo_score, report_url
) VALUES (
    %(prospect_id)s, %(visibility_score)s, %(unbranded_asked)s, %(unbranded_cited)s,
    %(unbranded_mentioned)s, %(branded_found)s, %(aeo_score)s, %(pillar_scores)s,
    %(psi_performance)s, %(psi_seo)s, %(findings_failed)s, %(gate_status)s, %(gate_rule)s,
    %(gate_priority)s, %(gate_hook)s, %(gate_reasons)s, %(probe_raw)s, %(site_raw)s,
    %(scan_errors)s, %(engine)s, %(geo_score)s, %(seo_score)s, %(report_url)s
) RETURNING id;
"""


def persist(results: Iterable[ScanResult], dsn: str | None = None) -> list[int]:
    import psycopg
    from psycopg.types.json import Jsonb

    dsn = dsn or os.getenv("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL is not set")

    scan_ids: list[int] = []
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            for r in results:
                p, s, v = r.prospect, r.site, r.verdict
                cur.execute(UPSERT_PROSPECT, {
                    "domain": p.registrable_domain(),
                    "name": p.name,
                    "category": p.category,
                    "city": p.city,
                    "state": p.state,
                    "place_id": p.place_id,
                    "review_count": p.review_count,
                    "rating": p.rating,
                })
                prospect_id = cur.fetchone()[0]

                cur.execute(INSERT_SCAN, {
                    "prospect_id": prospect_id,
                    "visibility_score": r.probe.visibility_score if r.probe else None,
                    "unbranded_asked": r.probe.unbranded_asked if r.probe else None,
                    "unbranded_cited": r.probe.unbranded_cited if r.probe else None,
                    "unbranded_mentioned": r.probe.unbranded_mentioned if r.probe else None,
                    "branded_found": r.probe.branded_found if r.probe else None,
                    "engine": s.engine,
                    "report_url": s.report_url,
                    "geo_score": s.geo_score,
                    "seo_score": s.seo_score,
                    "aeo_score": s.aeo_score,
                    "pillar_scores": Jsonb(s.pillar_scores),
                    "psi_performance": s.psi_performance,
                    "psi_seo": s.psi_seo,
                    "findings_failed": len(s.failed),
                    "gate_status": v.status,
                    "gate_rule": v.rule,
                    "gate_priority": v.priority,
                    "gate_hook": v.hook,
                    "gate_reasons": Jsonb(v.reasons),
                    "probe_raw": Jsonb(r.probe.to_dict()) if r.probe else None,
                    "site_raw": Jsonb(s.to_dict()),
                    "scan_errors": Jsonb(s.errors + (r.probe.errors if r.probe else [])),
                })
                scan_ids.append(cur.fetchone()[0])
        conn.commit()
    return scan_ids


def load_suppressed(dsn: str | None = None) -> set[str]:
    """Domains never to contact: clients, existing GHL contacts, opt-outs."""
    dsn = dsn or os.getenv("DATABASE_URL")
    if not dsn:
        return set()
    try:
        import psycopg
        with psycopg.connect(dsn) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT domain FROM sightline_prospects "
                "WHERE suppressed OR ghl_contact_id IS NOT NULL"
            )
            return {row[0] for row in cur.fetchall()}
    except Exception as exc:
        LOG.warning("could not load suppression list: %s", exc)
        return set()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _prospects_from_file(path: str) -> list[Prospect]:
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    rows = raw["prospects"] if isinstance(raw, dict) else raw
    allowed = set(Prospect.__dataclass_fields__)
    return [Prospect(**{k: v for k, v in row.items() if k in allowed}) for row in rows]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Scan prospects for AEO gaps")
    src = ap.add_argument_group("input")
    src.add_argument("--file", help="JSON file: list of prospect objects")
    src.add_argument("--name")
    src.add_argument("--domain")
    src.add_argument("--category")
    src.add_argument("--city")
    src.add_argument("--state")
    src.add_argument("--reviews", type=int)
    src.add_argument("--rating", type=float)

    ap.add_argument("--no-probe", action="store_true",
                    help="skip Perplexity; technical audit only")
    ap.add_argument("--engine", default=DEFAULT_ENGINE,
                    choices=["sightline-cli", "sightline-db", "builtin"],
                    help="which audit engine produces the site score")
    ap.add_argument("--persist", action="store_true", help="write to Postgres")
    ap.add_argument("--out", help="write full JSON results to this path")
    ap.add_argument("--min-reviews", type=int, default=GateConfig.min_reviews)
    ap.add_argument("--max-visibility", type=int, default=GateConfig.max_visibility)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
    )

    if args.file:
        prospects = _prospects_from_file(args.file)
    elif all([args.name, args.domain, args.category, args.city, args.state]):
        prospects = [Prospect(
            name=args.name, domain=args.domain, category=args.category,
            city=args.city, state=args.state,
            review_count=args.reviews, rating=args.rating,
        )]
    else:
        ap.error("provide --file, or all of --name --domain --category --city --state")
        return 2

    config = GateConfig(min_reviews=args.min_reviews,
                        max_visibility=args.max_visibility)
    suppressed = load_suppressed() if args.persist else set()

    results = scan_batch(prospects, config, do_probe=not args.no_probe,
                         suppressed_domains=suppressed, engine=args.engine)

    print("\n--- queue -------------------------------------------------------")
    for r in results:
        print(r.summary_line())
    qualified = [r for r in results if r.verdict.status == "QUALIFIED"]
    print(f"\n{len(qualified)} qualified of {len(results)} scanned")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump([r.to_dict() for r in results], fh, indent=2)
        print(f"wrote {args.out}")

    if args.persist:
        ids = persist(results)
        print(f"persisted {len(ids)} scan row(s)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
