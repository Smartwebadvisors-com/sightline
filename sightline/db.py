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
         f.examined, f.observed, f.remediation, Jsonb(f.evidence))
        for f in findings
    ]
    if not rows:
        return 0
    with conn() as c:
        c.cursor().executemany(
            """INSERT INTO sightline_findings
                 (scan_id, check_id, item_key, severity,
                  examined, observed, remediation, evidence)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (scan_id, check_id, item_key) DO UPDATE SET
                 severity    = EXCLUDED.severity,
                 examined    = EXCLUDED.examined,
                 observed    = EXCLUDED.observed,
                 remediation = EXCLUDED.remediation,
                 evidence    = EXCLUDED.evidence""",
            rows,
        )
        c.commit()
    return len(rows)


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
