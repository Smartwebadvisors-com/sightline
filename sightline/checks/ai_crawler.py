"""AI crawler accessibility. Two signals per assistant crawler:
  1. robots.txt says the crawler may fetch '/'
  2. actually fetching '/' with that UA returns 2xx (not blocked at the edge)

The known assistant crawler UAs are documented by their respective vendors.
Blocking these is a legitimate business choice — Sightline reports the state,
not a value judgment on the choice, so the remediation copy is neutral.
"""
from __future__ import annotations

from ..fetch import http as http_mod
from ..fetch import robots as robots_mod
from .base import Finding, ScanContext

CHECK_ID = "ai_crawler"

# (display_name, robots_agent, request_ua_string)
# request_ua_string is what real crawlers send; edge WAFs match on it.
ASSISTANT_CRAWLERS = [
    ("OpenAI GPTBot",         "GPTBot",          "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; GPTBot/1.2; +https://openai.com/gptbot"),
    ("OpenAI SearchBot",      "OAI-SearchBot",   "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; OAI-SearchBot/1.0; +https://openai.com/searchbot"),
    ("Anthropic ClaudeBot",   "ClaudeBot",       "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; ClaudeBot/1.0; +claudebot@anthropic.com"),
    ("Perplexity",            "PerplexityBot",   "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; PerplexityBot/1.0; +https://perplexity.ai/perplexitybot"),
    ("Google-Extended",       "Google-Extended", ""),   # no direct fetch; robots-only signal
    ("Applebot-Extended",     "Applebot-Extended", ""), # robots-only signal
    ("Common Crawl (CCBot)",  "CCBot",           "CCBot/2.0 (https://commoncrawl.org/faq/)"),
    ("Meta-ExternalAgent",    "Meta-ExternalAgent", "meta-externalagent/1.1 (+https://developers.facebook.com/docs/sharing/webmasters/crawler)"),
]


def run(ctx: ScanContext) -> list[Finding]:
    findings: list[Finding] = []
    # RFC 9309: rules match against the request-target of the URL being
    # accessed, not the origin root. Passing '/' here (as we did before)
    # made every Disallow: /some-path/ invisible.
    target = robots_mod.request_target(ctx.url)

    # Reachability of robots.txt itself.
    if ctx.robots_status == 0:
        findings.append(Finding(
            check_id=CHECK_ID, item_key="robots_fetch",
            severity="medium",
            examined="robots.txt reachability at /robots.txt",
            observed="Could not fetch robots.txt (network error or timeout).",
            remediation="Ensure robots.txt is reachable so crawlers know your rules.",
            evidence={"robots_status": ctx.robots_status},
        ))
        robots = robots_mod.parse("")
    elif ctx.robots_status == 404:
        findings.append(Finding(
            check_id=CHECK_ID, item_key="robots_fetch",
            severity="low",
            examined="robots.txt at /robots.txt",
            observed="No robots.txt present. All crawlers are permitted by default.",
            remediation=(
                "Optional: publish a robots.txt to make your crawler policy explicit. "
                "Absence is not itself a problem."
            ),
            evidence={"robots_status": 404},
        ))
        robots = robots_mod.parse("")
    elif ctx.robots_status >= 400:
        findings.append(Finding(
            check_id=CHECK_ID, item_key="robots_fetch",
            severity="medium",
            examined="robots.txt at /robots.txt",
            observed=f"robots.txt returned HTTP {ctx.robots_status}.",
            remediation="Return 200 OK for /robots.txt or 404 if intentionally absent.",
            evidence={"robots_status": ctx.robots_status},
        ))
        robots = robots_mod.parse("")
    else:
        findings.append(Finding(
            check_id=CHECK_ID, item_key="robots_fetch",
            severity="pass",
            examined="robots.txt at /robots.txt",
            observed=f"robots.txt served (HTTP {ctx.robots_status}, "
                     f"{len(ctx.robots_text or '')} bytes).",
            evidence={"robots_status": ctx.robots_status},
        ))
        robots = robots_mod.parse(ctx.robots_text or "")

    # Per-crawler evaluation.
    for name, agent, ua in ASSISTANT_CRAWLERS:
        blocked_by_robots, rule = robots_mod.is_blocked(robots, agent, target)
        # Edge-blocking probe. Only for crawlers with a real UA; and only if
        # robots didn't already block, to save requests.
        edge_status: int | None = None
        edge_error: str | None = None
        if ua and not blocked_by_robots:
            result = http_mod.fetch_as_bot(ctx.url, ua)
            edge_status = result.status
            edge_error = result.error

        item_key = f"crawler:{agent}"
        evidence: dict = {
            "crawler": name,
            "robots_agent": agent,
            "robots_blocked": blocked_by_robots,
            "robots_rule": rule,
            "path_tested": target,
        }
        if edge_status is not None:
            evidence["edge_status"] = edge_status
        if edge_error:
            evidence["edge_error"] = edge_error

        if blocked_by_robots:
            findings.append(Finding(
                check_id=CHECK_ID, item_key=item_key,
                severity="critical",
                examined=f"{name} access to {ctx.url}",
                observed=(
                    f"robots.txt disallows {agent} (matching rule: "
                    f"'Disallow: {rule}')."
                ),
                remediation=(
                    "If you want your content to appear in this assistant's answers, "
                    "remove or narrow the Disallow rule. If the block is intentional, "
                    "keep it."
                ),
                evidence=evidence,
            ))
            continue

        if edge_status is not None and edge_status in (403, 401, 429):
            findings.append(Finding(
                check_id=CHECK_ID, item_key=item_key,
                severity="critical",
                examined=f"{name} live fetch of {ctx.url}",
                observed=(
                    f"robots.txt permits {agent} but the origin returned "
                    f"HTTP {edge_status} when fetched with its UA. Likely "
                    f"edge/WAF or bot-management block."
                ),
                remediation=(
                    "Check Cloudflare / edge / WAF rules for a bot-management "
                    "policy that blocks assistant crawlers. If you want the crawler in, "
                    "allowlist its UA (and IP range where the vendor publishes one)."
                ),
                evidence=evidence,
            ))
            continue

        if edge_status is not None and edge_status >= 500:
            findings.append(Finding(
                check_id=CHECK_ID, item_key=item_key,
                severity="medium",
                examined=f"{name} live fetch of {ctx.url}",
                observed=f"Origin returned HTTP {edge_status} for {agent}.",
                remediation="Investigate origin errors returned for this UA.",
                evidence=evidence,
            ))
            continue

        findings.append(Finding(
            check_id=CHECK_ID, item_key=item_key,
            severity="pass",
            examined=f"{name} access to {ctx.url}",
            observed=(
                "robots.txt permits this crawler."
                + (f" Live fetch returned HTTP {edge_status}." if edge_status else "")
            ),
            evidence=evidence,
        ))

    return findings
