"""
test_queue_pg.py -- the Postgres queue, against a real database.

The property that matters is the one Redis was there to provide: two workers
draining at once must get different rows. That cannot be tested with mocks --
SKIP LOCKED is a database behaviour.

    DATABASE_URL=postgres://... python3 test_queue_pg.py
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
    print("DATABASE_URL not set -- skipping")
    sys.exit(0)

import psycopg


# ---------------------------------------------------------------------------
# This suite DROPS AND RECREATES tables. That is fine on a scratch database and
# catastrophic on one holding a sourced queue or real prospect history, so it
# refuses to run unless the database looks empty.
#
# Override deliberately if you know what you are doing:
#     AEO_TEST_DESTRUCTIVE=1 DATABASE_URL=... python3 test_queue_pg.py
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
        print("  DATABASE_URL=postgres:///aeo_scratch python3 test_queue_pg.py")
        print("\nOr override if you genuinely want the data gone:")
        print("  AEO_TEST_DESTRUCTIVE=1 python3 test_queue_pg.py")
        sys.exit(0)


_refuse_if_real_data(DSN)

import queue_pg
from aeo_probe import Prospect

with psycopg.connect(DSN) as conn, conn.cursor() as cur:
    cur.execute("DROP TABLE IF EXISTS sightline_queue CASCADE;")
    conn.commit()
    sql = open("schema.sql").read()
    start = sql.index("CREATE TABLE IF NOT EXISTS sightline_queue")
    cur.execute(sql[start:])
    conn.commit()
print("  ok   queue table created")

# --------------------------------------------------------------------------
print("\nenqueue and dedupe")
prospects = [
    Prospect(f"Shop {i}", f"shop{i}.com", "plumber", "Allentown", "PA",
             review_count=50 + i, rating=4.8)
    for i in range(5)
]
check("all five queued", queue_pg.enqueue(prospects), 5)
check("depth reflects it", queue_pg.depth(), 5)
check("re-queueing the same domains adds nothing", queue_pg.enqueue(prospects), 0)
check("depth unchanged", queue_pg.depth(), 5)

# A weekly sourcing run returns overlapping results; only the new one lands.
mixed = prospects[:3] + [Prospect("New Shop", "newshop.com", "plumber",
                                  "Easton", "PA", review_count=90, rating=4.9)]
check("only the genuinely new one is added", queue_pg.enqueue(mixed), 1)
check("depth grew by one", queue_pg.depth(), 6)

# --------------------------------------------------------------------------
print("\ndraining")
first = queue_pg.drain(2)
check("claims the requested number", len(first), 2)
check("payload round-trips", first[0]["domain"], "shop0.com")
check("oldest first", [r["domain"] for r in first], ["shop0.com", "shop1.com"])
check("claimed rows leave the depth", queue_pg.depth(), 4)

rest = queue_pg.drain(50)
check("drains the remainder", len(rest), 4)
check("empty queue returns nothing", queue_pg.drain(10), [])
check("depth is zero", queue_pg.depth(), 0)

# --------------------------------------------------------------------------
print("\nconcurrent workers (what Redis was here for)")
with psycopg.connect(DSN) as conn, conn.cursor() as cur:
    cur.execute("TRUNCATE sightline_queue;")
    conn.commit()
queue_pg.enqueue([
    Prospect(f"Race {i}", f"race{i}.com", "plumber", "Allentown", "PA",
             review_count=60, rating=4.7)
    for i in range(40)
])


def worker(_):
    return [r["domain"] for r in queue_pg.drain(10)]


with ThreadPoolExecutor(max_workers=4) as pool:
    batches = list(pool.map(worker, range(4)))

claimed = [d for batch in batches for d in batch]
check("every item claimed exactly once", len(claimed), 40)
check("no item handed to two workers", len(set(claimed)), 40)
check("queue drained", queue_pg.depth(), 0)

# --------------------------------------------------------------------------
print("\nstranded claims")
with psycopg.connect(DSN) as conn, conn.cursor() as cur:
    cur.execute("TRUNCATE sightline_queue;")
    conn.commit()
queue_pg.enqueue([Prospect("Stranded", "stranded.com", "plumber",
                           "Allentown", "PA", review_count=70, rating=4.8)])
queue_pg.drain(1)
check("claimed, so not in depth", queue_pg.depth(), 0)
check("recent claims are left alone", queue_pg.release_claims(older_than="1 hour"), 0)

# Simulate a run that died: backdate the claim.
with psycopg.connect(DSN) as conn, conn.cursor() as cur:
    cur.execute("UPDATE sightline_queue SET claimed_at = now() - interval '2 days';")
    conn.commit()
check("a dead run's claim is released", queue_pg.release_claims(older_than="6 hours"), 1)
check("and the item is workable again", queue_pg.depth(), 1)

# --------------------------------------------------------------------------
print("\nstages are separate")
with psycopg.connect(DSN) as conn, conn.cursor() as cur:
    cur.execute("TRUNCATE sightline_queue;")
    conn.commit()
queue_pg.enqueue([{"domain": "a.com"}], stage=queue_pg.STAGE_PROSPECT)
queue_pg.enqueue([{"domain": "a.com"}], stage=queue_pg.STAGE_REVIEW)
check("same key can sit in both stages", queue_pg.depth(queue_pg.STAGE_PROSPECT), 1)
check("review stage has its own copy", queue_pg.depth(queue_pg.STAGE_REVIEW), 1)
queue_pg.drain(10, stage=queue_pg.STAGE_PROSPECT)
check("draining one stage leaves the other", queue_pg.depth(queue_pg.STAGE_REVIEW), 1)

# Leave nothing behind, so a scratch database can be reused and a mistaken
# run against a real one costs less.
with psycopg.connect(DSN) as conn, conn.cursor() as cur:
    cur.execute("TRUNCATE sightline_queue;")
    conn.commit()
check("cleaned up after itself", queue_pg.depth(queue_pg.STAGE_REVIEW), 0)

# --------------------------------------------------------------------------
print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
    sys.exit(1)
print("all queue checks passed")
