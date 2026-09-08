"""Machine-readable scan export. This is Cited's interface to Sightline.

Cited used to read Sightline by scraping the HTML report's CSS class names
(see tests/test_seo_score.py::test_does_not_emit_dimension_tile_classes),
which made every stylesheet change a potential outage on someone else's
dashboard. This replaces that.

Two things this format guarantees, by construction:

  * `plain` only. The technical block is not exported, so Cited cannot
    render acronyms and file formats at a client (COPY.md rules 1 and 2).
  * `impact`, `effort_minutes` and `owner` are three discrete keys. There is
    no fused phrase to render, so nobody downstream can invent one
    (COPY.md rule 4).

Scores carry `value`, `scale`, `band`, `display` and `peer` (null when we
cannot evidence a comparison), so a consumer never has to guess the
denominator (rule 5). `disclaimer` appears once, at the top level (rule 8).
"""
from __future__ import annotations

from .. import db
from .. import findings as findings_mod
from ..scoring.report import compute_report


def _score(value, peers=None) -> dict:
    sentence = findings_mod.peer_sentence(value, peers)
    return {
        "value": None if value is None else round(float(value), 1),
        "scale": findings_mod.SCALE,
        "band": findings_mod.band(value),
        "display": findings_mod.score_phrase(value),
        "peer": ({"n_domains": peers["n_domains"],
                  "median": round(peers["median"], 1),
                  "sentence": sentence} if sentence else None),
    }


def export_scan(scan_id: int, weight_version_id: int) -> dict:
    scan = db.scan(scan_id)
    if not scan:
        raise ValueError(f"scan {scan_id} not found")
    profile = findings_mod.profile_from_meta(scan.get("meta"))
    report = compute_report(scan_id, weight_version_id)

    rows = db.scores_for_scan(scan_id, weight_version_id)
    out_findings = []
    for row in rows:
        f = findings_mod.from_row(row, profile)
        out_findings.append({
            "check_id": f.check_id,
            "item_key": f.item_key,
            "outcome": f.outcome,
            "plain": {"title": f.plain.title, "why": f.plain.why,
                      "do": f.plain.do, "payoff": f.plain.payoff},
            "impact": f.impact,
            "effort_minutes": f.effort_minutes,
            "owner": f.owner,
        })

    seo_metrics = scan.get("seo_metrics") or {}
    return {
        "scan": {
            "id": scan_id,
            "url": scan["url"],
            "domain": scan["domain"],
            "scanned_at": scan["requested_at"].isoformat(),
            "brand": profile.brand,
            "category": profile.category,
        },
        "disclaimer": findings_mod.disclaimer(profile),
        "scores": {
            "overall": _score(
                report["overall"],
                db.peer_overall(weight_version_id, scan["domain"])),
            "dimensions": {
                dim: _score(score)
                for dim, score in report["dimensions"].items()
            },
            "search_presence": _score(
                scan.get("seo_score"), db.peer_seo_score(scan["domain"])),
        },
        "coverage": report["coverage"],
        "seo_components": seo_metrics.get("components") or {},
        "findings": out_findings,
    }
