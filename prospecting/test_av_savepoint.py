"""
test_av_savepoint.py -- prove one bad observation write can't abort the batch.

scan_prospect.persist() writes every scan on a single connection, in one
transaction, and only commits at the end. av_persist.write_observations shares
that connection's cursor. In Postgres a statement that errors poisons the whole
transaction: every later statement fails until someone rolls back. So without
the savepoint, a single failing observation insert would take down the two
scans that come after it AND the final commit -- the batch would be lost.

This test stands in a fake psycopg that models exactly that rule: a failed
statement sets conn.aborted, and while aborted every execute/executemany/commit
raises. The only thing that clears it is rolling back to a savepoint -- which is
what `with cur.connection.transaction():` does on error. We run a batch of three
prospects, rig the MIDDLE one's observation insert to fail, and assert all three
scans persisted and the transaction committed.

    python3 test_av_savepoint.py
"""

from __future__ import annotations

import sys
import types

FAILURES: list[str] = []


def check(label: str, got, want) -> None:
    if got == want:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}: got {got!r}, want {want!r}")
        FAILURES.append(label)


# --------------------------------------------------------------------------
# A fake psycopg that enforces Postgres's aborted-transaction rule.
# --------------------------------------------------------------------------

FAIL_DOMAIN = "middle.com"


class Jsonb:
    """Stand-in for psycopg.types.json.Jsonb -- a passthrough wrapper."""
    def __init__(self, value):
        self.value = value


class FakeSavepoint:
    """Models `with conn.transaction():` when already inside a transaction.

    Enter = SAVEPOINT. Exit with an exception = ROLLBACK TO SAVEPOINT (which
    clears the aborted state) then re-raise. Exit clean = RELEASE SAVEPOINT.
    This is the whole point of the test: rolling back to the savepoint is the
    only thing that makes the connection usable again after a failed statement.
    """
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        self.conn.savepoints += 1
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            self.conn.aborted = False          # ROLLBACK TO SAVEPOINT
            self.conn.rollbacks += 1
        return False                            # never swallow the exception


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.connection = conn                  # psycopg exposes cursor.connection
        self._last = None

    def _guard(self):
        if self.conn.aborted:
            raise RuntimeError(
                "current transaction is aborted, commands ignored until end "
                "of transaction block"
            )

    def execute(self, sql, params=None):
        self._guard()
        if "INSERT INTO sightline_prospects" in sql:
            self._last = (self.conn.new_id(),)
        elif "INSERT INTO sightline_prospect_scans" in sql:
            scan_id = self.conn.new_id()
            self.conn.scans.append(scan_id)
            self._last = (scan_id,)
        else:
            self._last = None

    def executemany(self, sql, rows):
        self._guard()
        # rows are (domain, scan_id, model, query, mentioned, excerpt, meta)
        if rows and rows[0][0] == FAIL_DOMAIN:
            self.conn.aborted = True            # the failed statement poisons the tx
            raise RuntimeError("simulated observation insert failure")
        self.conn.observations.extend(rows)

    def fetchone(self):
        return self._last

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConnection:
    def __init__(self):
        self.aborted = False
        self.committed = False
        self.scans: list[int] = []
        self.observations: list[tuple] = []
        self.savepoints = 0
        self.rollbacks = 0
        self._id = 0

    def new_id(self) -> int:
        self._id += 1
        return self._id

    def cursor(self):
        return FakeCursor(self)

    def transaction(self):
        return FakeSavepoint(self)

    def commit(self):
        if self.aborted:
            raise RuntimeError("cannot commit: transaction is aborted")
        self.committed = True

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def install_fake_psycopg(conn: FakeConnection) -> None:
    psycopg = types.ModuleType("psycopg")
    psycopg.connect = lambda dsn: conn
    types_mod = types.ModuleType("psycopg.types")
    json_mod = types.ModuleType("psycopg.types.json")
    json_mod.Jsonb = Jsonb
    types_mod.json = json_mod
    psycopg.types = types_mod
    sys.modules["psycopg"] = psycopg
    sys.modules["psycopg.types"] = types_mod
    sys.modules["psycopg.types.json"] = json_mod


# --------------------------------------------------------------------------
# Build three scans; the middle one's observations are rigged to fail.
# --------------------------------------------------------------------------

def build_batch():
    from aeo_gate import Verdict
    from aeo_probe import ProbeResult, Prospect, QueryResult
    from aeo_types import SiteReport
    from scan_prospect import ScanResult

    def make(domain: str) -> ScanResult:
        prospect = Prospect(name=f"Biz {domain}", domain=domain,
                            category="plumber", city="Allentown", state="PA",
                            review_count=40, rating=4.6)
        probe = ProbeResult(
            domain=domain, name=prospect.name, visibility_score=20,
            unbranded_asked=4, unbranded_cited=1, unbranded_mentioned=1,
            branded_found=True,
            queries=[
                QueryResult(query="best plumber in Allentown, PA",
                            kind="unbranded", cited=True, mentioned=True,
                            citation_rank=2, sources=["https://example.com"]),
                QueryResult(query="top rated plumber near Allentown PA",
                            kind="unbranded", cited=False, mentioned=False,
                            citation_rank=None),
            ],
        )
        site = SiteReport(domain=domain, url=f"https://{domain}", reachable=True,
                          status_code=200, aeo_score=45, engine="sightline")
        verdict = Verdict(status="QUALIFIED", rule="TEST", priority=50,
                          hook="test", reasons=["r"])
        return ScanResult(prospect=prospect, probe=probe, site=site,
                          verdict=verdict)

    # registrable_domain() normalizes, so the middle domain reaches
    # write_observations exactly as FAIL_DOMAIN.
    return [make("first.com"), make(FAIL_DOMAIN), make("last.com")]


print("\nbatch of 3, middle observation insert fails mid-transaction")

conn = FakeConnection()
install_fake_psycopg(conn)

from scan_prospect import persist  # imported after the fake is installed

scan_ids = persist(build_batch(), dsn="postgres://fake")

check("all three scans persisted", len(conn.scans), 3)
check("persist returned all three ids", len(scan_ids), 3)
check("returned ids match the scans written", scan_ids, conn.scans)
check("transaction committed despite the failure", conn.committed, True)
check("connection is not left aborted", conn.aborted, False)

# The savepoint rolled back exactly once -- for the middle prospect -- and the
# good prospects' observations survived while the failed one's did not.
check("savepoint rolled back exactly once", conn.rollbacks, 1)
obs_domains = sorted({row[0] for row in conn.observations})
check("only the two good prospects' observations landed",
      obs_domains, ["first.com", "last.com"])
check("the failed prospect wrote no observations",
      any(row[0] == FAIL_DOMAIN for row in conn.observations), False)
check("each good prospect wrote its two queries",
      len(conn.observations), 4)

# --------------------------------------------------------------------------
print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
    sys.exit(1)
print("all savepoint checks passed")
