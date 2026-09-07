"""
sightline_schema.py -- read Sightline's real tables.

Written against the live schema on the VPS, not a guess:

    sightline_scans           id, url, domain, requested_at, completed_at,
                              status, error, meta
    sightline_findings        id, scan_id, check_id, item_key, severity,
                              examined, observed, remediation, evidence,
                              created_at
    sightline_scores          id, scan_id, finding_id, weight_version_id,
                              deduction, computed_at
    sightline_av_observations id, domain, scan_id, model, query,
                              brand_mentioned, response_excerpt, sampled_at, meta

Three things about this schema drive the whole module:

1. `severity` includes 'pass' and 'unavailable'. A finding is not automatically
   a problem. 'pass' means the check succeeded, 'unavailable' means it could
   not run -- which is neither pass nor fail, and must never be counted as a
   defect we pitch someone on.

2. There is no stored score. Sightline computes it from per-finding deductions
   in `sightline_scores`, versioned by `weight_version_id`. So we sum the
   deductions of the latest weight version and subtract from 100 -- and every
   finding's sort weight becomes Sightline's own deduction rather than a
   number I invented.

3. `remediation` is already-written fix advice. It goes straight into the gap
   report; there is nothing for us to phrase.

4. `sightline_scans.seo_score` IS stored, unlike the AEO composite -- it is
   measured from DataForSEO metrics rather than derived from findings, so
   there is nothing to recompute here. Read it, don't rebuild it. NULL means
   the scan predates the column or DataForSEO could not be reached; that is
   not a zero.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable

from aeo_types import Finding, SiteReport

LOG = logging.getLogger("aeo.sightline.schema")

SCAN_STATUS_OK = "complete"

# Sightline's checks grouped into its own v2 scoring dimensions -- the check
# groupings and names mirror the `dimensions` map Sightline persists in
# sightline_weight_versions (weight version "v2", the live default), so
# pillar_scores speaks Sightline's vocabulary instead of a parallel one of ours.
#
# llms_txt is the exception: v2 demoted it to informational (zero deduction
# weight) and gives it no dimension of its own. It still yields a finding, so
# it is grouped under answer_first -- the dimension nearest to it -- to keep it
# visible without inventing a dimension Sightline doesn't have.
CHECK_PILLARS: dict[str, str] = {
    "ai_crawler":          "ai_accessibility",
    "structured_data":     "structured_data",
    "entity_consistency":  "entity_consistency",
    "answer_first":        "answer_first",
    "llms_txt":            "answer_first",
    "pagespeed":           "performance",
    "rank":                "authority",
}

# Sightline severity -> ours. 'pass' and 'unavailable' are handled separately
# because they are not defects.
SEVERITY_MAP = {
    "critical": "critical",
    "high": "high",
    "medium": "medium",
    "low": "low",
    "info": "info",
}
PASS_SEVERITY = "pass"
UNAVAILABLE_SEVERITY = "unavailable"

# Fallback weights when a finding has no scores row yet.
FALLBACK_WEIGHT = {"critical": 10, "high": 6, "medium": 3, "low": 2, "info": 1}

# If this fraction of a scan's checks could not run, the audit is too thin to
# pitch from. Fail closed: REVIEW rather than QUALIFIED.
MAX_UNAVAILABLE_RATIO = 0.34


LATEST_SCAN_SQL = """
SELECT id, url, domain, requested_at, completed_at, status, error, meta,
       seo_score, seo_metrics
FROM   sightline_scans
WHERE  (domain = %(domain)s OR url ILIKE %(like)s)
  AND  status = %(ok)s
ORDER  BY COALESCE(completed_at, requested_at) DESC
LIMIT  1;
"""

# One deduction per finding: the newest weight version wins, so re-scoring
# Sightline's model doesn't produce duplicate rows here.
FINDINGS_SQL = """
SELECT f.id, f.check_id, f.item_key, f.severity, f.examined, f.observed,
       f.remediation, f.evidence,
       COALESCE(s.deduction, 0) AS deduction
FROM   sightline_findings f
LEFT JOIN LATERAL (
    SELECT deduction
    FROM   sightline_scores sc
    WHERE  sc.finding_id = f.id
    ORDER  BY sc.weight_version_id DESC, sc.computed_at DESC
    LIMIT  1
) s ON TRUE
WHERE  f.scan_id = %(scan_id)s
ORDER  BY f.id;
"""

AV_SQL = """
SELECT model, query, brand_mentioned, response_excerpt, sampled_at
FROM   sightline_av_observations
WHERE  scan_id = %(scan_id)s OR domain = %(domain)s
ORDER  BY sampled_at DESC
LIMIT  50;
"""


@dataclass
class AVSummary:
    """What Sightline's own answer-visibility sampling knows about a domain."""
    observations: int = 0
    mentions: int = 0
    models: list[str] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)

    @property
    def mention_rate(self) -> float:
        return self.mentions / self.observations if self.observations else 0.0

    @property
    def usable(self) -> bool:
        # One observation is an anecdote. Three is a signal.
        return self.observations >= 3


def _clean(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def build_finding(row: dict[str, Any]) -> Finding:
    """One sightline_findings row -> one Finding.

    `remediation` is preferred for the detail because Sightline already wrote
    it as advice; `observed` is the fallback description of what was seen.
    """
    severity_raw = _clean(row.get("severity")).lower()
    check_id = _clean(row.get("check_id")) or "unknown_check"
    item_key = _clean(row.get("item_key"))

    passed = severity_raw == PASS_SEVERITY
    unavailable = severity_raw == UNAVAILABLE_SEVERITY

    if passed:
        severity = "info"
    elif unavailable:
        severity = "info"
    else:
        severity = SEVERITY_MAP.get(severity_raw, "medium")

    try:
        deduction = float(row.get("deduction") or 0)
    except (TypeError, ValueError):
        deduction = 0.0
    weight = int(round(deduction)) or FALLBACK_WEIGHT.get(severity, 3)

    detail = (_clean(row.get("remediation"))
              or _clean(row.get("observed"))
              or _clean(row.get("examined"))
              or f"{check_id} reported {severity_raw}")

    evidence = _clean(row.get("observed")) or _clean(row.get("evidence"))

    return Finding(
        id=f"{check_id}:{item_key}" if item_key else check_id,
        pillar=CHECK_PILLARS.get(check_id, "other"),
        label=check_id.replace("_", " "),
        # An unavailable check is NOT a passed check, but it is also not a
        # defect. It is carried as unpassed so it stays visible, and counted
        # separately so it cannot become the reason we email someone.
        passed=passed,
        severity=severity,
        weight=weight,
        detail=detail,
        evidence=evidence[:300],
    )


def _seo_metrics(scan: dict[str, Any]) -> dict[str, Any]:
    """sightline_scans.seo_metrics, tolerating a JSON string or NULL."""
    raw = scan.get("seo_metrics")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        import json
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except ValueError:
            return {}
    return {}


def read_seo_score(scan: dict[str, Any]) -> int | None:
    """0-100, or None when the scan has no measured SEO score.

    None is not a zero: it means the scan predates the column, DataForSEO
    was unreachable, or no endpoint was authorized. Never coerce it to 0 --
    that would read as 'this site has no organic presence', which is a
    claim we would be making without evidence.
    """
    value = scan.get("seo_score")
    if value is None:
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def _seo_covered_weight(scan: dict[str, Any]) -> float | None:
    value = _seo_metrics(scan).get("covered_weight")
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def summarize_av(rows: Iterable[dict[str, Any]]) -> AVSummary:
    summary = AVSummary()
    for row in rows:
        summary.observations += 1
        if row.get("brand_mentioned"):
            summary.mentions += 1
        model = _clean(row.get("model"))
        query = _clean(row.get("query"))
        if model and model not in summary.models:
            summary.models.append(model)
        if query and query not in summary.queries:
            summary.queries.append(query)
    return summary


def build_report(
    scan: dict[str, Any],
    finding_rows: list[dict[str, Any]],
    av_rows: list[dict[str, Any]] | None = None,
    domain: str = "",
) -> tuple[SiteReport, AVSummary]:
    """Assemble a SiteReport from Sightline rows. Pure -- no database access."""
    findings: list[Finding] = []
    unavailable = 0
    total_deduction = 0.0

    for row in finding_rows:
        severity_raw = _clean(row.get("severity")).lower()
        if severity_raw == UNAVAILABLE_SEVERITY:
            unavailable += 1
        finding = build_finding(row)
        findings.append(finding)
        if severity_raw not in (PASS_SEVERITY, UNAVAILABLE_SEVERITY):
            try:
                total_deduction += float(row.get("deduction") or 0)
            except (TypeError, ValueError):
                pass

    checked = len(finding_rows)
    score = int(round(max(0.0, min(100.0, 100.0 - total_deduction))))

    # No scores rows at all: fall back to severity weights so a scan that
    # predates scoring still ranks, rather than reporting a perfect 100.
    if total_deduction == 0 and any(
        _clean(r.get("severity")).lower() not in (PASS_SEVERITY, UNAVAILABLE_SEVERITY)
        for r in finding_rows
    ):
        fallback = sum(f.weight for f in findings if not f.passed)
        score = int(round(max(0.0, min(100.0, 100.0 - fallback))))
        LOG.debug("no deductions recorded; scored from severity weights")

    errors: list[str] = []
    if scan.get("error"):
        errors.append(f"sightline_scan_error: {_clean(scan['error'])}")

    if checked and unavailable / checked > MAX_UNAVAILABLE_RATIO:
        errors.append(
            f"sightline_degraded: {unavailable} of {checked} checks could not "
            "run -- audit too thin to pitch from"
        )

    pillar_scores: dict[str, int] = {}
    for pillar in set(CHECK_PILLARS.values()):
        items = [f for f in findings if f.pillar == pillar]
        scored = [f for f in items if f.severity != "info" or f.passed]
        if not items:
            continue
        passed = len([f for f in items if f.passed])
        pillar_scores[pillar] = int(round(100 * passed / len(items)))

    # Sightline's stored SEO score. Read as-is: it is measured from
    # DataForSEO metrics, not derived from the findings above, so it is
    # deliberately free to disagree with aeo_score.
    seo = read_seo_score(scan)
    if seo is not None:
        pillar_scores["seo"] = seo
        partial = _seo_covered_weight(scan)
        if partial is not None and partial < 1.0:
            errors.append(
                f"sightline_seo_partial: SEO score measured on "
                f"{partial:.0%} of its inputs -- quote it with that caveat"
            )

    av = summarize_av(av_rows or [])

    report = SiteReport(
        domain=_clean(scan.get("domain")) or domain,
        url=_clean(scan.get("url")) or domain,
        reachable=_clean(scan.get("status")) == SCAN_STATUS_OK and checked > 0,
        status_code=200 if _clean(scan.get("status")) == SCAN_STATUS_OK else None,
        aeo_score=score,
        pillar_scores=pillar_scores,
        findings=findings,
        errors=errors,
        engine="sightline",
        seo_score=seo,
        external_scan_id=str(scan.get("id")) if scan.get("id") is not None else None,
    )
    return report, av


def fetch(domain: str, dsn: str, max_age_days: int | None = 30
          ) -> tuple[SiteReport, AVSummary]:
    """Read the newest complete scan for a domain. Raises if there isn't one."""
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(dsn, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(LATEST_SCAN_SQL,
                    {"domain": domain, "like": f"%{domain}%", "ok": SCAN_STATUS_OK})
        scan = cur.fetchone()
        if not scan:
            raise LookupError(f"no complete sightline scan on record for {domain}")

        if max_age_days is not None:
            when = scan.get("completed_at") or scan.get("requested_at")
            if when is not None:
                from datetime import datetime, timezone
                now = datetime.now(when.tzinfo or timezone.utc)
                age = (now - when).days
                if age > max_age_days:
                    raise LookupError(
                        f"latest sightline scan for {domain} is {age} days old "
                        f"(limit {max_age_days}) -- re-scan before contacting"
                    )

        cur.execute(FINDINGS_SQL, {"scan_id": scan["id"]})
        finding_rows = cur.fetchall()

        try:
            cur.execute(AV_SQL, {"scan_id": scan["id"], "domain": domain})
            av_rows = cur.fetchall()
        except Exception as exc:
            LOG.debug("av observations unavailable: %s", exc)
            av_rows = []

    return build_report(scan, finding_rows, av_rows, domain)
