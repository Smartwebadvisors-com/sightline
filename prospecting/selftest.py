"""
selftest.py -- no network, no API keys. Proves the logic that decides money.

    python3 selftest.py
"""

from __future__ import annotations

import sys

from bs4 import BeautifulSoup

from aeo_gate import GateConfig, evaluate
from aeo_probe import (Prospect, evaluate_response, name_in_text,
                       registrable_domain, _score)
from aeo_score import (blocks_agent, extract_jsonld,
                        node_types, parse_robots)
from aeo_types import Finding, SiteReport

FAILURES: list[str] = []


def check(label: str, got, want) -> None:
    if got == want:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}: got {got!r}, want {want!r}")
        FAILURES.append(label)


# --------------------------------------------------------------------------
print("\ndomain parsing")
check("strips scheme and www", registrable_domain("https://www.Joes-Plumbing.com/about"),
      "joes-plumbing.com")
check("bare domain", registrable_domain("ottersquad.com"), "ottersquad.com")
check("subdomain collapses", registrable_domain("http://blog.example.co.uk/x"),
      "example.co.uk")
check("empty is empty", registrable_domain(""), "")

# --------------------------------------------------------------------------
print("\nbusiness-name matching")
check("exact name in prose",
      name_in_text("Otter Squad Plumbing",
                   "For emergencies, Otter Squad Plumbing is well reviewed."), True)
check("legal suffix ignored",
      name_in_text("Otter Squad Plumbing LLC",
                   "Otter Squad Plumbing handles drain work."), True)
check("ampersand normalized",
      name_in_text("Smith & Sons Roofing", "Smith and Sons Roofing, est. 1994"), True)
check("category-only name does not match on category words",
      name_in_text("The Plumbing Company",
                   "Any plumbing company in the area will do.", "plumber"), False)
check("category-only name still matches its literal name",
      name_in_text("The Plumbing Company",
                   "We called The Plumbing Company on Tilghman St.", "plumber"), True)
check("different business rejected",
      name_in_text("Otter Squad Plumbing", "Beaver Brigade Plumbing is top rated."),
      False)

# --------------------------------------------------------------------------
print("\nprobe response evaluation")
prospect = Prospect(name="Otter Squad Plumbing", domain="ottersquad.com",
                    category="plumber", city="Allentown", state="PA",
                    review_count=64, rating=4.8)

cited_payload = {
    "choices": [{"message": {"content":
        "Top options include Beaver Brigade Plumbing and Otter Squad Plumbing."}}],
    "citations": ["https://www.yelp.com/biz/x", "https://ottersquad.com/services",
                  "https://beaverbrigade.com"],
}
res = evaluate_response(prospect, "best plumber in Allentown, PA", "unbranded",
                        cited_payload)
check("detects own citation", res.cited, True)
check("citation rank is 1-based", res.citation_rank, 2)
check("detects name in answer", res.mentioned, True)
check("competitors exclude directories", res.competitor_domains, ["beaverbrigade.com"])
check("directories tracked separately", res.directory_domains, ["yelp.com"])

absent_payload = {
    "choices": [{"message": {"content": "Try Beaver Brigade Plumbing or Dam Fine Drains."}}],
    "search_results": [{"url": "https://beaverbrigade.com"},
                       {"url": "https://damfinedrains.com"}],
}
res2 = evaluate_response(prospect, "best plumber in Allentown, PA", "unbranded",
                         absent_payload)
check("absent business not cited", res2.cited, False)
check("absent business not mentioned", res2.mentioned, False)
check("search_results parsed as sources", len(res2.sources), 2)

# --------------------------------------------------------------------------
print("\nvisibility scoring")
check("cited everywhere + branded = 100",
      _score([res, res, res, res,
              evaluate_response(prospect, "q", "branded", cited_payload)]), 100)
check("invisible everywhere = 0",
      _score([res2, res2, res2, res2,
              evaluate_response(prospect, "q", "branded", absent_payload)]), 0)
check("findable only when named = 20",
      _score([res2, res2, res2, res2,
              evaluate_response(prospect, "q", "branded", cited_payload)]), 20)

# --------------------------------------------------------------------------
print("\nrobots.txt parsing")
ROBOTS = """
# block the AI crawlers, allow Google
User-agent: GPTBot
Disallow: /

User-agent: PerplexityBot
User-agent: ClaudeBot
Disallow: /

User-agent: Googlebot
Disallow: /admin/

User-agent: *
Disallow: /wp-admin/

Sitemap: https://example.com/sitemap_index.xml
"""
groups = parse_robots(ROBOTS)
check("blocks GPTBot", blocks_agent(groups, "GPTBot"), True)
check("shared block applies to both agents", blocks_agent(groups, "ClaudeBot"), True)
check("Perplexity in shared block", blocks_agent(groups, "PerplexityBot"), True)
check("Googlebot not site-blocked", blocks_agent(groups, "Googlebot"), False)
check("unlisted agent falls back to *", blocks_agent(groups, "CCBot"), False)

WILDCARD_BLOCK = "User-agent: *\nDisallow: /\n"
check("wildcard block catches unlisted agent",
      blocks_agent(parse_robots(WILDCARD_BLOCK), "GPTBot"), True)

# --------------------------------------------------------------------------
print("\nJSON-LD extraction")
HTML = """
<html><head>
<script type="application/ld+json">
{"@context":"https://schema.org","@graph":[
  {"@type":"Plumber","name":"Otter Squad Plumbing",
   "telephone":"+1-610-555-0100","address":{"@type":"PostalAddress",
   "addressLocality":"Allentown"},"sameAs":["https://facebook.com/otter"]},
  {"@type":"FAQPage","mainEntity":[{"@type":"Question","name":"Do you offer emergency service?"}]}
]}
</script>
<script type="application/ld+json">{ this is not valid json }</script>
</head><body><h2>How fast can you get here?</h2></body></html>
"""
soup = BeautifulSoup(HTML, "html.parser")
nodes = extract_jsonld(soup)
check("malformed block skipped, graph flattened", len(nodes) >= 3, True)
types = set().union(*(node_types(n) for n in nodes))
check("finds Plumber type", "plumber" in types, True)
check("finds FAQPage type", "faqpage" in types, True)

# --------------------------------------------------------------------------
print("\nqualification gate")


def fake_site(aeo=40, reachable=True, blocked=False, n_findings=5) -> SiteReport:
    findings = [
        Finding(f"f{i}", "answerability", f"check {i}", False, "medium", 3,
                f"gap {i}")
        for i in range(n_findings)
    ]
    if blocked:
        findings.insert(0, Finding("ai_crawlers_allowed", "crawlability",
                                   "AI crawlers allowed", False, "critical", 10,
                                   "robots.txt blocks GPTBot, PerplexityBot"))
    return SiteReport(domain="ottersquad.com", url="https://ottersquad.com",
                      reachable=reachable, status_code=200 if reachable else None,
                      aeo_score=aeo, findings=findings)


def fake_probe(score: int, probed: bool = True, asked: int = 4):
    from aeo_probe import ProbeResult
    return ProbeResult(domain="ottersquad.com", name="Otter Squad Plumbing",
                       visibility_score=score, unbranded_asked=asked,
                       unbranded_cited=0, unbranded_mentioned=0,
                       branded_found=score > 0, probed=probed,
                       top_competitors=[("beaverbrigade.com", 3)])


v = evaluate(prospect, fake_probe(10), fake_site())
check("strong reviews + invisible qualifies", v.status, "QUALIFIED")
check("fires the money rule", v.rule, "STRONG_REPUTATION_INVISIBLE")
check("names a competitor in the hook", "beaverbrigade.com" in v.hook, True)
check("priority is sane", 0 < v.priority <= 100, True)

v = evaluate(prospect, fake_probe(10), fake_site(blocked=True))
check("blocked crawlers take precedence", v.rule, "AI_CRAWLERS_BLOCKED")

thin = Prospect(name="New Shop", domain="newshop.com", category="plumber",
                city="Allentown", state="PA", review_count=3, rating=5.0)
check("too few reviews is skipped",
      evaluate(thin, fake_probe(0), fake_site()).rule, "INSUFFICIENT_DEMAND")

bad = Prospect(name="Angry Yelp", domain="angry.com", category="plumber",
               city="Allentown", state="PA", review_count=90, rating=2.4)
check("bad rating is skipped",
      evaluate(bad, fake_probe(0), fake_site()).rule, "REPUTATION_PROBLEM")

check("unreachable site is skipped",
      evaluate(prospect, fake_probe(0), fake_site(reachable=False)).rule,
      "NO_REACHABLE_SITE")

check("clean site goes to review",
      evaluate(prospect, fake_probe(10), fake_site(n_findings=1)).rule, "TOO_CLEAN")

check("already visible and solid is skipped",
      evaluate(prospect, fake_probe(90), fake_site(aeo=95)).rule, "NO_CLEAR_GAP")

check("suppression wins over everything",
      evaluate(prospect, fake_probe(0), fake_site(),
               suppressed_domains={"ottersquad.com"}).status, "SKIP")

check("skipped probe still qualifies on technical evidence",
      evaluate(prospect, fake_probe(0, probed=False, asked=0), fake_site()).rule,
      "TECHNICAL_GAPS_ONLY")
check("failed probe blocks, unlike a skipped one",
      evaluate(prospect, fake_probe(0, probed=True, asked=0), fake_site()).rule,
      "PROBE_INCOMPLETE")
check("skipped probe does not fake invisibility points",
      evaluate(prospect, fake_probe(0, probed=False, asked=0), fake_site(aeo=95)).status,
      "REVIEW")

check("tighter config changes the verdict",
      evaluate(prospect, fake_probe(10), fake_site(),
               GateConfig(min_reviews=100)).rule, "INSUFFICIENT_DEMAND")

# --------------------------------------------------------------------------
print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
    sys.exit(1)
print("all checks passed")
