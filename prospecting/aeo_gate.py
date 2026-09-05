"""
aeo_gate.py -- deterministic qualification for the SWA lead engine.

No LLM call happens here and none ever should. This is the boundary between
"the machine gathered data" and "the machine acted", and it stays arithmetic so
outcomes are reproducible, auditable and tunable from one config object.

Verdicts:
    QUALIFIED  auto-advance to draft + queue (still human-approved for the
               first N sends -- see AUTO_SEND_AFTER in scan_prospect.py)
    REVIEW     worth a look, but a rule was ambiguous; goes to your queue
    SKIP       do not contact; reason recorded so the list can be tuned

The money rule is STRONG_REPUTATION_INVISIBLE: a business with real reviews and
real ratings that answer engines never surface. They have earned the demand and
are losing it to whoever shows up in the answer instead. That is the whole pitch.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from aeo_probe import ProbeResult, Prospect
from aeo_types import (ANSWER_ENGINE_CRAWLERS, BLOCK_EDGE, SiteReport,
                       block_kind, crawler_http_status, crawler_name,
                       is_lead_finding, is_real_block)


@dataclass
class GateConfig:
    min_reviews: int = 20          # below this, no demand to lose
    min_rating: float = 3.8        # below this, marketing is not their problem
    max_visibility: int = 35       # AI-visibility score at or under = invisible
    max_aeo_score: int = 70        # technical score at or under = fixable gaps
    strong_reviews: int = 40       # "strong reputation" threshold
    min_findings: int = 2          # need enough to fill a report
    require_website: bool = True


@dataclass
class Verdict:
    status: str                    # QUALIFIED | REVIEW | SKIP
    rule: str                      # which rule fired
    reasons: list[str] = field(default_factory=list)
    priority: int = 0              # 0-100, sort key for the outreach queue
    hook: str = ""                 # one sentence: why we are emailing them

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _priority(prospect: Prospect, probe: ProbeResult, site: SiteReport) -> int:
    """Higher = contact sooner.

    Weighted by how much demand they have (reviews, rating) against how badly
    they are losing it (invisibility, fixable technical gaps). When the probe
    was skipped, its 30 points move to the technical side rather than being
    scored as if the business were invisible.
    """
    reviews = prospect.review_count or 0
    demand = min(reviews / 100, 1.0) * 40                               # up to 40
    rating_bonus = max(0.0, ((prospect.rating or 0) - 3.5) / 1.5) * 10  # up to 10
    fixable = (100 - site.aeo_score) / 100

    if probe.probed:
        invisibility = (100 - probe.visibility_score) / 100 * 30        # up to 30
        fixability = fixable * 20                                       # up to 20
    else:
        invisibility = 0.0
        fixability = fixable * 50                                       # up to 50

    return int(round(min(100, demand + rating_bonus + invisibility + fixability)))


def evaluate(
    prospect: Prospect,
    probe: ProbeResult,
    site: SiteReport,
    config: GateConfig | None = None,
    suppressed_domains: set[str] | None = None,
) -> Verdict:
    cfg = config or GateConfig()
    suppressed = suppressed_domains or set()
    reviews = prospect.review_count or 0
    rating = prospect.rating or 0.0
    domain = prospect.registrable_domain()

    # ---- hard skips --------------------------------------------------------
    if domain in suppressed:
        return Verdict("SKIP", "SUPPRESSED",
                       ["already a client, in GHL, or previously opted out"])

    if cfg.require_website and not site.reachable:
        return Verdict("SKIP", "NO_REACHABLE_SITE",
                       [f"site unreachable ({site.status_code or 'no response'})"])

    if reviews < cfg.min_reviews:
        return Verdict("SKIP", "INSUFFICIENT_DEMAND",
                       [f"{reviews} reviews, below the {cfg.min_reviews} floor"])

    if rating and rating < cfg.min_rating:
        return Verdict("SKIP", "REPUTATION_PROBLEM",
                       [f"{rating} rating -- visibility is not their bottleneck"])

    # Only a *failed* probe blocks. A deliberately skipped one (--no-probe)
    # falls through to the technical-evidence rules below.
    #
    # The distinction being enforced here is the one that matters most in the
    # whole file: "we asked and they were absent" is evidence, "we could not
    # ask" is not. A partial outage must never read as invisibility.
    if probe.probed and not probe.trustworthy:
        return Verdict(
            "REVIEW", "PROBE_UNRELIABLE",
            [f"only {probe.unbranded_asked} unbranded quer(y/ies) answered -- "
             "too few to call this business invisible",
             *probe.errors[:3]],
        )

    if probe.probed and probe.unbranded_asked == 0:
        return Verdict("REVIEW", "PROBE_INCOMPLETE",
                       ["every unbranded probe query errored; re-run before contacting"])

    failed = site.failed
    if len(failed) < cfg.min_findings:
        return Verdict("REVIEW", "TOO_CLEAN",
                       [f"only {len(failed)} finding(s) -- not enough for a gap report"],
                       priority=_priority(prospect, probe, site))

    # A fallback audit is thinner than a Sightline one. Still actionable, but
    # it should not be the basis of an automated send without a look.
    if any(e.startswith("sightline_degraded") for e in site.errors):
        return Verdict(
            "REVIEW", "AUDIT_INCOMPLETE",
            [e for e in site.errors if e.startswith("sightline_degraded")][:1],
            priority=_priority(prospect, probe, site),
        )

    if any(e.startswith("sightline_fallback") for e in site.errors):
        return Verdict(
            "REVIEW", "AUDIT_ENGINE_DEGRADED",
            ["Sightline was unavailable; scored with the fallback audit",
             *[e for e in site.errors if e.startswith("sightline_fallback")][:1]],
            priority=_priority(prospect, probe, site),
        )

    # ---- qualifying rules, most conviction first ---------------------------
    priority = _priority(prospect, probe, site)

    # A crawler finding only counts if the crawler is actually being kept out.
    # A 429 is throttling -- possibly of our own scan -- and qualifying on one
    # would put a claim in the email that the prospect's own logs disprove.
    lead = [f for f in failed if is_lead_finding(f.id)]
    real = [f for f in lead if is_real_block(f)]
    blocked = real[0] if real else None

    if blocked:
        # Name the crawlers actually blocked. A generic "blocked from ChatGPT"
        # is a claim the prospect can disprove by opening robots.txt, so the
        # hook is built from the finding rather than asserted.
        names = sorted({crawler_name(f.id) for f in real
                        if crawler_name(f.id) in ANSWER_ENGINE_CRAWLERS})
        pretty = {"gptbot": "GPTBot (ChatGPT)",
                  "oai-searchbot": "OAI-SearchBot (ChatGPT search)",
                  "chatgpt-user": "ChatGPT-User",
                  "perplexitybot": "PerplexityBot",
                  "perplexity-user": "Perplexity-User",
                  "claudebot": "ClaudeBot",
                  "claude-user": "Claude-User",
                  "google-extended": "Google-Extended (AI Overviews)"}
        listed = ", ".join(pretty.get(n, n) for n in names) or "an answer-engine crawler"

        # A blocked crawler and a high visibility score are not a contradiction:
        # an engine can recommend a business from directory pages and reviews
        # without ever fetching their site. But "those engines cannot read the
        # site" is a claim the prospect disproves by asking ChatGPT about
        # themselves and seeing their own name. When we measured them as
        # visible, say the true and more useful thing instead.
        # The test is an observed citation, not the composite score. A score of
        # 40 can be mostly branded credit; "engines still name them" has to rest
        # on a buyer query that actually returned them, or it is the same kind
        # of unearned claim in the other direction.
        # A query can both cite and mention, so the two counts overlap by an
        # unknown amount and must not be added. max() is the largest number the
        # data supports; erring low here costs nothing, erring high is a claim
        # about their own search results that they can count themselves.
        surfaced = min(max(probe.unbranded_cited, probe.unbranded_mentioned),
                       probe.unbranded_asked)

        # Name the mechanism, because the two have different fixes and a
        # prospect who opens a clean robots.txt after being told about one
        # stops reading.
        if any(block_kind(f) == BLOCK_EDGE for f in real):
            codes = sorted({str(crawler_http_status(f)) for f in real
                            if block_kind(f) == BLOCK_EDGE})
            cause = (f"Their robots.txt permits {listed}, but the server itself "
                     f"answers HTTP {'/'.join(codes)} to those crawlers -- an edge "
                     "or bot-management rule nobody chose on purpose")
            fix = "It is a setting, not a code change."
        else:
            cause = f"Their robots.txt blocks {listed} by name"
            fix = "It is a one-line fix."

        if probe.probed and probe.trustworthy and surfaced > 0:
            hook = (f"{cause}. Answer engines still name them in {surfaced} of "
                    f"{probe.unbranded_asked} buyer queries -- off directory "
                    "listings and other people's pages, not their own site. They "
                    "do not control a word of what is being said about them.")
        else:
            hook = (f"{cause}, so those engines cannot read the site at all. {fix}")

        return Verdict(
            "QUALIFIED", "AI_CRAWLERS_BLOCKED",
            [blocked.detail],
            priority=min(100, priority + 10),
            hook=hook,
        )

    if not probe.probed:
        # Audit-only run: judge on technical evidence alone, and say so.
        if site.aeo_score <= cfg.max_aeo_score:
            return Verdict(
                "QUALIFIED", "TECHNICAL_GAPS_ONLY",
                [f"technical AEO {site.aeo_score}/100",
                 f"{len(failed)} findings", "AI-visibility probe not run"],
                priority=priority,
                hook=(f"{len(failed)} concrete AEO gaps on the site. Run the "
                      "visibility probe before sending to name what it is costing them."),
            )
        return Verdict("REVIEW", "TECHNICALLY_SOLID_UNPROBED",
                       [f"technical AEO {site.aeo_score}/100 with no probe data"],
                       priority=priority)

    if (reviews >= cfg.strong_reviews
            and probe.visibility_score <= cfg.max_visibility):
        competitors = [d for d, _ in probe.top_competitors[:3]]
        return Verdict(
            "QUALIFIED", "STRONG_REPUTATION_INVISIBLE",
            [f"{reviews} reviews at {rating}",
             f"AI-visibility score {probe.visibility_score}/100",
             f"cited in {probe.unbranded_cited} of {probe.unbranded_asked} buyer queries"],
            priority=priority,
            hook=(f"{reviews} reviews and a {rating} rating, and answer engines still "
                  f"don't name them"
                  + (f" -- they recommend {', '.join(competitors)} instead."
                     if competitors else ".")),
        )

    if (probe.visibility_score <= cfg.max_visibility
            and site.aeo_score <= cfg.max_aeo_score):
        return Verdict(
            "QUALIFIED", "INVISIBLE_AND_FIXABLE",
            [f"AI-visibility {probe.visibility_score}/100",
             f"technical AEO {site.aeo_score}/100",
             f"{len(failed)} findings"],
            priority=priority,
            hook=(f"Invisible in AI answers with {len(failed)} concrete technical gaps "
                  "behind it -- all of them fixable."),
        )

    if probe.visibility_score > cfg.max_visibility and site.aeo_score <= cfg.max_aeo_score:
        return Verdict(
            "REVIEW", "VISIBLE_BUT_WEAK_FOUNDATION",
            [f"already surfacing (visibility {probe.visibility_score}) but "
             f"technical AEO is {site.aeo_score}"],
            priority=max(0, priority - 20),
            hook="Showing up today, but on a foundation that will not hold as "
                 "answer engines get pickier.",
        )

    return Verdict(
        "SKIP", "NO_CLEAR_GAP",
        [f"visibility {probe.visibility_score}, AEO {site.aeo_score} -- "
         "nothing worth opening a conversation about"],
    )
