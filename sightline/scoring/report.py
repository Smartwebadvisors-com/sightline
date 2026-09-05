"""Read-side scoring: turn stored per-finding deductions into a per-
dimension report. Kept separate from apply.py so the web dashboard can
call it on every render without re-inserting score rows."""
from __future__ import annotations

from .. import db
from . import weights as weights_mod


def compute_report(scan_id: int, weight_version_id: int) -> dict:
    """Returns:
      overall            straight mean of the scored dimensions (or None)
      dimensions         {dim_id: score_0_100_or_None}
      dim_data           {dim_id: {label, findings, available, earned,
                                    n_scored, n_unavailable, n_info}}
      unmapped_findings  findings whose check_id isn't in any dimension
                         (e.g. llms.txt — informational)
      coverage           {scored, unavailable, total}
    """
    wv = db.get_weight_version_by_id(weight_version_id)
    if wv is None:
        raise ValueError(f"weight version id {weight_version_id} not found")
    weights = wv["weights"]

    # Fall back to the current live mapping for older snapshots that
    # predate the DIMENSIONS layer.
    dimensions_cfg = weights.get("dimensions") or weights_mod.DIMENSIONS
    dimension_labels = weights.get("dimension_labels") or weights_mod.DIMENSION_LABELS
    dimension_order = (weights.get("dimension_order")
                       or list(dimensions_cfg.keys())
                       or weights_mod.DIMENSION_ORDER)
    deductions_cfg = weights.get("deductions", {})
    check_to_dim = {check: dim
                    for dim, checks in dimensions_cfg.items()
                    for check in checks}

    dim_data: dict[str, dict] = {
        dim: {"label": dimension_labels.get(dim, dim),
              "findings": [], "available": 0.0, "earned": 0.0,
              "n_scored": 0, "n_unavailable": 0, "n_info": 0}
        for dim in dimension_order if dim in dimensions_cfg
    }
    unmapped: list[dict] = []

    rows = db.scores_for_scan(scan_id, weight_version_id)

    for r in rows:
        dim = check_to_dim.get(r["check_id"])
        if dim is None:
            unmapped.append(r)
            continue
        entry = dim_data[dim]
        entry["findings"].append(r)
        sev = r["severity"]
        if sev == "unavailable":
            entry["n_unavailable"] += 1
        elif sev == "info":
            entry["n_info"] += 1
        elif sev in ("pass", "low", "medium", "high", "critical"):
            entry["n_scored"] += 1
            max_ded = float(deductions_cfg.get(r["check_id"], {})
                            .get("critical", 0) or 0)
            actual_ded = float(r["deduction"] or 0)
            entry["available"] += max_ded
            entry["earned"] += max(0.0, max_ded - actual_ded)

    dim_scores: dict[str, float | None] = {}
    for dim, entry in dim_data.items():
        if entry["available"] > 0:
            dim_scores[dim] = round(100.0 * entry["earned"] / entry["available"], 1)
        else:
            dim_scores[dim] = None

    scored = [s for s in dim_scores.values() if s is not None]
    overall = round(sum(scored) / len(scored), 1) if scored else None

    total_scored = sum(d["n_scored"] for d in dim_data.values())
    total_unavailable = sum(d["n_unavailable"] for d in dim_data.values())

    return {
        "scan_id": scan_id,
        "weight_version_id": weight_version_id,
        "overall": overall,
        "dimensions": dim_scores,
        "dim_data": dim_data,
        "unmapped_findings": unmapped,
        "coverage": {
            "scored": total_scored,
            "unavailable": total_unavailable,
            "total": total_scored + total_unavailable,
        },
    }


def composite_score(scan_id: int, weight_version_id: int) -> float:
    """The v1 model: 100 - sum(deductions), clamped [0, 100]. Kept only
    so `sightline compare` can show the number the old report displayed
    without re-running scoring under old semantics."""
    with db.conn() as c:
        r = c.execute(
            "SELECT COALESCE(SUM(deduction), 0) AS t "
            "FROM sightline_scores "
            "WHERE scan_id = %s AND weight_version_id = %s",
            (scan_id, weight_version_id),
        ).fetchone()
    total = float((r or {}).get("t") or 0)
    return round(max(0.0, min(100.0, 100.0 - total)), 1)
