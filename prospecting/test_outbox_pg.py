"""
test_outbox_pg.py -- prove the duplicate-send guard against a real Postgres.

The at-most-once guarantee rests on a UNIQUE constraint, not on application
logic, so testing it with mocks proves nothing. This runs the actual schema and
the actual concurrent inserts.

    DATABASE_URL=postgres://... python3 test_outbox_pg.py

Uses a throwaway database. Skips cleanly (exit 0) if none is configured.
"""

from __future__ import annotations

import os
import sys
from concurrent.futures import ThreadPoolExecutor

FAILURES: list[str] = []


def check(label: str, got, want) -> None:
    if got == want:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}: got {got!r}, want {want!r}")
        FAILURES.append(label)


DSN = os.getenv("DATABASE_URL")
if not DSN:
    print("DATABASE_URL not set -- skipping live database tests")
    sys.exit(0)

import psycopg


# ---------------------------------------------------------------------------
# This suite DROPS AND RECREATES tables. That is fine on a scratch database and
# catastrophic on one holding a sourced queue or real prospect history, so it
# refuses to run unless the database looks empty.
#
# Override deliberately if you know what you are doing:
#     AEO_TEST_DESTRUCTIVE=1 DATABASE_URL=... python3 test_outbox_pg.py
# ---------------------------------------------------------------------------

def _refuse_if_real_data(dsn: str) -> None:
    import psycopg

    if os.getenv("AEO_TEST_DESTRUCTIVE") == "1":
        return
    counts = {}
    with psycopg.connect(dsn, connect_timeout=10) as conn, conn.cursor() as cur:
        for table in ("sightline_prospects", "sightline_prospect_scans",
                      "sightline_queue", "sightline_outreach_events"):
            try:
                cur.execute(f"SELECT count(*) FROM {table};")
                n = cur.fetchone()[0]
                if n:
                    counts[table] = n
            except Exception:
                pass          # table not created yet -- nothing to lose
    if counts:
        held = ", ".join(f"{v} in {k}" for k, v in counts.items())
        print("REFUSING TO RUN -- this suite drops tables and this database "
              "holds real data:")
        print(f"  {held}")
        print("\nRun it against a scratch database instead:")
        print("  createdb aeo_scratch")
        print("  DATABASE_URL=postgres:///aeo_scratch python3 test_outbox_pg.py")
        print("\nOr override if you genuinely want the data gone:")
        print("  AEO_TEST_DESTRUCTIVE=1 python3 test_outbox_pg.py")
        sys.exit(0)


_refuse_if_real_data(DSN)

from outbox import (claim_send, daily_sent_count, mark_failed, mark_sent,
                    stuck_sends, suppress)

# --------------------------------------------------------------------------
print("\nschema")
with psycopg.connect(DSN) as conn:
    with conn.cursor() as cur:
        cur.execute("DROP VIEW IF EXISTS sightline_stuck_sends, sightline_outreach_queue CASCADE;")
        cur.execute("DROP TABLE IF EXISTS sightline_outreach_events, "
                    "sightline_prospect_scans, sightline_prospects CASCADE;")
    conn.commit()
    with conn.cursor() as cur:
        cur.execute(open("schema.sql").read())
    conn.commit()
    print("  ok   schema applies cleanly")

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO sightline_prospects (domain, name, category, city, state, "
            "review_count, rating) VALUES ('ottersquad.com', 'Otter Squad Plumbing', "
            "'plumber', 'Allentown', 'PA', 64, 4.8) RETURNING id;")
        PROSPECT = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO sightline_prospects (domain, name, category, city, state, "
            "review_count, rating) VALUES ('beaverbrigade.com', 'Beaver Brigade', "
            "'plumber', 'Bethlehem', 'PA', 88, 4.6) RETURNING id;")
        OTHER = cur.fetchone()[0]

        # Real scan rows -- outreach_events references them.
        scans = {}
        for pid in (PROSPECT, OTHER):
            for _ in range(3):
                cur.execute(
                    "INSERT INTO sightline_prospect_scans "
                    "(prospect_id, aeo_score, gate_status, gate_rule) "
                    "VALUES (%s, 30, 'QUALIFIED', 'TEST') RETURNING id;", (pid,))
                scans.setdefault(pid, []).append(cur.fetchone()[0])
    conn.commit()

P_SCAN_A, P_SCAN_B, P_SCAN_C = scans[PROSPECT]
O_SCAN_A, O_SCAN_B, O_SCAN_C = scans[OTHER]

# --------------------------------------------------------------------------
print("\nsingle-worker claiming")
with psycopg.connect(DSN) as conn:
    first = claim_send(conn, PROSPECT, P_SCAN_A, "email")
    check("first claim is granted", first.granted, True)

    second = claim_send(conn, PROSPECT, P_SCAN_A, "email")
    check("second claim is refused", second.granted, False)
    check("and says why", "already contacted" in second.reason, True)

    # Even a brand-new scan must not earn a second cold email.
    third = claim_send(conn, PROSPECT, P_SCAN_B, "email")
    check("a newer scan does not re-open the prospect", third.granted, False)

    check("a different prospect is unaffected",
          claim_send(conn, OTHER, O_SCAN_A, "email").granted, True)

# --------------------------------------------------------------------------
print("\nconcurrent workers racing for the same prospect")
with psycopg.connect(DSN) as conn, conn.cursor() as cur:
    cur.execute("TRUNCATE sightline_outreach_events;")
    conn.commit()


def race(_):
    with psycopg.connect(DSN) as c:
        return claim_send(c, PROSPECT, P_SCAN_C, "email", once_per_prospect=False).granted


with ThreadPoolExecutor(max_workers=8) as pool:
    granted = list(pool.map(race, range(8)))

check("exactly one of eight workers wins", sum(granted), 1)

# --------------------------------------------------------------------------
print("\noutcomes")
with psycopg.connect(DSN) as conn, conn.cursor() as cur:
    cur.execute("TRUNCATE sightline_outreach_events;")
    conn.commit()

with psycopg.connect(DSN) as conn:
    claim = claim_send(conn, PROSPECT, P_SCAN_A, "email")
    check("claim granted", claim.granted, True)
    check("claiming alone is not a send", daily_sent_count(conn), 0)

    mark_sent(conn, claim)
    check("marked sent", daily_sent_count(conn), 1)
    check("nothing stuck after a clean send", len(stuck_sends(conn, "0 seconds")), 0)

# a claim that dies mid-flight
with psycopg.connect(DSN) as conn:
    orphan = claim_send(conn, OTHER, O_SCAN_B, "email")
    check("second prospect claimed", orphan.granted, True)
    stuck = stuck_sends(conn, "0 seconds")
    check("unresolved claim is surfaced", len(stuck), 1)
    check("and names the prospect", stuck[0]["domain"], "beaverbrigade.com")
    check("stuck claims are not counted as sent", daily_sent_count(conn), 1)

    mark_failed(conn, orphan, RuntimeError("GHL 502"))
    check("recording the failure clears it from stuck",
          len(stuck_sends(conn, "0 seconds")), 0)
    check("a failed send is still not a send", daily_sent_count(conn), 1)

    retry = claim_send(conn, OTHER, O_SCAN_B, "email")
    check("a failed send does NOT free the prospect for a retry",
          retry.granted, False)

# --------------------------------------------------------------------------
print("\nsuppression")
with psycopg.connect(DSN) as conn:
    suppress(conn, OTHER, "replied: not interested")
    with conn.cursor() as cur:
        cur.execute("SELECT suppressed, suppressed_note FROM sightline_prospects "
                    "WHERE id = %s;", (OTHER,))
        row = cur.fetchone()
    check("prospect suppressed", row[0], True)
    check("reason recorded", row[1], "replied: not interested")

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM sightline_outreach_queue WHERE domain = %s;",
                    ("beaverbrigade.com",))
        check("suppressed prospects leave the outreach queue", cur.fetchone()[0], 0)

# --------------------------------------------------------------------------
print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
    sys.exit(1)
print("all database guard checks passed")
