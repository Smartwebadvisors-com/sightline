"""
test_sightline.py -- adapter tests. No network, no Sightline, no database.

The point of these: prove the adapter survives Sightline's output being shaped
differently than assumed, because the exact shape is still unconfirmed. Every
case here is a plausible way a scored-audit tool emits JSON.

    python3 test_sightline.py
"""

from __future__ import annotations

import sys

from sightline_adapter import (SightlineError, _first_json_object,
                               normalize, normalize_finding)

FAILURES: list[str] = []


def check(label: str, got, want) -> None:
    if got == want:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}: got {got!r}, want {want!r}")
        FAILURES.append(label)


# --------------------------------------------------------------------------
print("\nflat shape (scores at top level, list of issues)")
FLAT = {
    "url": "https://ottersquad.com",
    "domain": "ottersquad.com",
    "aeo_score": 38, "geo_score": 45, "seo_score": 72,
    "status_code": 200,
    "findings": [
        {"id": "schema_missing", "title": "No LocalBusiness schema",
         "severity": "critical", "message": "No LocalBusiness markup found.",
         "category": "structured_data"},
        {"id": "thin_content", "title": "Thin homepage",
         "severity": "warning", "message": "Homepage has 180 words."},
    ],
}
r = normalize(FLAT, "ottersquad.com")
check("aeo score read", r.aeo_score, 38)
check("geo carried", r.geo_score, 45)
check("seo carried", r.seo_score, 72)
check("engine tagged", r.engine, "sightline")
check("findings normalized", len(r.findings), 2)
check("severity mapped: warning -> medium", r.findings[1].severity, "medium")
check("issues default to failed", all(not f.passed for f in r.findings), True)
check("pillar picked up from category", r.findings[0].pillar, "structured_data")
check("gate sees critical first", r.failed[0].id, "schema_missing")
check("reachable inferred", r.reachable, True)

# --------------------------------------------------------------------------
print("\nnested shape (scores under a key, checks as a dict)")
NESTED = {
    "target_url": "https://ottersquad.com",
    "scores": {"aeo": 0.41, "geo": 0.5, "seo": 0.8, "overall": 0.57},
    "checks": {
        "faq_schema": {"passed": False, "level": "major",
                       "description": "No FAQPage schema."},
        "sitemap": {"passed": True, "level": "minor",
                    "description": "Sitemap found."},
    },
    "id": "scan_9f21",
}
r2 = normalize(NESTED, "ottersquad.com")
check("0-1 float scaled to 0-100", r2.aeo_score, 41)
check("dotted lookup works", r2.geo_score, 50)
check("url alias resolved", r2.url, "https://ottersquad.com")
check("dict-of-checks flattened", len(r2.findings), 2)
check("dict key becomes finding id",
      sorted(f.id for f in r2.findings), ["faq_schema", "sitemap"])
check("explicit pass respected", len(r2.failed), 1)
check("major -> high", r2.failed[0].severity, "high")
check("external scan id kept", r2.external_scan_id, "scan_9f21")

# --------------------------------------------------------------------------
print("\ndegraded input")
r3 = normalize({"scores": {"geo": 60, "seo": 80}}, "ottersquad.com")
check("missing aeo falls back to layer mean", r3.aeo_score, 70)

r4 = normalize({"aeo_score": 0, "findings": [], "reachable": False,
                "status_code": 503}, "dead.com")
check("unreachable respected", r4.reachable, False)
check("status code kept", r4.status_code, 503)
check("no findings is not a crash", r4.failed, [])

r5 = normalize({"aeo_score": 55, "errors": "dataforseo timeout"}, "x.com")
check("string error becomes list", r5.errors, ["dataforseo timeout"])

r6 = normalize({"aeo_score": 150}, "x.com")
check("out-of-range score clamped", r6.aeo_score, 100)

try:
    normalize(["not", "an", "object"], "x.com")
    check("non-object payload rejected", "no error", "SightlineError")
except SightlineError:
    check("non-object payload rejected", "SightlineError", "SightlineError")

junk = normalize({"findings": [{"detail": "unlabelled issue"}]}, "x.com")
check("finding with no id gets one", junk.findings[0].id, "sightline_0")
check("finding with no severity defaults medium", junk.findings[0].severity, "medium")
check("label falls back to detail", junk.findings[0].label, "unlabelled issue")

# --------------------------------------------------------------------------
print("\nCLI output parsing")
check("clean JSON parsed",
      _first_json_object('{"aeo_score": 40}'), {"aeo_score": 40})
check("JSON after log lines parsed",
      _first_json_object('INFO starting\nINFO done\n{"aeo_score": 40}\n'),
      {"aeo_score": 40})
check("nested braces handled",
      _first_json_object('log\n{"a": {"b": 1}, "c": "}"}\ntrailing'),
      {"a": {"b": 1}, "c": "}"})
check("no JSON returns None", _first_json_object("nothing here"), None)
check("empty returns None", _first_json_object(""), None)
check("bare array returns None", _first_json_object("[1,2,3]"), None)

# --------------------------------------------------------------------------
print("\nend-to-end through the gate")
from aeo_gate import evaluate
from aeo_probe import ProbeResult, Prospect
from aeo_types import Finding

prospect = Prospect(name="Otter Squad Plumbing", domain="ottersquad.com",
                    category="plumber", city="Allentown", state="PA",
                    review_count=64, rating=4.8)
probe = ProbeResult(domain="ottersquad.com", name="Otter Squad Plumbing",
                    visibility_score=12, unbranded_asked=4, unbranded_cited=0,
                    unbranded_mentioned=0, branded_found=True,
                    top_competitors=[("beaverbrigade.com", 3)])

v = evaluate(prospect, probe, r)
check("sightline report drives the gate", v.status, "QUALIFIED")
check("money rule still fires on sightline data", v.rule,
      "STRONG_REPUTATION_INVISIBLE")

# supplemented crawler finding must still outrank everything
r.findings.append(Finding("ai_crawlers_allowed", "crawlability",
                          "AI crawlers allowed", False, "critical", 10,
                          "robots.txt blocks GPTBot, PerplexityBot"))
v2 = evaluate(prospect, probe, r)
check("supplement takes precedence", v2.rule, "AI_CRAWLERS_BLOCKED")
check("headline gaps are report-ready",
      r.headline_gaps(1)[0].detail.startswith("robots.txt blocks"), True)

# --------------------------------------------------------------------------
print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
    sys.exit(1)
print("all adapter checks passed")
