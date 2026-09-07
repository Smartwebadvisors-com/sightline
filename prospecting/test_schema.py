"""
test_schema.py -- the Sightline schema reader, against rows shaped like the
live database. No database needed.

    python3 test_schema.py
"""

from __future__ import annotations

import sys

from aeo_gate import evaluate
from aeo_probe import ProbeResult, Prospect
from aeo_types import is_lead_finding
from sightline_schema import (CHECK_PILLARS, build_finding, build_report,
                              summarize_av)

FAILURES: list[str] = []


def check(label: str, got, want) -> None:
    if got == want:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}: got {got!r}, want {want!r}")
        FAILURES.append(label)


def finding(check_id, severity, deduction=0, item_key="", remediation="",
            observed="", evidence=None):
    return {"id": abs(hash((check_id, item_key))) % 100000, "check_id": check_id,
            "item_key": item_key, "severity": severity, "examined": "homepage",
            "observed": observed, "remediation": remediation,
            "evidence": evidence or {}, "deduction": deduction}


SCAN = {"id": 41, "url": "https://ottersquad.com", "domain": "ottersquad.com",
        "status": "complete", "error": None, "meta": {}}

# --------------------------------------------------------------------------
print("\nseverity handling")
p = build_finding(finding("structured_data", "pass"))
check("'pass' means passed", p.passed, True)

f = build_finding(finding("structured_data", "critical", deduction=12))
check("'critical' is a defect", f.passed, False)
check("severity mapped", f.severity, "critical")
check("deduction becomes the weight", f.weight, 12)

u = build_finding(finding("rank", "unavailable"))
check("'unavailable' is not passed", u.passed, False)
check("'unavailable' is not a real severity", u.severity, "info")

check("no deduction falls back to severity weight",
      build_finding(finding("pagespeed", "high")).weight, 6)

# --------------------------------------------------------------------------
print("\nfinding content")
r = build_finding(finding("ai_crawler", "critical", 15, item_key="gptbot",
                          remediation="Remove the GPTBot Disallow rule from robots.txt.",
                          observed="robots.txt line 4 disallows GPTBot"))
check("id namespaced by item", r.id, "ai_crawler:gptbot")
check("remediation becomes the report line", r.detail,
      "Remove the GPTBot Disallow rule from robots.txt.")
check("observed becomes evidence", r.evidence,
      "robots.txt line 4 disallows GPTBot")
check("pillar mapped from check_id (v2 dimension)", r.pillar, "ai_accessibility")
check("recognised as the lead finding", is_lead_finding(r.id), True)

check("detail falls back to observed when no remediation",
      build_finding(finding("llms_txt", "low", observed="No llms.txt served.")).detail,
      "No llms.txt served.")

print("\n  all seven live check_ids map to a pillar:")
for cid in ("ai_crawler", "pagespeed", "rank", "entity_consistency",
            "structured_data", "answer_first", "llms_txt"):
    check(f"    {cid}", cid in CHECK_PILLARS, True)

# --------------------------------------------------------------------------
print("\nscore from deductions")
rows = [
    finding("structured_data", "critical", 20),
    finding("ai_crawler", "high", 15),
    finding("pagespeed", "medium", 5),
    finding("llms_txt", "pass"),
    finding("rank", "pass"),
]
report, _ = build_report(SCAN, rows)
check("100 minus deductions", report.aeo_score, 60)
check("passes are not counted as gaps", len(report.failed), 3)
check("engine tagged", report.engine, "sightline")
check("scan id kept", report.external_scan_id, "41")
check("reachable when complete with findings", report.reachable, True)

perfect, _ = build_report(SCAN, [finding("llms_txt", "pass"),
                                 finding("rank", "pass")])
check("all passes scores 100", perfect.aeo_score, 100)

# A scan from before scoring existed: deductions are all zero, but the
# findings are real. It must not report a perfect score.
unscored, _ = build_report(SCAN, [finding("structured_data", "critical", 0),
                                  finding("ai_crawler", "high", 0)])
check("no deductions falls back to severity weights", unscored.aeo_score, 84)

# --------------------------------------------------------------------------
print("\nunavailable checks degrade the audit")
mostly_unavailable, _ = build_report(SCAN, [
    finding("rank", "unavailable"),
    finding("pagespeed", "unavailable"),
    finding("entity_consistency", "unavailable"),
    finding("structured_data", "critical", 10),
    finding("llms_txt", "pass"),
])
check("degradation recorded",
      any(e.startswith("sightline_degraded") for e in mostly_unavailable.errors), True)

healthy, _ = build_report(SCAN, [
    finding("rank", "unavailable"),
    finding("structured_data", "critical", 10),
    finding("ai_crawler", "high", 8),
    finding("llms_txt", "pass"),
    finding("pagespeed", "pass"),
])
check("one unavailable check is fine",
      any(e.startswith("sightline_degraded") for e in healthy.errors), False)

# --------------------------------------------------------------------------
print("\nanswer-visibility observations")
empty = summarize_av([])
check("no rows is not usable", empty.usable, False)
check("no rows means zero rate", empty.mention_rate, 0.0)

av = summarize_av([
    {"model": "gpt-4o", "query": "best plumber allentown", "brand_mentioned": True},
    {"model": "gpt-4o", "query": "top rated plumber", "brand_mentioned": False},
    {"model": "sonar", "query": "best plumber allentown", "brand_mentioned": False},
    {"model": "sonar", "query": "who to hire", "brand_mentioned": False},
])
check("observations counted", av.observations, 4)
check("mentions counted", av.mentions, 1)
check("mention rate", round(av.mention_rate, 2), 0.25)
check("models deduped", av.models, ["gpt-4o", "sonar"])
check("three or more rows is usable", av.usable, True)
check("two rows is not enough",
      summarize_av([{"brand_mentioned": True}, {"brand_mentioned": False}]).usable,
      False)

# --------------------------------------------------------------------------
print("\nthrough the gate")
prospect = Prospect("Otter Squad Plumbing", "ottersquad.com", "plumber",
                    "Allentown", "PA", review_count=64, rating=4.8)
probe = ProbeResult(domain="ottersquad.com", name="Otter Squad Plumbing",
                    visibility_score=12, unbranded_asked=4, unbranded_cited=0,
                    unbranded_mentioned=0, branded_found=True,
                    top_competitors=[("beaverbrigade.com", 3)])

blocked_report, _ = build_report(SCAN, [
    finding("ai_crawler", "critical", 15, item_key="gptbot",
            remediation="Remove the GPTBot Disallow rule from robots.txt."),
    finding("structured_data", "high", 10),
    finding("pagespeed", "medium", 4),
])
v = evaluate(prospect, probe, blocked_report)
check("blocked crawler qualifies", v.status, "QUALIFIED")
check("via the Sightline check_id", v.rule, "AI_CRAWLERS_BLOCKED")
check("Sightline's own remediation is the reason",
      v.reasons[0], "Remove the GPTBot Disallow rule from robots.txt.")
check("crawler finding leads the report",
      blocked_report.headline_gaps(1)[0].id, "ai_crawler:gptbot")

# The regression that matters most: a WAF returning 403 to CCBot is not
# grounds for telling someone ChatGPT cannot see them. Marshall's Plumbing
# had exactly this shape -- every answer-engine crawler permitted, only
# CCBot and Meta-ExternalAgent failing -- and the rule fired anyway.
marshalls, _ = build_report(SCAN, [
    finding("ai_crawler", "critical", 30, item_key="crawler:CCBot",
            remediation="Check Cloudflare / edge / WAF rules for a bot-management policy.",
            observed="robots.txt permits CCBot but the origin returned HTTP 403."),
    finding("ai_crawler", "critical", 30, item_key="crawler:Meta-ExternalAgent",
            remediation="Check Cloudflare / edge / WAF rules.",
            observed="robots.txt permits Meta-ExternalAgent but origin returned 403."),
    finding("ai_crawler", "pass", item_key="crawler:GPTBot"),
    finding("ai_crawler", "pass", item_key="crawler:PerplexityBot"),
    finding("ai_crawler", "pass", item_key="crawler:Google-Extended"),
    finding("structured_data", "high", 10),
    finding("answer_first", "medium", 5),
])
visible = ProbeResult(domain="ottersquad.com", name="Marshall's",
                      visibility_score=100, unbranded_asked=4, unbranded_cited=4,
                      unbranded_mentioned=4, branded_found=True)
vm = evaluate(prospect, visible, marshalls)
check("training-only crawler does NOT fire the blocked rule",
      vm.rule == "AI_CRAWLERS_BLOCKED", False)
check("and the hook makes no ChatGPT claim",
      "ChatGPT" in (vm.hook or ""), False)

# But a genuinely blocked answer-engine crawler still fires, and names it.
really_blocked, _ = build_report(SCAN, [
    finding("ai_crawler", "critical", 30, item_key="crawler:GPTBot",
            remediation="Remove the GPTBot Disallow rule from robots.txt.",
            observed="robots.txt disallows GPTBot."),
    finding("ai_crawler", "pass", item_key="crawler:PerplexityBot"),
    finding("structured_data", "high", 10),
    finding("answer_first", "medium", 5),
])
vb = evaluate(prospect, probe, really_blocked)
check("a real block still qualifies", vb.rule, "AI_CRAWLERS_BLOCKED")
check("and the hook names the crawler", "GPTBot (ChatGPT)" in vb.hook, True)
check("without claiming engines it did not check",
      "Perplexity" in vb.hook, False)

# --------------------------------------------------------------------------
# recheck.py re-judges scans already in the database from their stored JSON.
# If reconstruction loses anything, a corrected verdict would be wrong in a
# new way, so the round-trip must be exact.
print("\nre-judging a stored scan")
from recheck import _probe, _prospect, _site  # noqa: E402

ROW = {"name": "Otter Squad Plumbing", "domain": "ottersquad.com",
       "category": "plumber", "city": "Allentown", "state": "PA",
       "review_count": 64, "rating": 4.8}

for label, report_obj, probe_obj in (
        ("blocked crawler", blocked_report, probe),
        ("training-only crawler", marshalls, visible),
        ("degraded audit", mostly_unavailable, probe)):
    direct = evaluate(prospect, probe_obj, report_obj)
    stored = evaluate(_prospect(ROW), _probe(probe_obj.to_dict(), ROW),
                      _site(report_obj.to_dict()))
    check(f"{label} re-judges identically",
          (stored.status, stored.rule, stored.priority, stored.hook),
          (direct.status, direct.rule, direct.priority, direct.hook))

check("a missing probe_raw reads as skipped, not failed",
      _probe(None, ROW).probed, False)

# A blocked crawler on a business the probe found visible. Both facts are real,
# so the hook must not claim the engines cannot see them -- Penn Integrity
# blocks PerplexityBot and Perplexity names them anyway.
print("\nblocked but already visible")
seen = ProbeResult(domain="ottersquad.com", name="Otter Squad Plumbing",
                   visibility_score=80, unbranded_asked=4, unbranded_cited=3,
                   unbranded_mentioned=1, branded_found=True)
vv = evaluate(prospect, seen, really_blocked)
check("still qualifies", vv.rule, "AI_CRAWLERS_BLOCKED")
check("still names the crawler", "GPTBot (ChatGPT)" in vv.hook, True)
check("does NOT claim the engines cannot see them",
      "cannot read the site" in vv.hook, False)
check("counts the queries that actually returned them",
      "3 of 4 buyer queries" in vv.hook, True)

# cited and mentioned overlap, so adding them can claim more queries than were
# asked. The count must never exceed the sample it came from.
overlap = ProbeResult(domain="ottersquad.com", name="Otter Squad Plumbing",
                      visibility_score=90, unbranded_asked=4, unbranded_cited=4,
                      unbranded_mentioned=4, branded_found=True)
check("overlapping counts do not inflate the claim",
      "4 of 4 buyer queries" in evaluate(prospect, overlap, really_blocked).hook,
      True)
check("and never exceed the sample",
      "5 of 4" in evaluate(prospect, overlap, really_blocked).hook, False)
check("an invisible business keeps the plain hook",
      "cannot read the site" in vb.hook, True)

# The trap in the first version of this rule: a composite score above the
# threshold is not evidence of a citation. Branded-only credit must not buy
# the "engines still name them" claim.
branded_only = ProbeResult(domain="ottersquad.com", name="Otter Squad Plumbing",
                           visibility_score=40, unbranded_asked=4,
                           unbranded_cited=0, unbranded_mentioned=0,
                           branded_found=True)
check("branded-only credit keeps the plain hook",
      "cannot read the site" in evaluate(prospect, branded_only, really_blocked).hook,
      True)

unreliable = ProbeResult(domain="ottersquad.com", name="Otter Squad Plumbing",
                         visibility_score=90, unbranded_asked=1, unbranded_cited=1,
                         unbranded_mentioned=0, branded_found=True,
                         trustworthy=False)
check("an unreliable probe never reaches the hook at all",
      evaluate(prospect, unreliable, really_blocked).rule, "PROBE_UNRELIABLE")

# --------------------------------------------------------------------------
# Three ways a crawler fetch can fail, and only two of them are a block.
print("\n403, 429 and a robots rule are three different things")
from aeo_types import (BLOCK_EDGE, BLOCK_RATE_LIMITED, BLOCK_ROBOTS,  # noqa: E402
                       block_kind, is_real_block)

_403 = build_finding(finding("ai_crawler", "critical", 30, item_key="crawler:GPTBot",
                             remediation="Check Cloudflare / edge / WAF rules.",
                             observed="robots.txt permits GPTBot but the origin "
                                      "returned HTTP 403 when fetched with its UA."))
_429 = build_finding(finding("ai_crawler", "critical", 30, item_key="crawler:GPTBot",
                             remediation="Check Cloudflare / edge / WAF rules.",
                             observed="robots.txt permits GPTBot but the origin "
                                      "returned HTTP 429 when fetched with its UA."))
_dis = build_finding(finding("ai_crawler", "critical", 30, item_key="crawler:GPTBot",
                             remediation="Remove the GPTBot Disallow rule.",
                             observed="robots.txt line 4 disallows GPTBot."))
check("403 is an edge block", block_kind(_403), BLOCK_EDGE)
check("429 is rate limiting", block_kind(_429), BLOCK_RATE_LIMITED)
check("no status means a robots.txt rule", block_kind(_dis), BLOCK_ROBOTS)
check("403 counts", is_real_block(_403), True)
check("robots rule counts", is_real_block(_dis), True)
check("429 does NOT count", is_real_block(_429), False)

# Dual Temp: GPTBot 429 and nothing else failing. Must not qualify as blocked.
throttled, _ = build_report(SCAN, [
    finding("ai_crawler", "critical", 30, item_key="crawler:GPTBot",
            remediation="Check Cloudflare / edge / WAF rules.",
            observed="robots.txt permits GPTBot but the origin returned HTTP 429 "
                     "when fetched with its UA."),
    finding("ai_crawler", "pass", item_key="crawler:PerplexityBot"),
    finding("structured_data", "high", 10, remediation="Add LocalBusiness schema."),
    finding("answer_first", "medium", 5, remediation="Lead with the answer."),
])
vt = evaluate(prospect, probe, throttled)
check("a 429 alone does not fire the blocked rule",
      vt.rule == "AI_CRAWLERS_BLOCKED", False)
check("and makes no blocking claim", "blocking" in (vt.hook or ""), False)

# Penn Integrity: robots.txt is clean, the origin 403s four answer engines.
edge, _ = build_report(SCAN, [
    finding("ai_crawler", "critical", 30, item_key="crawler:GPTBot",
            remediation="Check Cloudflare / edge / WAF rules.",
            observed="robots.txt permits GPTBot but the origin returned HTTP 403."),
    finding("ai_crawler", "critical", 30, item_key="crawler:PerplexityBot",
            remediation="Check Cloudflare / edge / WAF rules.",
            observed="robots.txt permits PerplexityBot but the origin returned HTTP 403."),
    finding("structured_data", "high", 10, remediation="Add LocalBusiness schema."),
])
ve = evaluate(prospect, probe, edge)
check("an edge block still qualifies", ve.rule, "AI_CRAWLERS_BLOCKED")
check("and does NOT blame robots.txt",
      "robots.txt blocks" in ve.hook, False)
check("it says robots.txt permits them",
      "robots.txt permits" in ve.hook, True)
check("and names the status code", "HTTP 403" in ve.hook, True)

# A genuine Disallow line should still say robots.txt, because there it is true.
vr = evaluate(prospect, probe, really_blocked)
check("a real robots rule is named as one",
      "robots.txt blocks" in vr.hook, True)

v2 = evaluate(prospect, probe, mostly_unavailable)
check("a degraded audit will not auto-send", v2.status, "REVIEW")
check("and says why", v2.rule, "AUDIT_INCOMPLETE")

# --------------------------------------------------------------------------
# The stored SEO score. Measured from DataForSEO, not derived from the
# findings above, so the reader must pass it through untouched -- and must
# keep "not measured" distinct from a measured zero.
# --------------------------------------------------------------------------
print()
print("stored SEO score:")

FINDINGS = [
    finding("ai_crawler", "pass"),
    finding("rank", "low", 2, item_key="backlinks"),
]


def scan_with(**kw):
    return {**SCAN, **kw}


r_scored, _ = build_report(
    scan_with(seo_score=43.1, seo_metrics={"covered_weight": 1.0,
                                           "version": "seo-v1"}),
    FINDINGS)
check("seo_score is read from the scan row", r_scored.seo_score, 43)
check("and exposed as its own pillar", r_scored.pillar_scores.get("seo"), 43)
check("a fully measured score raises no caveat",
      any("seo_partial" in e for e in r_scored.errors), False)

r_none, _ = build_report(scan_with(seo_score=None, seo_metrics={}), FINDINGS)
check("an unmeasured score stays None", r_none.seo_score, None)
check("and is NOT coerced to zero", r_none.pillar_scores.get("seo"), None)

r_zero, _ = build_report(
    scan_with(seo_score=0.0, seo_metrics={"covered_weight": 1.0}), FINDINGS)
check("a measured zero survives as zero", r_zero.seo_score, 0)
check("and is reported as a pillar", r_zero.pillar_scores.get("seo"), 0)

r_partial, _ = build_report(
    scan_with(seo_score=46.3, seo_metrics={"covered_weight": 0.55}), FINDINGS)
check("a partial score is flagged for the pitch",
      any("sightline_seo_partial" in e for e in r_partial.errors), True)

# psycopg returns jsonb as a dict, but a row that came through row_to_json
# or a cached payload can arrive as text.
r_text, _ = build_report(
    scan_with(seo_score=46.3, seo_metrics='{"covered_weight": 0.55}'),
    FINDINGS)
check("seo_metrics parses when handed back as JSON text",
      any("sightline_seo_partial" in e for e in r_text.errors), True)

check("the SEO score does not disturb the AEO score",
      r_scored.aeo_score, r_none.aeo_score)

# --------------------------------------------------------------------------
print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
    sys.exit(1)
print("all schema checks passed")
