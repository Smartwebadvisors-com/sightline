"""
sightline_adapter.py -- use Sightline as the audit engine for prospecting.

Sightline already does the hard part: single URL in, scored AEO/GEO/SEO report
out, on the VPS, sharing Postgres with the trend tool. This module turns a
Sightline result into the same SiteReport the qualification gate already reads,
so nothing downstream changes.

Two backends, pick whichever matches how Sightline is actually invoked:

    from_cli(domain)        subprocess -> JSON on stdout
    from_postgres(domain)   read the latest sightline_ scan row for a domain

Neither one is guessing about Sightline's internals: both funnel through
`normalize()`, and every assumption about field names lives in FIELD_ALIASES
and FINDING_ALIASES at the top of this file. Run `discover.sh` on the VPS,
send me the output, and those two dicts are the only thing that changes.

Why this instead of the built-in aeo_score.py:
  * one audit engine, one definition of a score -- the number in a prospect's
    gap report is the same number a paying client sees
  * Sightline covers GEO and SEO too, which the prospecting audit never did
  * DataForSEO ranked-keywords and backlink data come along for free
  * the report Sightline renders IS the gap report; no second renderer needed

The one thing to check Sightline for: whether it flags AI crawlers blocked in
robots.txt. If not, `supplement=True` (the default) merges that check in --
it is the single most convincing finding in a cold email.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
import time
from typing import Any, Iterable

import requests

from aeo_score import check_ai_crawlers
from aeo_types import Finding, SiteReport, is_lead_finding
import sightline_schema

LOG = logging.getLogger("aeo.sightline")

SIGHTLINE_DIR = os.getenv("SIGHTLINE_DIR", "/root/sightline")
SIGHTLINE_CMD = os.getenv("SIGHTLINE_CMD", "python3 -m sightline scan --json {url}")
SIGHTLINE_TIMEOUT = int(os.getenv("SIGHTLINE_TIMEOUT", "300"))
SIGHTLINE_TABLE = os.getenv("SIGHTLINE_TABLE", "sightline_scans")
SIGHTLINE_REPORT_BASE = os.getenv("SIGHTLINE_REPORT_BASE", "")  # e.g. https://sightline.smartwebadvisors.com/r/

# ---------------------------------------------------------------------------
# THE MAPPING. This is the whole surface between Sightline and the loop.
# Each tuple is the candidate keys to look for, first match wins.
# ---------------------------------------------------------------------------

FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "aeo_score":   ("aeo_score", "aeo", "scores.aeo", "answer_engine_score"),
    "geo_score":   ("geo_score", "geo", "scores.geo", "generative_engine_score"),
    "seo_score":   ("seo_score", "seo", "scores.seo", "search_engine_score"),
    "overall":     ("overall_score", "total_score", "score", "scores.overall"),
    "url":         ("url", "final_url", "site_url", "target_url"),
    "domain":      ("domain", "host", "hostname"),
    "status_code": ("status_code", "http_status", "status"),
    "reachable":   ("reachable", "ok", "success", "is_reachable"),
    "report_url":  ("report_url", "report", "permalink", "public_url"),
    "scan_id":     ("id", "scan_id", "uuid", "run_id"),
    "findings":    ("findings", "issues", "checks", "results", "recommendations"),
    "errors":      ("errors", "error", "warnings"),
}

FINDING_ALIASES: dict[str, tuple[str, ...]] = {
    "id":       ("id", "key", "check", "code", "slug"),
    "label":    ("label", "title", "name", "check_name"),
    "passed":   ("passed", "ok", "pass", "success"),
    "severity": ("severity", "level", "priority", "impact"),
    "detail":   ("detail", "message", "description", "recommendation", "summary"),
    "evidence": ("evidence", "value", "observed", "data"),
    "pillar":   ("pillar", "category", "group", "layer", "section"),
    "weight":   ("weight", "points", "score_impact"),
}

# Sightline severities -> ours. Unknown values fall back to "medium".
SEVERITY_ALIASES = {
    "critical": "critical", "blocker": "critical", "fatal": "critical", "p0": "critical",
    "high": "high", "major": "high", "error": "high", "p1": "high",
    "medium": "medium", "moderate": "medium", "warn": "medium", "warning": "medium", "p2": "medium",
    "low": "low", "minor": "low", "notice": "low", "p3": "low",
    "info": "info", "informational": "info", "pass": "info",
}

DEFAULT_WEIGHTS = {"critical": 10, "high": 6, "medium": 3, "low": 2, "info": 1}


class SightlineError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# tolerant field access
# ---------------------------------------------------------------------------

def _dig(payload: Any, path: str) -> Any:
    """Look up 'scores.aeo' style dotted paths without exploding."""
    node = payload
    for part in path.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return None
    return node


def pick(payload: dict[str, Any], key: str, aliases: dict[str, tuple[str, ...]]) -> Any:
    for candidate in aliases.get(key, ()):
        value = _dig(payload, candidate)
        if value is not None:
            return value
    return None


def _as_score(value: Any) -> int | None:
    """Accept 0-100 ints, 0-1 floats, and numeric strings alike."""
    if value is None:
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if 0 <= num <= 1:
        num *= 100
    return int(round(max(0, min(100, num))))


def _as_bool(value: Any, default: bool | None = None) -> bool | None:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "yes", "y", "1", "ok", "pass", "passed"):
            return True
        if low in ("false", "no", "n", "0", "fail", "failed"):
            return False
    return default


# ---------------------------------------------------------------------------
# normalization
# ---------------------------------------------------------------------------

def normalize_finding(raw: dict[str, Any], index: int) -> Finding:
    severity_raw = pick(raw, "severity", FINDING_ALIASES)
    severity = SEVERITY_ALIASES.get(str(severity_raw).strip().lower(), "medium")

    # If Sightline doesn't say pass/fail, treat a listed issue as a failure --
    # audit tools generally emit only what's wrong.
    passed = _as_bool(pick(raw, "passed", FINDING_ALIASES), default=False)

    weight = pick(raw, "weight", FINDING_ALIASES)
    try:
        weight = int(weight)
    except (TypeError, ValueError):
        weight = DEFAULT_WEIGHTS.get(severity, 3)

    detail = pick(raw, "detail", FINDING_ALIASES) or ""
    label = pick(raw, "label", FINDING_ALIASES) or detail[:60] or f"finding {index}"
    ident = pick(raw, "id", FINDING_ALIASES) or f"sightline_{index}"
    pillar = pick(raw, "pillar", FINDING_ALIASES) or "sightline"

    return Finding(
        id=str(ident),
        pillar=str(pillar).lower(),
        label=str(label),
        passed=bool(passed),
        severity=severity,
        weight=weight,
        detail=str(detail) or str(label),
        evidence=str(pick(raw, "evidence", FINDING_ALIASES) or ""),
    )


def normalize(payload: dict[str, Any], domain: str) -> SiteReport:
    """Sightline JSON (however shaped) -> the SiteReport the gate reads."""
    if not isinstance(payload, dict):
        raise SightlineError(f"expected a JSON object, got {type(payload).__name__}")

    raw_findings = pick(payload, "findings", FIELD_ALIASES) or []
    if isinstance(raw_findings, dict):
        # dict-of-checks shape: {"schema_missing": {...}, ...}
        raw_findings = [
            {**v, "id": k} if isinstance(v, dict) else {"id": k, "detail": str(v)}
            for k, v in raw_findings.items()
        ]
    findings = [
        normalize_finding(f, i)
        for i, f in enumerate(raw_findings)
        if isinstance(f, dict)
    ]

    aeo = _as_score(pick(payload, "aeo_score", FIELD_ALIASES))
    geo = _as_score(pick(payload, "geo_score", FIELD_ALIASES))
    seo = _as_score(pick(payload, "seo_score", FIELD_ALIASES))
    overall = _as_score(pick(payload, "overall", FIELD_ALIASES))

    # The gate keys off aeo_score. Fall back to overall, then to the mean of
    # whatever layers came back, so a partial result still ranks sensibly.
    if aeo is None:
        layers = [s for s in (geo, seo) if s is not None]
        aeo = overall if overall is not None else (
            int(round(sum(layers) / len(layers))) if layers else 0
        )

    status_code = pick(payload, "status_code", FIELD_ALIASES)
    try:
        status_code = int(status_code)
    except (TypeError, ValueError):
        status_code = None

    reachable = _as_bool(pick(payload, "reachable", FIELD_ALIASES))
    if reachable is None:
        reachable = bool(findings) or aeo > 0 or (status_code or 0) < 400

    errors = pick(payload, "errors", FIELD_ALIASES) or []
    if isinstance(errors, str):
        errors = [errors]

    report_url = pick(payload, "report_url", FIELD_ALIASES)
    scan_id = pick(payload, "scan_id", FIELD_ALIASES)
    if not report_url and SIGHTLINE_REPORT_BASE and scan_id:
        report_url = SIGHTLINE_REPORT_BASE.rstrip("/") + "/" + str(scan_id)

    pillar_scores: dict[str, int] = {}
    for name, value in (("aeo", aeo), ("geo", geo), ("seo", seo)):
        if value is not None:
            pillar_scores[name] = value

    return SiteReport(
        domain=pick(payload, "domain", FIELD_ALIASES) or domain,
        url=pick(payload, "url", FIELD_ALIASES) or domain,
        reachable=bool(reachable),
        status_code=status_code,
        aeo_score=aeo,
        pillar_scores=pillar_scores,
        findings=findings,
        errors=[str(e) for e in errors],
        engine="sightline",
        report_url=report_url,
        geo_score=geo,
        seo_score=seo,
        external_scan_id=str(scan_id) if scan_id is not None else None,
        scanned_at=time.time(),
    )


def _merge_supplement(
    report: SiteReport, domain: str, session: requests.Session | None
) -> SiteReport:
    """Add the AI-crawler check if Sightline didn't already make that call."""
    if any(is_lead_finding(f.id) or "crawler" in f.id.lower()
           for f in report.findings):
        return report
    try:
        report.findings.append(check_ai_crawlers(domain, session))
    except Exception as exc:
        LOG.warning("AI-crawler supplement failed for %s: %s", domain, exc)
        report.errors.append(f"ai_crawler_check: {type(exc).__name__}: {exc}")
    return report


# ---------------------------------------------------------------------------
# backend: CLI
# ---------------------------------------------------------------------------

def from_cli(
    domain: str,
    supplement: bool = True,
    session: requests.Session | None = None,
) -> SiteReport:
    url = domain if domain.startswith(("http://", "https://")) else "https://" + domain
    cmd = SIGHTLINE_CMD.format(url=shlex.quote(url), domain=shlex.quote(domain))

    try:
        proc = subprocess.run(
            cmd, shell=True, cwd=SIGHTLINE_DIR, capture_output=True,
            text=True, timeout=SIGHTLINE_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise SightlineError(f"sightline timed out after {SIGHTLINE_TIMEOUT}s on {domain}")

    if proc.returncode != 0:
        raise SightlineError(
            f"sightline exited {proc.returncode}: {(proc.stderr or '').strip()[:500]}"
        )

    payload = _first_json_object(proc.stdout)
    if payload is None:
        raise SightlineError(
            f"no JSON found in sightline output: {(proc.stdout or '')[:300]!r}"
        )

    report = normalize(payload, domain)
    return _merge_supplement(report, domain, session) if supplement else report


def _first_json_object(text: str) -> dict[str, Any] | None:
    """Tolerate log lines around the JSON payload."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        parsed = json.loads(text[start:i + 1])
                        if isinstance(parsed, dict):
                            return parsed
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    return None


# ---------------------------------------------------------------------------
# backend: Postgres
# ---------------------------------------------------------------------------

SELECT_LATEST = """
SELECT row_to_json(t) FROM (
    SELECT * FROM {table}
    WHERE domain = %(domain)s OR url ILIKE %(like)s
    ORDER BY created_at DESC
    LIMIT 1
) t;
"""


def from_postgres(
    domain: str,
    dsn: str | None = None,
    max_age_days: int | None = 30,
    supplement: bool = False,
    session: requests.Session | None = None,
) -> SiteReport:
    """Read the most recent complete Sightline scan for a domain.

    Goes through sightline_schema, which joins scans + findings + scores the
    way the live database actually stores them. Use this when Sightline already
    runs on its own schedule -- prospecting then costs nothing extra.

    `supplement` defaults to False here: Sightline's own `ai_crawler` check
    already covers what the supplement was adding.
    """
    dsn = dsn or os.getenv("DATABASE_URL")
    if not dsn:
        raise SightlineError("DATABASE_URL is not set")

    try:
        report, av = sightline_schema.fetch(domain, dsn, max_age_days)
    except LookupError as exc:
        raise SightlineError(str(exc)) from exc
    except Exception as exc:
        raise SightlineError(f"sightline query failed: {exc}") from exc

    if av.usable:
        # Record what Sightline's own sampling already knows, so the probe can
        # skip domains it has covered rather than re-buying the answer.
        report.errors.append(
            f"av_observations: {av.mentions}/{av.observations} mentions "
            f"across {len(av.models)} model(s)"
        )

    return _merge_supplement(report, domain, session) if supplement else report


def av_summary(domain: str, dsn: str | None = None):
    """Sightline's answer-visibility rows for a domain, or an empty summary."""
    dsn = dsn or os.getenv("DATABASE_URL")
    if not dsn:
        return sightline_schema.AVSummary()
    try:
        _, av = sightline_schema.fetch(domain, dsn, max_age_days=None)
        return av
    except Exception:
        return sightline_schema.AVSummary()


# ---------------------------------------------------------------------------
# engine selection
# ---------------------------------------------------------------------------

def audit(
    domain: str,
    engine: str = "sightline-cli",
    session: requests.Session | None = None,
    supplement: bool = True,
) -> SiteReport:
    """Single entry point used by scan_prospect.py.

    engine: sightline-cli | sightline-db | builtin
    """
    if engine == "sightline-cli":
        return from_cli(domain, supplement=supplement, session=session)
    if engine == "sightline-db":
        # Sightline's own ai_crawler check makes the supplement redundant.
        return from_postgres(domain, supplement=False, session=session)
    if engine == "builtin":
        from aeo_score import score_site
        return score_site(domain, session=session)
    raise ValueError(f"unknown engine: {engine}")
