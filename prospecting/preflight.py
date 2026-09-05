"""
preflight.py -- prove the instruments work before trusting what they measure.

Runs before every batch. Two canaries, chosen to fail in opposite directions,
because between them they catch the failure that no HTTP status code reveals:
a dependency that answers 200 with data we are silently misreading.

    POSITIVE canary   a business we KNOW answer engines cite for its category
                      and city. If the probe reports it invisible, the probe is
                      broken -- not the market. HALT.

    NEGATIVE canary   a domain that cannot legitimately be cited for anything.
                      If the probe reports it cited, our matching logic is
                      generating false positives. HALT.

Two API calls. They cost pennies and they are the difference between "the run
failed" and "the run quietly emailed two hundred people a false claim."

Set your own positive canary -- pick a business you are certain shows up:

    export CANARY_NAME="Rita's Italian Ice"
    export CANARY_DOMAIN=ritasice.com
    export CANARY_CATEGORY="italian ice shop"
    export CANARY_CITY=Bethlehem
    export CANARY_STATE=PA

Usage:
    python3 preflight.py              # exits 0 GO, 1 DEGRADED, 2 HALT
    from preflight import preflight; report = preflight(guard)
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field
from typing import Any

import requests

from aeo_probe import Prospect, build_queries, evaluate_response
from resilience import (BudgetExhausted, DependencyError, Guard, ShapeError,
                        validate_probe_payload)

LOG = logging.getLogger("aeo.preflight")

GO, DEGRADED, HALT = "GO", "DEGRADED", "HALT"

POSITIVE_CANARY = Prospect(
    name=os.getenv("CANARY_NAME", "Rita's Italian Ice"),
    domain=os.getenv("CANARY_DOMAIN", "ritasice.com"),
    category=os.getenv("CANARY_CATEGORY", "italian ice shop"),
    city=os.getenv("CANARY_CITY", "Bethlehem"),
    state=os.getenv("CANARY_STATE", "PA"),
)

# Deliberately absurd: registered to nobody, cited by nothing.
NEGATIVE_CANARY = Prospect(
    name="Zzyzx Quantum Drainworks Unlimited",
    domain="zzyzx-quantum-drainworks-unlimited-9f21x.example",
    category=POSITIVE_CANARY.category,
    city=POSITIVE_CANARY.city,
    state=POSITIVE_CANARY.state,
)


@dataclass
class Check:
    name: str
    status: str            # GO | DEGRADED | HALT
    detail: str

    def line(self) -> str:
        mark = {"GO": "ok  ", "DEGRADED": "warn", "HALT": "HALT"}[self.status]
        return f"  {mark}  {self.name}: {self.detail}"


@dataclass
class PreflightReport:
    checks: list[Check] = field(default_factory=list)

    @property
    def status(self) -> str:
        if any(c.status == HALT for c in self.checks):
            return HALT
        if any(c.status == DEGRADED for c in self.checks):
            return DEGRADED
        return GO

    def render(self) -> str:
        lines = [c.line() for c in self.checks]
        lines.append(f"\npreflight: {self.status}")
        return "\n".join(lines)

    def exit_code(self) -> int:
        return {GO: 0, DEGRADED: 1, HALT: 2}[self.status]


def _probe_once(prospect: Prospect, guard: Guard,
                session: requests.Session) -> tuple[bool, str]:
    """Run one unbranded query. Returns (cited_or_mentioned, detail)."""
    from aeo_probe import _ask

    api_key = os.getenv("PERPLEXITY_API_KEY")
    if not api_key:
        raise DependencyError("perplexity", "PERPLEXITY_API_KEY not set")

    query, kind = build_queries(prospect)[0]
    payload = guard.call(
        "perplexity",
        lambda: _ask(query, api_key, session),
        validate=validate_probe_payload,
    )
    result = evaluate_response(prospect, query, kind, payload)
    detail = (f"{'cited' if result.cited else 'not cited'}, "
              f"{'named' if result.mentioned else 'not named'} "
              f"({len(result.sources)} sources)")
    return (result.cited or result.mentioned), detail


def check_positive_canary(guard: Guard, session: requests.Session) -> Check:
    name = f"positive canary ({POSITIVE_CANARY.name})"
    try:
        found, detail = _probe_once(POSITIVE_CANARY, guard, session)
    except ShapeError as exc:
        return Check(name, HALT, f"response shape changed -- {exc.message}")
    except (DependencyError, BudgetExhausted) as exc:
        return Check(name, HALT, f"probe unavailable -- {exc}")

    if found:
        return Check(name, GO, detail)
    return Check(
        name, HALT,
        f"{detail}. A business we expect to be cited came back absent -- treat "
        "every invisibility score in this batch as unreliable until this passes.",
    )


def check_negative_canary(guard: Guard, session: requests.Session) -> Check:
    name = "negative canary (nonexistent business)"
    try:
        found, detail = _probe_once(NEGATIVE_CANARY, guard, session)
    except ShapeError as exc:
        return Check(name, HALT, f"response shape changed -- {exc.message}")
    except (DependencyError, BudgetExhausted) as exc:
        return Check(name, DEGRADED, f"could not run -- {exc}")

    if not found:
        return Check(name, GO, detail)
    return Check(
        name, HALT,
        "a business that does not exist was reported as cited -- the matching "
        "logic is producing false positives, so visibility scores are inflated.",
    )


def check_database() -> Check:
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        return Check("database", DEGRADED, "DATABASE_URL not set (no persistence)")
    try:
        import psycopg
        with psycopg.connect(dsn, connect_timeout=10) as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM sightline_prospects;")
            n = cur.fetchone()[0]
        return Check("database", GO, f"connected, {n} prospect(s) on record")
    except Exception as exc:
        return Check("database", HALT, f"{type(exc).__name__}: {exc}")


def check_audit_engine(session: requests.Session) -> Check:
    """Confirm the configured engine answers for a domain we control."""
    engine = os.getenv("AEO_ENGINE", "sightline-cli")
    probe_domain = os.getenv("AEO_ENGINE_CANARY", "smartwebadvisors.com")
    try:
        from sightline_adapter import audit
        report = audit(probe_domain, engine=engine, session=session,
                       supplement=False)
    except Exception as exc:
        return Check(f"audit engine ({engine})", DEGRADED,
                     f"{type(exc).__name__}: {exc} -- runs will fall back to builtin")

    if not report.reachable:
        return Check(f"audit engine ({engine})", DEGRADED,
                     f"returned unreachable for {probe_domain}")
    if not report.findings:
        return Check(f"audit engine ({engine})", DEGRADED,
                     "returned no findings at all -- check the field mapping")
    return Check(f"audit engine ({engine})", GO,
                 f"scored {probe_domain} at {report.aeo_score} "
                 f"with {len(report.findings)} finding(s)")


def preflight(guard: Guard | None = None, skip_probe: bool = False,
              session: requests.Session | None = None) -> PreflightReport:
    guard = guard or Guard()
    own_session = session is None
    session = session or requests.Session()
    report = PreflightReport()
    try:
        report.checks.append(check_database())
        report.checks.append(check_audit_engine(session))
        if not skip_probe:
            report.checks.append(check_positive_canary(guard, session))
            report.checks.append(check_negative_canary(guard, session))
        else:
            report.checks.append(Check("canaries", DEGRADED, "skipped"))
    finally:
        if own_session:
            session.close()
    return report


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Check dependencies before a batch")
    ap.add_argument("--skip-probe", action="store_true",
                    help="database and audit engine only, no API spend")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
    report = preflight(skip_probe=args.skip_probe)
    print(report.render())
    if report.status == HALT:
        print("\nDo not run the batch. Scores produced now cannot be trusted.")
    return report.exit_code()


if __name__ == "__main__":
    sys.exit(main())
