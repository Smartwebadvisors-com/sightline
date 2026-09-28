"""A second click must not start a second scan of the same address."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from sightline.scan_slot import slot_decision

NOW = datetime(2026, 9, 28, 16, 0, tzinfo=timezone.utc)


class TestScanSlot(unittest.TestCase):
    def test_a_scan_still_running_is_joined(self):
        row = {"status": "running", "requested_at": NOW - timedelta(minutes=2)}
        self.assertEqual(slot_decision(row, NOW), "reuse")

    def test_a_scan_that_just_finished_is_joined(self):
        row = {
            "status": "complete",
            "completed_at": NOW - timedelta(minutes=1),
        }
        self.assertEqual(slot_decision(row, NOW), "reuse")

    def test_a_running_scan_with_no_worker_is_not_joined(self):
        row = {"status": "running", "requested_at": NOW - timedelta(minutes=30)}
        self.assertEqual(slot_decision(row, NOW), "abandon")

    def test_an_older_finished_scan_starts_fresh(self):
        row = {
            "status": "complete",
            "completed_at": NOW - timedelta(hours=2),
        }
        self.assertEqual(slot_decision(row, NOW), "start")

    def test_a_failed_scan_starts_fresh(self):
        row = {"status": "failed", "requested_at": NOW}
        self.assertEqual(slot_decision(row, NOW), "start")

    def test_no_row_starts_fresh(self):
        self.assertEqual(slot_decision(None, NOW), "start")


if __name__ == "__main__":
    unittest.main()
