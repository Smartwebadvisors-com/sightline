"""
sightline_cli.py -- run a Sightline scan, and work out how to do that.

The invocation isn't something to guess at or ask about repeatedly: it is
testable. `detect()` tries each plausible command against a domain you own and
keeps the one that actually writes a row into sightline_scans. Exit code 0
isn't enough evidence -- plenty of ways to exit 0 without scanning anything.
The proof is a new scan row.

    python3 sightline_cli.py --detect          # find it, write it to .env
    python3 sightline_cli.py --scan example.com

Once detected, SIGHTLINE_CMD is written to .env and never guessed again.
"""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
import time
from dataclasses import dataclass
from typing import Any

LOG = logging.getLogger("aeo.sightline.cli")

SIGHTLINE_DIR = os.getenv("SIGHTLINE_DIR", "/root/sightline")
SCAN_TIMEOUT = int(os.getenv("SIGHTLINE_TIMEOUT", "300"))

# Ordered by likelihood. {url} and {domain} are both substituted.
CANDIDATE_COMMANDS = (
    "python3 -m sightline scan {url}",
    "python3 -m sightline.cli scan {url}",
    "python3 -m sightline {url}",
    "python3 main.py scan {url}",
    "python3 cli.py scan {url}",
    "python3 sightline.py scan {url}",
    "python3 main.py {url}",
    "python3 sightline.py {url}",
    "python3 run.py scan {url}",
    "python3 scan.py {url}",
    "./sightline scan {url}",
    "sightline scan {url}",
)


class ScanFailed(RuntimeError):
    pass


@dataclass
class ScanOutcome:
    command: str
    returncode: int
    scan_id: int | None
    duration_s: float
    stdout_tail: str = ""
    stderr_tail: str = ""

    @property
    def ok(self) -> bool:
        return self.scan_id is not None


# ---------------------------------------------------------------------------
# database probes -- proof that a scan actually happened
# ---------------------------------------------------------------------------

def _latest_scan_id(dsn: str, domain: str) -> int | None:
    import psycopg
    with psycopg.connect(dsn, connect_timeout=10) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM sightline_scans "
            "WHERE domain = %(d)s OR url ILIKE %(l)s "
            "ORDER BY COALESCE(completed_at, requested_at) DESC LIMIT 1;",
            {"d": domain, "l": f"%{domain}%"},
        )
        row = cur.fetchone()
        return row[0] if row else None


def has_recent_scan(dsn: str, domain: str, max_age_days: int = 30) -> bool:
    import psycopg
    with psycopg.connect(dsn, connect_timeout=10) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM sightline_scans "
            "WHERE (domain = %(d)s OR url ILIKE %(l)s) AND status = 'complete' "
            "AND COALESCE(completed_at, requested_at) > now() - %(age)s::interval "
            "LIMIT 1;",
            {"d": domain, "l": f"%{domain}%", "age": f"{max_age_days} days"},
        )
        return cur.fetchone() is not None


# ---------------------------------------------------------------------------
# running
# ---------------------------------------------------------------------------

def run_command(command: str, domain: str, dsn: str,
                timeout: int = SCAN_TIMEOUT) -> ScanOutcome:
    url = domain if domain.startswith(("http://", "https://")) else "https://" + domain
    before = _latest_scan_id(dsn, domain)
    cmd = command.format(url=shlex.quote(url), domain=shlex.quote(domain))

    started = time.time()
    try:
        proc = subprocess.run(cmd, shell=True, cwd=SIGHTLINE_DIR,
                              capture_output=True, text=True, timeout=timeout)
        rc, out, err = proc.returncode, proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired:
        return ScanOutcome(command, -1, None, time.time() - started,
                           stderr_tail=f"timed out after {timeout}s")

    after = _latest_scan_id(dsn, domain)
    # A new row (or a changed id) is the only acceptable proof of a scan.
    scan_id = after if after is not None and after != before else None

    return ScanOutcome(
        command=command, returncode=rc, scan_id=scan_id,
        duration_s=time.time() - started,
        stdout_tail=out.strip()[-400:], stderr_tail=err.strip()[-400:],
    )


def detect(dsn: str | None = None, test_domain: str | None = None,
           write_env: str | None = None) -> str | None:
    """Find the working invocation by trying each and checking the database.

    Uses a domain you own by default -- detection scans a real site, and that
    should be yours, not a stranger's.
    """
    dsn = dsn or os.getenv("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL is not set")
    test_domain = test_domain or os.getenv("AEO_ENGINE_CANARY", "smartwebadvisors.com")

    LOG.info("detecting Sightline invocation using %s", test_domain)
    for command in CANDIDATE_COMMANDS:
        LOG.info("trying: %s", command)
        outcome = run_command(command, test_domain, dsn)
        if outcome.ok:
            LOG.info("WORKS: %s (scan id %s in %.0fs)",
                     command, outcome.scan_id, outcome.duration_s)
            if write_env:
                _write_env(write_env, "SIGHTLINE_CMD", command)
            return command
        LOG.info("  no scan row (rc=%s) %s", outcome.returncode,
                 (outcome.stderr_tail or outcome.stdout_tail)[:120])
    return None


def shell_quote(value: str) -> str:
    """Single-quote a value so the shell reads it literally.

    Double quotes are wrong here: a value containing $ or ` is expanded when
    the .env is sourced, and the variable silently comes out mangled. API keys
    and generated passwords contain both. Single quotes expand nothing; an
    embedded single quote is closed, escaped, and reopened.
    """
    return "'" + str(value).replace("'", "'\\''") + "'"


def _write_env(path: str, key: str, value: str) -> None:
    """Set or replace one key in a .env file, quoted so it survives sourcing."""
    lines: list[str] = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    out, replaced = [], False
    for line in lines:
        if line.strip().startswith(f"{key}=") or line.strip().startswith(f"export {key}="):
            out.append(f"{key}={shell_quote(value)}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"{key}={shell_quote(value)}")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out) + "\n")
    LOG.info("wrote %s to %s", key, path)


def scan(domain: str, dsn: str | None = None, command: str | None = None,
         timeout: int = SCAN_TIMEOUT) -> ScanOutcome:
    """Run one Sightline scan. Raises ScanFailed if no scan row appeared."""
    dsn = dsn or os.getenv("DATABASE_URL")
    command = command or os.getenv("SIGHTLINE_CMD")
    if not command:
        raise ScanFailed("SIGHTLINE_CMD is not set -- run --detect first")

    outcome = run_command(command, domain, dsn, timeout)
    if not outcome.ok:
        raise ScanFailed(
            f"no scan row appeared for {domain} (rc={outcome.returncode}): "
            f"{(outcome.stderr_tail or outcome.stdout_tail)[:200]}"
        )
    return outcome


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Run or detect Sightline scans")
    ap.add_argument("--detect", action="store_true",
                    help="find the working invocation and save it to .env")
    ap.add_argument("--scan", metavar="DOMAIN")
    ap.add_argument("--test-domain", default=None,
                    help="domain to use for detection (must be one you own)")
    ap.add_argument("--env", default=".env")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

    if args.detect:
        found = detect(test_domain=args.test_domain, write_env=args.env)
        if found:
            print(f"\nSIGHTLINE_CMD=\"{found}\"\nsaved to {args.env}")
            return 0
        print("\nNone of the candidate commands produced a scan row.\n"
              "Run a scan by hand and tell me the exact command; add it as:\n"
              '  SIGHTLINE_CMD="<your command with {url} where the URL goes>"')
        return 1

    if args.scan:
        outcome = scan(args.scan)
        print(f"scan {outcome.scan_id} completed in {outcome.duration_s:.0f}s")
        return 0

    ap.error("pass --detect or --scan DOMAIN")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
