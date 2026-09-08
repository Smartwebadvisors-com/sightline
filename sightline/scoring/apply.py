"""Apply a weight version to a scan's findings. Writes one row per
finding to sightline_scores with the raw deduction from the weight table
(no per-check cap scaling). Dimension scores are computed at read time
from these deductions — see scoring/report.py."""
from __future__ import annotations

from .. import db
from . import report as report_mod


def apply_to_scan(scan_id: int, weights_snapshot: dict,
                  weight_version_id: int) -> dict:
    deductions_map = weights_snapshot.get("deductions", {})
    findings = db.findings_for_scan(scan_id)

    rows = [
        (scan_id, f["id"], weight_version_id,
         float(deductions_map.get(f["check_id"], {}).get(f["severity"], 0)))
        for f in findings
    ]
    db.write_scores(rows)
    report = report_mod.compute_report(scan_id, weight_version_id)
    # Cache the overall this call just computed. Peer comparison needs the
    # median across every other domain, and deriving it live cost one
    # compute_report() per peer domain per render. Written here because this
    # is the one place the number is produced -- see sql/005.
    db.write_scan_overall(scan_id, weight_version_id, report["overall"])
    return report
