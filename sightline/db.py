"""Thin psycopg wrapper. Mirrors trendsignal's db.py conventions.
No ORM — small schema, explicit queries."""
from __future__ import annotations

import json
import pathlib
from contextlib import contextmanager
from typing import Any, Iterable

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .config import settings
from .findings import MIN_PEER_DOMAINS


@contextmanager
def conn():
    with psycopg.connect(settings.db_url, row_factory=dict_row) as c:
        yield c


def init_schema(sql_dir: str = "sql") -> None:
    with conn() as c:
        for path in sorted(pathlib.Path(sql_dir).glob("*.sql")):
            c.execute(path.read_text())
        c.commit()


def create_scan(url: str, domain: str, meta: dict | None = None) -> int:
    with conn() as c:
        row = c.execute(
            """INSERT INTO sightline_scans (url, domain, meta)
               VALUES (%s, %s, %s) RETURNING id""",
            (url, domain, Jsonb(meta or {})),
        ).fetchone()
        c.commit()
    return row["id"]


def complete_scan(scan_id: int, status: str = "complete",
                  error: str | None = None) -> None:
    with conn() as c:
        c.execute(
            """UPDATE sightline_scans
               SET status = %s, error = %s, completed_at = NOW()
               WHERE id = %s""",
            (status, error, scan_id),
        )
        c.commit()


def set_seo_score(scan_id: int, score: float | None,
                  metrics: dict) -> None:
    """Persist the SEO score on the scan row. NULL score means 'not
    measured' — never conflate it with a measured zero."""
    with conn() as c:
        c.execute(
            """UPDATE sightline_scans
                  SET seo_score = %s, seo_metrics = %s
                WHERE id = %s""",
            (score, Jsonb(metrics), scan_id),
        )
        c.commit()


def set_seo_score_for_scans(scan_ids: Iterable[int], score: float | None,
                            metrics: dict) -> int:
    """Same, for every scan of one domain in a backfill. DataForSEO
    measures a domain, not a URL, so one call covers all of them."""
    ids = list(scan_ids)
    if not ids:
        return 0
    with conn() as c:
        c.execute(
            """UPDATE sightline_scans
                  SET seo_score = %s, seo_metrics = %s
                WHERE id = ANY(%s)""",
            (score, Jsonb(metrics), ids),
        )
        c.commit()
    return len(ids)


def domains_missing_seo_score() -> list[dict]:
    """[{domain, scan_ids}] for completed scans with no SEO score yet,
    grouped so the backfill buys one pair of API calls per domain."""
    with conn() as c:
        return c.execute(
            """SELECT LOWER(domain) AS domain,
                      ARRAY_AGG(id ORDER BY id) AS scan_ids
                 FROM sightline_scans
                WHERE status = 'complete' AND seo_score IS NULL
                  AND COALESCE(domain, '') <> ''
                GROUP BY LOWER(domain)
                ORDER BY LOWER(domain)"""
        ).fetchall()


def write_findings(scan_id: int, findings: Iterable[Any]) -> int:
    """Idempotent per (scan_id, check_id, item_key)."""
    rows = [
        (scan_id, f.check_id, f.item_key, f.severity,
         f.technical.title, f.technical.detail, f.remediation,
         Jsonb(f.evidence), f.impact, f.effort_minutes, f.owner)
        for f in findings
    ]
    if not rows:
        return 0
    with conn() as c:
        c.cursor().executemany(
            """INSERT INTO sightline_findings
                 (scan_id, check_id, item_key, severity,
                  examined, observed, remediation, evidence,
                  impact, effort_minutes, owner)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (scan_id, check_id, item_key) DO UPDATE SET
                 severity       = EXCLUDED.severity,
                 examined       = EXCLUDED.examined,
                 observed       = EXCLUDED.observed,
                 remediation    = EXCLUDED.remediation,
                 evidence       = EXCLUDED.evidence,
                 impact         = EXCLUDED.impact,
                 effort_minutes = EXCLUDED.effort_minutes,
                 owner          = EXCLUDED.owner""",
            rows,
        )
        c.commit()
    return len(rows)


def set_scan_profile(scan_id: int, profile: Any) -> None:
    """Persist the brand + category used for this scan's plain copy, so the
    report and the export re-derive the same sentences later."""
    with conn() as c:
        c.execute(
            """UPDATE sightline_scans
                  SET meta = meta || %s
                WHERE id = %s""",
            (Jsonb({"profile": {"brand": profile.brand,
                                "category": profile.category}}), scan_id),
        )
        c.commit()


def set_finding_task_fields(rows: Iterable[tuple]) -> int:
    """rows: (impact, effort_minutes, owner, finding_id). For the backfill."""
    rows = list(rows)
    if not rows:
        return 0
    with conn() as c:
        c.cursor().executemany(
            """UPDATE sightline_findings
                  SET impact = %s, effort_minutes = %s, owner = %s
                WHERE id = %s""",
            rows,
        )
        c.commit()
    return len(rows)


def scans_with_findings() -> list[dict]:
    """Every scan that has at least one finding. Used by the profile refresh,
    which must revisit scans whose task fields are already populated."""
    with conn() as c:
        return c.execute(
            """SELECT DISTINCT s.id, s.domain, s.meta
                 FROM sightline_scans s
                 JOIN sightline_findings f ON f.scan_id = s.id
                ORDER BY s.id"""
        ).fetchall()


def scans_needing_task_backfill() -> list[dict]:
    with conn() as c:
        return c.execute(
            """SELECT DISTINCT s.id, s.domain, s.meta
                 FROM sightline_scans s
                 JOIN sightline_findings f ON f.scan_id = s.id
                WHERE f.impact IS NULL OR f.owner IS NULL
                   OR f.effort_minutes IS NULL
                ORDER BY s.id"""
        ).fetchall()


def upsert_weight_version(version: str, description: str,
                          weights: dict) -> int:
    with conn() as c:
        row = c.execute(
            """INSERT INTO sightline_weight_versions (version, description, weights)
               VALUES (%s, %s, %s)
               ON CONFLICT (version) DO UPDATE SET
                 description = EXCLUDED.description,
                 weights     = EXCLUDED.weights
               RETURNING id""",
            (version, description, Jsonb(weights)),
        ).fetchone()
        c.commit()
    return row["id"]


def get_weight_version(version: str) -> dict | None:
    with conn() as c:
        return c.execute(
            "SELECT * FROM sightline_weight_versions WHERE version = %s",
            (version,),
        ).fetchone()


def get_weight_version_by_id(wv_id: int) -> dict | None:
    with conn() as c:
        return c.execute(
            "SELECT * FROM sightline_weight_versions WHERE id = %s",
            (wv_id,),
        ).fetchone()


def findings_for_scan(scan_id: int) -> list[dict]:
    with conn() as c:
        return c.execute(
            """SELECT * FROM sightline_findings
               WHERE scan_id = %s ORDER BY check_id, item_key""",
            (scan_id,),
        ).fetchall()


def write_scores(rows: Iterable[tuple]) -> int:
    """rows: (scan_id, finding_id, weight_version_id, deduction)."""
    rows = list(rows)
    if not rows:
        return 0
    with conn() as c:
        c.cursor().executemany(
            """INSERT INTO sightline_scores
                 (scan_id, finding_id, weight_version_id, deduction)
               VALUES (%s,%s,%s,%s)
               ON CONFLICT (finding_id, weight_version_id) DO UPDATE SET
                 deduction   = EXCLUDED.deduction,
                 computed_at = NOW()""",
            rows,
        )
        c.commit()
    return len(rows)


def scan(scan_id: int) -> dict | None:
    with conn() as c:
        return c.execute(
            "SELECT * FROM sightline_scans WHERE id = %s", (scan_id,)
        ).fetchone()


def scores_for_scan(scan_id: int, weight_version_id: int) -> list[dict]:
    """Findings joined with their deductions under a given weight version."""
    with conn() as c:
        return c.execute(
            """SELECT f.*, s.deduction
                 FROM sightline_findings f
                 LEFT JOIN sightline_scores s
                   ON s.finding_id = f.id
                  AND s.weight_version_id = %s
                WHERE f.scan_id = %s
                ORDER BY COALESCE(s.deduction, 0) DESC, f.check_id, f.item_key""",
            (weight_version_id, scan_id),
        ).fetchall()


def scores_are_current(scan_id: int, weight_version_id: int) -> bool:
    """True when every finding on this scan already has a deduction under
    this weight version.

    Two counts, no writes. apply_to_scan() rewrites one sightline_scores row
    per finding on every call and bumps computed_at, which is fine on a
    human-triggered render and wrong on an endpoint Cited polls. A read path
    calls this first and only re-applies when the answer is False: either the
    scan was never scored, or the weights version was bumped (a new
    weight_version_id has no rows yet), or findings were written after a
    partial apply.
    """
    with conn() as c:
        row = c.execute(
            """SELECT (SELECT COUNT(*) FROM sightline_findings
                        WHERE scan_id = %s) AS n_findings,
                      (SELECT COUNT(*) FROM sightline_scores
                        WHERE scan_id = %s
                          AND weight_version_id = %s) AS n_scores""",
            (scan_id, scan_id, weight_version_id),
        ).fetchone()
    return bool(row["n_findings"]) and row["n_scores"] == row["n_findings"]


def peer_overall(weight_version_id: int, exclude_domain: str,
                 limit: int = 200) -> dict | None:
    """Median overall score across the most recent complete scan of every
    OTHER domain. None when there are too few to compare against.

    This is the only peer data we have: our own scan history. It is not
    category-matched, so the report says exactly what it is — "the N other
    sites we have scanned" — and says nothing at all below
    findings.MIN_PEER_DOMAINS. See COPY.md rule 5.

    One compute_report() per peer domain, capped at `limit`, on a page that
    already runs one for the scan being rendered. If the scan table grows
    past a few hundred domains this wants a materialized column.
    """
    from .scoring.report import compute_report
    with conn() as c:
        rows = c.execute(
            """SELECT DISTINCT ON (LOWER(domain)) id
                 FROM sightline_scans
                WHERE status = 'complete'
                  AND LOWER(domain) <> LOWER(%s)
                ORDER BY LOWER(domain), requested_at DESC
                LIMIT %s""",
            (exclude_domain or "", limit),
        ).fetchall()
    scores: list[float] = []
    for r in rows:
        try:
            v = compute_report(r["id"], weight_version_id)["overall"]
        except Exception:
            continue
        if v is not None:
            scores.append(float(v))
    if len(scores) < MIN_PEER_DOMAINS:
        return None
    scores.sort()
    mid = len(scores) // 2
    median = (scores[mid] if len(scores) % 2
              else (scores[mid - 1] + scores[mid]) / 2)
    return {"n_domains": len(scores), "median": median}


def peer_seo_score(exclude_domain: str) -> dict | None:
    """Same, for the measured SEO score. Read straight off the scan rows."""
    with conn() as c:
        row = c.execute(
            """SELECT COUNT(*) AS n,
                      PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY seo_score)
                          AS median
                 FROM (SELECT DISTINCT ON (LOWER(domain)) seo_score
                         FROM sightline_scans
                        WHERE status = 'complete' AND seo_score IS NOT NULL
                          AND LOWER(domain) <> LOWER(%s)
                        ORDER BY LOWER(domain), requested_at DESC) latest""",
            (exclude_domain or "",),
        ).fetchone()
    n = (row or {}).get("n") or 0
    if n < MIN_PEER_DOMAINS:
        return None
    return {"n_domains": n, "median": float(row["median"])}


def delete_scan(scan_id: int) -> None:
    """FK cascades wipe findings + scores; av_observations.scan_id is set to
    NULL (the observation itself is not lost)."""
    with conn() as c:
        c.execute("DELETE FROM sightline_scans WHERE id = %s", (scan_id,))
        c.commit()


def list_scans_grouped(weight_version_id: int | None) -> list[dict]:
    """[{domain, latest, scans: [{id, url, domain, requested_at, status,
    error, score}]}] grouped by lowercased domain, newest scan first within
    group, groups ordered by most-recent activity first.

    score is the dimension-averaged overall computed from stored per-
    finding deductions + the weight-version snapshot. None if the scan
    isn't complete or has no scored findings under this version."""
    from .scoring.report import compute_report
    with conn() as c:
        rows = c.execute(
            """SELECT s.id, s.url, LOWER(s.domain) AS domain,
                      s.requested_at, s.status, s.error
                 FROM sightline_scans s
                ORDER BY LOWER(s.domain), s.requested_at DESC"""
        ).fetchall()
    for r in rows:
        if r["status"] == "complete" and weight_version_id is not None:
            try:
                r["score"] = compute_report(r["id"], weight_version_id)["overall"]
            except Exception:
                r["score"] = None
        else:
            r["score"] = None
    groups: dict[str, dict] = {}
    for r in rows:
        key = r["domain"] or ""
        g = groups.setdefault(key, {"domain": key, "scans": [],
                                    "latest": r["requested_at"]})
        g["scans"].append(r)
        if r["requested_at"] > g["latest"]:
            g["latest"] = r["requested_at"]
    return sorted(groups.values(), key=lambda g: g["latest"], reverse=True)


def scan_history(domain: str, limit: int = 20) -> list[dict]:
    with conn() as c:
        return c.execute(
            """SELECT id, url, requested_at, completed_at, status
                 FROM sightline_scans
                WHERE domain = %s
                ORDER BY requested_at DESC LIMIT %s""",
            (domain, limit),
        ).fetchall()


def write_av_observation(domain: str, scan_id: int | None, model: str,
                         query: str, brand_mentioned: bool,
                         response_excerpt: str, meta: dict) -> int:
    with conn() as c:
        row = c.execute(
            """INSERT INTO sightline_av_observations
                 (domain, scan_id, model, query, brand_mentioned,
                  response_excerpt, meta)
               VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
            (domain, scan_id, model, query, brand_mentioned,
             response_excerpt, Jsonb(meta)),
        ).fetchone()
        c.commit()
    return row["id"]


def av_summary(domain: str, window_days: int = 30) -> dict:
    with conn() as c:
        row = c.execute(
            """SELECT COUNT(*) AS n,
                      SUM(CASE WHEN brand_mentioned THEN 1 ELSE 0 END) AS mentions,
                      MIN(sampled_at) AS first_at,
                      MAX(sampled_at) AS last_at
                 FROM sightline_av_observations
                WHERE domain = %s
                  AND sampled_at >= NOW() - make_interval(days => %s)""",
            (domain, window_days),
        ).fetchone()
    n = row["n"] or 0
    m = row["mentions"] or 0
    return {
        "n_samples": n,
        "mentions": m,
        "visibility_rate": (m / n) if n else None,
        "window_days": window_days,
        "first_at": row["first_at"],
        "last_at": row["last_at"],
    }
