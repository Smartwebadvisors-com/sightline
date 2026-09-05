"""
autoscan.py -- the unattended run. One command, cron-safe, end to end.

    source prospects -> scan each with Sightline -> probe -> gate -> persist

Written to be run by cron on a box nobody is watching, which means the
interesting parts are all about not doing damage when something goes wrong:

  * a file lock, so a slow run and the next cron tick never overlap. Two
    processes scanning the same prospects would double-spend the API budget
    and race each other to claim sends.
  * preflight first. If the canaries fail, the run stops before spending
    anything -- scores produced with a broken probe are worse than no scores.
  * scan reuse. A domain Sightline scanned recently is not re-scanned; the
    existing row is read instead. Most of the cost of this pipeline is scans.
  * hard caps on scans, probes and spend per run, enforced before each call.
  * resume by construction. Every prospect is persisted as it finishes, so a
    killed run loses at most the one in flight and the next run continues.
  * nothing is ever sent. This produces QUALIFIED rows in the review queue;
    a human still releases them.

    python3 autoscan.py --limit 25
    python3 autoscan.py --limit 25 --source        # refill the queue first
    python3 autoscan.py --dry-run                  # decide, persist nothing
"""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import requests

import sightline_cli
from aeo_gate import GateConfig
from aeo_probe import Prospect, probe as run_probe
from aeo_types import SiteReport
from preflight import HALT, preflight
from resilience import Budget, BudgetExhausted, Guard, RetryPolicy
from scan_prospect import ScanResult, load_suppressed, persist
from sightline_adapter import SightlineError, audit, av_summary

LOG = logging.getLogger("aeo.autoscan")

LOCK_PATH = os.getenv("AEO_LOCK", "/tmp/aeo-autoscan.lock")
SCAN_MAX_AGE_DAYS = int(os.getenv("AEO_SCAN_MAX_AGE_DAYS", "30"))
MAX_SCANS_PER_RUN = int(os.getenv("AEO_MAX_SCANS", "25"))
MAX_PROBES_PER_RUN = int(os.getenv("AEO_MAX_PROBES", "25"))
MAX_SPEND_PER_RUN = float(os.getenv("AEO_MAX_SPEND_USD", "5.00"))
PERPLEXITY_COST = float(os.getenv("AEO_PERPLEXITY_COST", "0.01"))
PAUSE_BETWEEN = float(os.getenv("AEO_SCAN_PAUSE", "2.0"))


class AlreadyRunning(RuntimeError):
    pass


class RunLock:
    """Exclusive lock held for the life of the run.

    flock is released automatically if the process dies, so a crashed run
    never leaves the pipeline permanently wedged.
    """

    def __init__(self, path: str = LOCK_PATH):
        self.path = path
        self._fh = None

    def __enter__(self):
        self._fh = open(self.path, "w")
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._fh.close()
            raise AlreadyRunning(
                f"another autoscan holds {self.path} -- skipping this tick")
        self._fh.write(f"{os.getpid()} {time.time()}\n")
        self._fh.flush()
        return self

    def __exit__(self, *exc):
        if self._fh:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            self._fh.close()


@dataclass
class RunStats:
    considered: int = 0
    scans_run: int = 0
    scans_reused: int = 0
    probes_run: int = 0
    probes_skipped_av: int = 0
    qualified: int = 0
    review: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    started: float = field(default_factory=time.time)

    def summary(self) -> str:
        mins = (time.time() - self.started) / 60
        return (
            f"{self.considered} prospects in {mins:.1f} min\n"
            f"  scans:   {self.scans_run} run, {self.scans_reused} reused\n"
            f"  probes:  {self.probes_run} run, {self.probes_skipped_av} skipped "
            f"(Sightline had observations)\n"
            f"  verdicts: {self.qualified} qualified, {self.review} review, "
            f"{self.skipped} skipped\n"
            f"  errors:  {len(self.errors)}"
        )


def _queue_prospects(limit: int, do_source: bool) -> list[Prospect]:
    """Prospects to work on: the sense queue, topped up from sourcing if asked."""
    from loop_hook import drain_sense_queue

    prospects = drain_sense_queue(limit)
    if prospects or not do_source:
        return prospects

    LOG.info("sense queue empty -- sourcing")
    import sourcing
    result = sourcing.source()
    LOG.info("%s", result.summary().replace("\n", " "))
    sourcing.enqueue(result.prospects)
    return drain_sense_queue(limit)


def process_one(
    prospect: Prospect,
    guard: Guard,
    stats: RunStats,
    config: GateConfig,
    suppressed: set[str],
    session: requests.Session,
    dsn: str,
    dry_run: bool,
) -> ScanResult | None:
    from aeo_gate import evaluate
    from scan_prospect import _empty_probe

    domain = prospect.registrable_domain()

    # --- scan, or reuse a recent one ---------------------------------------
    site: SiteReport | None = None
    if sightline_cli.has_recent_scan(dsn, domain, SCAN_MAX_AGE_DAYS):
        stats.scans_reused += 1
        LOG.info("%s: reusing recent Sightline scan", domain)
    else:
        if stats.scans_run >= MAX_SCANS_PER_RUN:
            LOG.info("%s: scan cap reached, leaving for the next run", domain)
            return None
        try:
            outcome = sightline_cli.scan(domain, dsn)
            stats.scans_run += 1
            LOG.info("%s: scanned (id %s, %.0fs)", domain, outcome.scan_id,
                     outcome.duration_s)
        except sightline_cli.ScanFailed as exc:
            stats.errors.append(f"{domain}: {exc}")
            LOG.warning("%s: scan failed -- %s", domain, exc)
            return None

    try:
        site = audit(domain, engine="sightline-db", session=session)
    except SightlineError as exc:
        stats.errors.append(f"{domain}: {exc}")
        LOG.warning("%s: could not read scan -- %s", domain, exc)
        return None

    # --- probe, unless Sightline already sampled this domain ----------------
    av = av_summary(domain, dsn)
    if av.usable:
        stats.probes_skipped_av += 1
        LOG.info("%s: %d/%d Sightline observations, skipping probe",
                 domain, av.mentions, av.observations)
        probe_result = _empty_probe(prospect)
    elif stats.probes_run >= MAX_PROBES_PER_RUN:
        LOG.info("%s: probe cap reached, auditing only", domain)
        probe_result = _empty_probe(prospect)
    else:
        try:
            probe_result = run_probe(prospect, session=session, guard=guard)
            stats.probes_run += 1
        except BudgetExhausted as exc:
            LOG.error("budget exhausted: %s", exc)
            stats.errors.append(str(exc))
            probe_result = _empty_probe(prospect)
        except Exception as exc:
            stats.errors.append(f"{domain}: probe {type(exc).__name__}: {exc}")
            probe_result = _empty_probe(prospect)

    verdict = evaluate(prospect, probe_result, site, config, suppressed)
    result = ScanResult(prospect=prospect,
                        probe=probe_result if probe_result.probed else None,
                        site=site, verdict=verdict)

    if verdict.status == "QUALIFIED":
        stats.qualified += 1
    elif verdict.status == "REVIEW":
        stats.review += 1
    else:
        stats.skipped += 1

    LOG.info("%s", result.summary_line())

    if not dry_run:
        try:
            persist([result], dsn)
        except Exception as exc:
            stats.errors.append(f"{domain}: persist failed: {exc}")
            LOG.error("%s: could not persist -- %s", domain, exc)

    return result


def run(limit: int = 25, do_source: bool = False, dry_run: bool = False,
        skip_preflight: bool = False) -> RunStats:
    stats = RunStats()
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL is not set")

    session = requests.Session()
    guard = Guard(
        budget=Budget(max_calls={"perplexity": MAX_PROBES_PER_RUN * 6},
                      max_spend_usd=MAX_SPEND_PER_RUN,
                      cost_per_call={"perplexity": PERPLEXITY_COST}),
        retry=RetryPolicy(attempts=3, base_delay_s=2),
    )

    try:
        if not skip_preflight:
            report = preflight(guard, session=session)
            LOG.info("preflight:\n%s", report.render())
            if report.status == HALT:
                stats.errors.append("preflight HALT -- nothing was scanned")
                LOG.error("preflight halted the run; not scanning anything")
                return stats

        prospects = _queue_prospects(limit, do_source)
        if not prospects:
            LOG.info("nothing queued")
            return stats

        config = GateConfig()
        suppressed = load_suppressed(dsn)
        LOG.info("processing %d prospect(s); %d suppressed domain(s) on file",
                 len(prospects), len(suppressed))

        for i, prospect in enumerate(prospects):
            if i:
                time.sleep(PAUSE_BETWEEN)
            stats.considered += 1
            try:
                process_one(prospect, guard, stats, config, suppressed,
                            session, dsn, dry_run)
            except BudgetExhausted as exc:
                LOG.error("stopping run: %s", exc)
                stats.errors.append(str(exc))
                break
            except Exception as exc:
                LOG.exception("%s failed", prospect.domain)
                stats.errors.append(f"{prospect.domain}: {type(exc).__name__}: {exc}")

        LOG.info("budget: %s", guard.budget.summary())
        breaker_state = guard.breaker.state()
        if breaker_state:
            LOG.info("circuits: %s", breaker_state)
    finally:
        session.close()

    return stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Unattended prospect scanning")
    ap.add_argument("--limit", type=int, default=25)
    ap.add_argument("--source", action="store_true",
                    help="source new prospects if the queue is empty")
    ap.add_argument("--dry-run", action="store_true",
                    help="decide but persist nothing")
    ap.add_argument("--skip-preflight", action="store_true",
                    help="not recommended; preflight is what catches bad data")
    ap.add_argument("--json", action="store_true", help="machine-readable summary")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
    )

    try:
        with RunLock():
            stats = run(args.limit, args.source, args.dry_run, args.skip_preflight)
    except AlreadyRunning as exc:
        LOG.info("%s", exc)
        return 0     # not an error: the previous run is still working

    if args.json:
        print(json.dumps({
            "considered": stats.considered,
            "scans_run": stats.scans_run,
            "scans_reused": stats.scans_reused,
            "probes_run": stats.probes_run,
            "qualified": stats.qualified,
            "review": stats.review,
            "skipped": stats.skipped,
            "errors": stats.errors,
        }, indent=2))
    else:
        print("\n" + stats.summary())
        for err in stats.errors[:10]:
            print(f"  ! {err}")

    return 1 if stats.errors and stats.considered == 0 else 0


if __name__ == "__main__":
    sys.exit(main())
