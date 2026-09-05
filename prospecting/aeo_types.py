"""
aeo_types.py -- shared result types.

Split out so the gate never has to care which engine produced the audit.
Sightline and the built-in fallback both hand back a SiteReport; everything
downstream is identical.
"""

from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}

# Findings that lead the gap report ahead of equally-severe ones. A blocked AI
# crawler is the most convincing thing you can open a cold email with -- but
# ONLY if it is a crawler that actually gates visibility in answer engines.
LEAD_FINDINGS = ("ai_crawler", "ai_crawlers_allowed")

# These crawlers decide whether a business can appear in a live AI answer.
# Blocking one is a real, current, sellable problem.
ANSWER_ENGINE_CRAWLERS = {
    "gptbot",            # ChatGPT browsing + training
    "oai-searchbot",     # ChatGPT search index
    "chatgpt-user",      # ChatGPT live retrieval
    "perplexitybot",
    "perplexity-user",
    "claudebot",
    "claude-user",
    "google-extended",   # Gemini / AI Overviews grounding
}

# These feed training corpora or products that are not answer engines. Blocking
# one is worth mentioning; it is NOT grounds for telling someone they are
# invisible to ChatGPT. Saying that when GPTBot is allowed is a false claim the
# prospect can disprove in ten seconds by opening their own robots.txt.
TRAINING_ONLY_CRAWLERS = {
    "ccbot",             # Common Crawl
    "meta-externalagent",
    "anthropic-ai",      # legacy, superseded by ClaudeBot
    "applebot-extended",
    "bytespider",
}


def crawler_name(finding_id: str) -> str:
    """'ai_crawler:crawler:GPTBot' -> 'gptbot'. '' when it isn't a crawler finding."""
    parts = (finding_id or "").split(":")
    return parts[-1].strip().lower() if len(parts) > 1 else ""


def is_lead_finding(finding_id: str) -> bool:
    """True only for a crawler that actually gates answer-engine visibility.

    The builtin check (`ai_crawlers_allowed`) already looks at answer-engine
    bots only, so it leads on its own. A Sightline `ai_crawler` finding names
    the specific crawler, and CCBot being 403'd by a WAF is not the same
    problem as GPTBot being disallowed.
    """
    head = (finding_id or "").split(":", 1)[0]
    if head == "ai_crawlers_allowed":
        return True
    if head != "ai_crawler":
        return False
    return crawler_name(finding_id) in ANSWER_ENGINE_CRAWLERS


_HTTP_STATUS = re.compile(r"returned HTTP (\d{3})")

# How a crawler came to fail. These are three different problems with three
# different fixes, and saying the wrong one costs the email:
#
#   robots        a Disallow line someone wrote. Fix: edit one text file.
#   edge          robots.txt permits it, but the origin refuses that UA (403).
#                 Nobody chose this; it is a WAF or bot-management default.
#   rate_limited  the origin answered 429. That is throttling, not refusal, and
#                 it may be nothing more than our own scan getting capped. It is
#                 NOT grounds for telling someone an engine cannot reach them.
BLOCK_ROBOTS = "robots"
BLOCK_EDGE = "edge"
BLOCK_RATE_LIMITED = "rate_limited"
BLOCK_NONE = "none"


def crawler_http_status(finding: "Finding") -> int | None:
    """The HTTP status Sightline recorded for the live crawler fetch, if any."""
    text = f"{finding.evidence or ''} {finding.detail or ''}"
    m = _HTTP_STATUS.search(text)
    return int(m.group(1)) if m else None


def block_kind(finding: "Finding") -> str:
    """Which of the three failure modes this finding actually is."""
    if finding.passed:
        return BLOCK_NONE
    status = crawler_http_status(finding)
    if status == 429:
        return BLOCK_RATE_LIMITED
    if status is not None and status >= 400:
        return BLOCK_EDGE
    if status is not None:
        return BLOCK_NONE          # a 2xx/3xx live fetch is not a failure
    return BLOCK_ROBOTS            # no live-fetch status: a robots.txt rule


def is_real_block(finding: "Finding") -> bool:
    """True only for a failure that actually keeps the crawler off the site."""
    return block_kind(finding) in (BLOCK_ROBOTS, BLOCK_EDGE)


@dataclass
class Finding:
    id: str
    pillar: str
    label: str
    passed: bool
    severity: str          # critical | high | medium | low | info
    weight: int            # points within its pillar
    detail: str            # report-ready sentence
    evidence: str = ""


@dataclass
class SiteReport:
    domain: str
    url: str
    reachable: bool
    status_code: int | None
    aeo_score: int                        # 0-100, whichever engine produced it
    pillar_scores: dict[str, int] = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)
    performance_scored: bool = False
    psi_performance: int | None = None
    psi_seo: int | None = None
    errors: list[str] = field(default_factory=list)
    scanned_at: float = field(default_factory=time.time)

    # Set by the Sightline adapter; the built-in engine leaves these alone.
    engine: str = "builtin"
    report_url: str | None = None
    geo_score: int | None = None
    seo_score: int | None = None
    external_scan_id: str | None = None

    @property
    def failed(self) -> list[Finding]:
        return sorted(
            (f for f in self.findings if not f.passed),
            key=lambda f: (
                SEVERITY_ORDER.get(f.severity, 9),
                0 if is_lead_finding(f.id) else 1,
                -f.weight,
            ),
        )

    def headline_gaps(self, n: int = 3) -> list[Finding]:
        """The findings worth naming in a first-touch email."""
        return self.failed[:n]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["findings"] = [asdict(f) for f in self.findings]
        return d
