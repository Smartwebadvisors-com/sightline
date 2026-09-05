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
    return report_mod.compute_report(scan_id, weight_version_id)
