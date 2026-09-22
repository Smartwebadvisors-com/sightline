"""Render every scan in the database. READ ONLY.

This is the companion to test_unavailable.py, which proves rule 10 against
fixtures. This one proves it against the rows we actually have — because
the rule is enforced in `__post_init__`, and a constructor that raises on
the READ path turns a stored row into a 500 on a report that rendered
fine yesterday. Fixtures cannot catch that; only the real rows can.

Specifically it would have caught: the 86 stored `severity='unavailable'`
rows backfilled with impact='low', effort_minutes=15, owner='swa'. Those
are legal values for a task, and rule 10 says an unmeasured finding has
none — so `from_row` must ignore them rather than pass them to a
constructor that rejects them.

This test writes nothing. It calls the render path (`render_scan`), the
export path (`export_scan`) and `failure_message`, all of which are pure
reads. It deliberately does NOT call `apply_to_scan`, which the web route
calls before rendering and which writes score rows.

It is not skippable when a database is configured: skipping is how a
render regression reaches production. It skips only when no database is
reachable at all, which is the CI case.
"""
from __future__ import annotations

import os
import pathlib
import unittest


# tests/__init__.py loads .env before any module imports sightline.config.
# Doing it here would be too late under `unittest discover`: an earlier
# test module imports config first, settings.db_url falls back to the
# password-less default, and this whole file skips itself.
from tests import load_dotenv               # noqa: E402

load_dotenv()

from sightline import db                     # noqa: E402
from sightline import unavailable            # noqa: E402
from sightline.render.html import render_scan       # noqa: E402
from sightline.render.json_export import export_scan  # noqa: E402
from sightline.scoring import weights as weights_mod  # noqa: E402
from tests.test_unavailable import (LEAK_MARKERS,     # noqa: E402
                                    assert_no_exception_text)


def _database_is_reachable() -> bool:
    try:
        with db.conn() as c:
            c.execute("SELECT 1")
        return True
    except Exception:
        return False


REACHABLE = _database_is_reachable()


@unittest.skipUnless(REACHABLE, "no database reachable (CI)")
class TestEveryStoredScanRenders(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with db.conn() as c:
            cls.scans = c.execute(
                "SELECT id, status FROM sightline_scans ORDER BY id"
            ).fetchall()
            wv = c.execute(
                "SELECT id FROM sightline_weight_versions WHERE version = %s",
                (weights_mod.WEIGHTS_VERSION,),
            ).fetchone()
        cls.wv_id = wv["id"] if wv else None

    def test_there_is_something_to_render(self):
        self.assertTrue(self.scans, "no scans found; this test proves nothing")
        self.assertIsNotNone(self.wv_id, "current weight version not registered")

    def test_every_complete_scan_renders_without_raising(self):
        failures = []
        for scan in self.scans:
            if scan["status"] != "complete":
                continue
            try:
                render_scan(scan["id"], self.wv_id)
            except Exception as exc:          # noqa: BLE001 — report them all
                failures.append(f"scan {scan['id']}: {type(exc).__name__}: {exc}")
        self.assertEqual(failures, [], "\n".join(failures))

    def test_every_complete_scan_exports_without_raising(self):
        failures = []
        for scan in self.scans:
            if scan["status"] != "complete":
                continue
            try:
                export_scan(scan["id"], self.wv_id)
            except Exception as exc:          # noqa: BLE001
                failures.append(f"scan {scan['id']}: {type(exc).__name__}: {exc}")
        self.assertEqual(failures, [], "\n".join(failures))

    def test_no_rendered_report_leaks_exception_text(self):
        """Rule 10 over real data: the stored `observed` on those PSI rows
        is Google's error payload, and the renderer prints it."""
        for scan in self.scans:
            if scan["status"] != "complete":
                continue
            html = render_scan(scan["id"], self.wv_id)
            assert_no_exception_text(self, html, f"scan {scan['id']} html")

    def test_no_failed_scan_shows_a_stored_exception(self):
        with db.conn() as c:
            failed = c.execute(
                "SELECT id, error FROM sightline_scans WHERE status = 'failed'"
            ).fetchall()
        for scan in failed:
            message = unavailable.failure_message(dict(scan))
            assert_no_exception_text(self, message, f"scan {scan['id']} banner")

    def test_every_stored_unmeasured_row_rebuilds_as_unmeasured(self):
        """Whatever a row stores, it comes back with no impact, no effort,
        no remediation and owner 'swa'."""
        from sightline import findings as F

        with db.conn() as c:
            rows = c.execute(
                "SELECT * FROM sightline_findings WHERE severity = 'unavailable'"
            ).fetchall()
        self.assertTrue(rows, "no unavailable rows; this test proves nothing")
        profile = F.ClientProfile(brand="Acme", category="business")
        for row in rows:
            f = F.from_row(dict(row), profile)
            self.assertEqual(f.outcome, F.UNMEASURED, row["id"])
            self.assertIsNone(f.impact, row["id"])
            self.assertIsNone(f.effort_minutes, row["id"])
            self.assertEqual(f.owner, "swa", row["id"])
            self.assertEqual(f.remediation, "", row["id"])
            assert_no_exception_text(self, f.technical.detail,
                                     f"finding {row['id']} detail")


if __name__ == "__main__":
    unittest.main()
