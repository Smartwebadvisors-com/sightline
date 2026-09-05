"""
aeo_probe.py -- AI-visibility probe for the SWA lead engine.

Answers one question per prospect: when a real buyer asks an answer engine for
this kind of business in this town, does the prospect show up at all?

Deterministic in and out. The LLM is only a data source here -- it never decides
anything. Scoring, thresholds and qualification are plain arithmetic so the
sense-reason-act loop stays gated by rules.

Usage:
    from aeo_probe import Prospect, probe

    p = Prospect(name="Otter Squad Plumbing", domain="ottersquadplumbing.com",
                 category="plumber", city="Allentown", state="PA")
    result = probe(p)
    print(result.visibility_score, result.top_competitors)

Env:
    PERPLEXITY_API_KEY   required
    PPLX_API_URL         default https://api.perplexity.ai/v1/sonar
    PPLX_MODEL           default sonar
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable
from urllib.parse import urlparse

import requests

from resilience import (BudgetExhausted, CircuitOpen, DependencyError, Guard,
                        validate_probe_payload)

LOG = logging.getLogger("aeo.probe")

PPLX_URL = os.getenv("PPLX_API_URL", "https://api.perplexity.ai/v1/sonar")
PPLX_MODEL = os.getenv("PPLX_MODEL", "sonar")
REQUEST_TIMEOUT = 90
PAUSE_BETWEEN_QUERIES = 1.0

# Unbranded queries: what a buyer actually types. These carry the weight.
UNBRANDED_TEMPLATES = (
    "best {category} in {city}, {state}",
    "top rated {category} near {city} {state}",
    "who should I hire for {category} in {city} {state}",
    "most recommended {category} companies in {city} {state}",
)

# Branded control: if an answer engine can't describe them even when named,
# their entity footprint is missing entirely -- a different, worse problem.
BRANDED_TEMPLATE = (
    "Tell me about {name}, a {category} in {city}, {state}. "
    "Who are they, what do they offer, and how do customers rate them?"
)

# Cited directories are not competitors -- they are a separate finding
# ("answer engines cite aggregators instead of local businesses").
DIRECTORY_DOMAINS = {
    "yelp.com", "angi.com", "angieslist.com", "thumbtack.com", "homeadvisor.com",
    "bbb.org", "nextdoor.com", "tripadvisor.com", "mapquest.com", "yellowpages.com",
    "google.com", "facebook.com", "instagram.com", "reddit.com", "porch.com",
    "houzz.com", "expertise.com", "birdeye.com", "chamberofcommerce.com",
    "manta.com", "bark.com", "trustpilot.com", "indeed.com", "linkedin.com",
    "apple.com", "bing.com", "youtube.com", "tiktok.com", "wikipedia.org",
}

# Multi-part public suffixes worth handling without pulling in tldextract.
_MULTI_TLDS = {
    "co.uk", "org.uk", "ac.uk", "gov.uk", "co.nz", "co.za", "com.au", "net.au",
    "org.au", "com.br", "co.jp", "co.in", "com.mx", "co.il",
}

_LEGAL_SUFFIXES = {
    "llc", "l.l.c", "inc", "inc.", "incorporated", "corp", "corp.", "corporation",
    "co", "co.", "company", "ltd", "ltd.", "lp", "llp", "pllc", "pc", "pa",
}

# Words too generic to prove a match on their own.
_GENERIC_TOKENS = {
    "the", "and", "of", "for", "in", "at", "a", "an", "services", "service",
    "solutions", "group", "associates", "partners", "center", "centre", "shop",
    "store", "studio", "works", "brothers", "bros", "sons", "family", "local",
    "professional", "quality", "best", "top", "premier", "elite", "advanced",
} | _LEGAL_SUFFIXES


@dataclass
class Prospect:
    name: str
    domain: str
    category: str
    city: str
    state: str
    place_id: str | None = None
    review_count: int | None = None
    rating: float | None = None

    def registrable_domain(self) -> str:
        return registrable_domain(self.domain)


@dataclass
class QueryResult:
    query: str
    kind: str                      # "unbranded" | "branded"
    cited: bool                    # prospect's domain appears in the sources
    mentioned: bool                # prospect's name appears in the answer text
    citation_rank: int | None      # 1-based position of first matching source
    competitor_domains: list[str] = field(default_factory=list)
    directory_domains: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    answer_excerpt: str = ""
    error: str | None = None

    @property
    def credit(self) -> float:
        """How much presence this single query demonstrates: 0, 0.5 or 1.0."""
        if self.error:
            return 0.0
        if self.cited:
            return 1.0
        if self.mentioned:
            return 0.5
        return 0.0


@dataclass
class ProbeResult:
    domain: str
    name: str
    visibility_score: int          # 0-100, deterministic (see _score)
    unbranded_asked: int
    unbranded_cited: int
    unbranded_mentioned: int
    branded_found: bool
    probed: bool = True            # False = probe deliberately skipped, not failed
    trustworthy: bool = True       # False = too few queries succeeded to score
    top_competitors: list[tuple[str, int]] = field(default_factory=list)
    top_directories: list[tuple[str, int]] = field(default_factory=list)
    queries: list[QueryResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    probed_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["queries"] = [asdict(q) for q in self.queries]
        return d


# --------------------------------------------------------------------------
# matching helpers
# --------------------------------------------------------------------------

def registrable_domain(value: str) -> str:
    """'https://www.Joes-Plumbing.com/about' -> 'joes-plumbing.com'."""
    if not value:
        return ""
    value = value.strip().lower()
    if "//" not in value:
        value = "http://" + value
    host = (urlparse(value).hostname or "").strip(".")
    if not host:
        return ""
    if host.startswith("www."):
        host = host[4:]
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    if ".".join(parts[-2:]) in _MULTI_TLDS:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _normalize(text: str) -> str:
    text = text.lower()
    text = text.replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def name_tokens(name: str, category: str = "") -> list[str]:
    """Distinctive tokens from a business name.

    Generic words are stripped, and so are the words of the category being
    searched: in a page about plumbers, 'plumbing' proves nothing.
    """
    stop = set(_GENERIC_TOKENS) | set(_normalize(category).split())
    # 'plumber' in the category shouldn't leave 'plumbing' distinctive
    stop |= {t[:-3] for t in stop if t.endswith("ing") and len(t) > 5}
    stop |= {t + "ing" for t in list(stop)}
    tokens = [t for t in _normalize(name).split() if t not in stop]
    return [t for t in tokens if len(t) > 2]


def name_in_text(name: str, text: str, category: str = "") -> bool:
    """True if the answer text plausibly names this business.

    Two ways to match: the full normalized name appears as a substring, or at
    least two distinctive tokens all appear. The two-token floor is what stops
    a business named entirely out of category words ('The Plumbing Company')
    from matching every article that mentions plumbing -- a false positive
    there would inflate the visibility score and drop a real prospect.
    """
    norm_name, norm_text = _normalize(name), _normalize(text)
    if not norm_name or not norm_text:
        return False
    if norm_name in norm_text:
        return True
    tokens = name_tokens(name, category)
    if len(tokens) < 2:
        return False
    return all(re.search(rf"\b{re.escape(t)}\b", norm_text) for t in tokens)


def _extract_sources(payload: dict[str, Any]) -> list[str]:
    """Pull source URLs out of a Sonar response, in the order returned.

    Handles both `citations` (flat URL list) and `search_results`
    (objects with a `url` field); dedupes while preserving order.
    """
    urls: list[str] = []
    for item in payload.get("citations") or []:
        if isinstance(item, str):
            urls.append(item)
        elif isinstance(item, dict) and item.get("url"):
            urls.append(item["url"])
    for item in payload.get("search_results") or []:
        if isinstance(item, dict) and item.get("url"):
            urls.append(item["url"])
    seen, ordered = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            ordered.append(u)
    return ordered


# --------------------------------------------------------------------------
# API call
# --------------------------------------------------------------------------

def _ask(query: str, api_key: str, session: requests.Session) -> dict[str, Any]:
    resp = session.post(
        PPLX_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": PPLX_MODEL,
            "messages": [{"role": "user", "content": query}],
            "stream": False,
        },
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def evaluate_response(
    prospect: Prospect, query: str, kind: str, payload: dict[str, Any]
) -> QueryResult:
    """Pure function: response JSON in, structured verdict out. Unit-testable."""
    content = ""
    choices = payload.get("choices") or []
    if choices:
        content = ((choices[0] or {}).get("message") or {}).get("content") or ""

    sources = _extract_sources(payload)
    target = prospect.registrable_domain()

    citation_rank = None
    competitors: list[str] = []
    directories: list[str] = []
    for idx, url in enumerate(sources, start=1):
        d = registrable_domain(url)
        if not d:
            continue
        if d == target:
            if citation_rank is None:
                citation_rank = idx
        elif d in DIRECTORY_DOMAINS:
            if d not in directories:
                directories.append(d)
        elif d not in competitors:
            competitors.append(d)

    return QueryResult(
        query=query,
        kind=kind,
        cited=citation_rank is not None,
        mentioned=name_in_text(prospect.name, content, prospect.category),
        citation_rank=citation_rank,
        competitor_domains=competitors,
        directory_domains=directories,
        sources=sources,
        answer_excerpt=content[:600],
    )


def build_queries(prospect: Prospect) -> list[tuple[str, str]]:
    ctx = {
        "name": prospect.name,
        "category": prospect.category,
        "city": prospect.city,
        "state": prospect.state,
    }
    queries = [(t.format(**ctx), "unbranded") for t in UNBRANDED_TEMPLATES]
    queries.append((BRANDED_TEMPLATE.format(**ctx), "branded"))
    return queries


def _score(results: Iterable[QueryResult]) -> int:
    """0-100. Unbranded presence is 80% of it; the branded control is 20%.

    Cited in sources = full credit, named in the answer only = half credit.
    A business nobody's answer engine has heard of scores 0; one that is only
    findable when you already know its name tops out at 20.
    """
    unbranded = [r for r in results if r.kind == "unbranded" and not r.error]
    branded = [r for r in results if r.kind == "branded" and not r.error]

    unbranded_share = (
        sum(r.credit for r in unbranded) / len(unbranded) if unbranded else 0.0
    )
    branded_share = (
        sum(r.credit for r in branded) / len(branded) if branded else 0.0
    )
    return int(round(100 * (0.8 * unbranded_share + 0.2 * branded_share)))


def _tally(results: Iterable[QueryResult], attr: str) -> list[tuple[str, int]]:
    counts: dict[str, int] = {}
    for r in results:
        if r.kind != "unbranded":
            continue
        for d in getattr(r, attr):
            counts[d] = counts.get(d, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


# How many unbranded queries must succeed before a score means anything.
# Below this the result is marked untrustworthy and the gate refuses to act on
# it -- an absence we could not measure is not evidence of invisibility.
MIN_UNBRANDED_QUORUM = int(os.getenv("AEO_PROBE_QUORUM", "3"))


def probe(
    prospect: Prospect,
    api_key: str | None = None,
    session: requests.Session | None = None,
    guard: Guard | None = None,
) -> ProbeResult:
    api_key = api_key or os.getenv("PERPLEXITY_API_KEY")
    if not api_key:
        raise RuntimeError("PERPLEXITY_API_KEY is not set")

    guard = guard or Guard()
    own_session = session is None
    session = session or requests.Session()
    results: list[QueryResult] = []
    errors: list[str] = []

    try:
        for i, (query, kind) in enumerate(build_queries(prospect)):
            if i:
                time.sleep(PAUSE_BETWEEN_QUERIES)
            try:
                payload = guard.call(
                    "perplexity",
                    lambda q=query: _ask(q, api_key, session),
                    validate=validate_probe_payload,
                )
                results.append(evaluate_response(prospect, query, kind, payload))
            except BudgetExhausted as exc:
                # Never retried, never worked around: stop the whole probe and
                # let the caller see an untrustworthy result.
                LOG.error("budget exhausted mid-probe for %s: %s", prospect.domain, exc)
                errors.append(f"BudgetExhausted: {exc}")
                results.append(QueryResult(query=query, kind=kind, cited=False,
                                           mentioned=False, citation_rank=None,
                                           error=str(exc)))
                break
            except CircuitOpen as exc:
                LOG.warning("circuit open, skipping remaining queries for %s",
                            prospect.domain)
                errors.append(str(exc))
                results.append(QueryResult(query=query, kind=kind, cited=False,
                                           mentioned=False, citation_rank=None,
                                           error=str(exc)))
                break
            except DependencyError as exc:
                LOG.warning("probe query failed (%s): %s", query, exc.message)
                errors.append(exc.message)
                results.append(QueryResult(query=query, kind=kind, cited=False,
                                           mentioned=False, citation_rank=None,
                                           error=exc.message))
    finally:
        if own_session:
            session.close()

    unbranded = [r for r in results if r.kind == "unbranded"]
    branded = [r for r in results if r.kind == "branded"]
    answered = len([r for r in unbranded if not r.error])
    trustworthy = answered >= MIN_UNBRANDED_QUORUM

    if not trustworthy:
        LOG.warning("%s: only %d/%d unbranded queries answered (need %d) -- "
                    "result marked untrustworthy",
                    prospect.domain, answered, len(unbranded), MIN_UNBRANDED_QUORUM)

    return ProbeResult(
        domain=prospect.registrable_domain(),
        name=prospect.name,
        visibility_score=_score(results),
        unbranded_asked=answered,
        trustworthy=trustworthy,
        unbranded_cited=len([r for r in unbranded if r.cited]),
        unbranded_mentioned=len([r for r in unbranded if r.mentioned]),
        branded_found=any(r.cited or r.mentioned for r in branded),
        top_competitors=_tally(results, "competitor_domains")[:8],
        top_directories=_tally(results, "directory_domains")[:8],
        queries=results,
        errors=errors,
    )


if __name__ == "__main__":  # quick manual check
    import argparse

    ap = argparse.ArgumentParser(description="Probe one prospect's AI visibility")
    ap.add_argument("--name", required=True)
    ap.add_argument("--domain", required=True)
    ap.add_argument("--category", required=True)
    ap.add_argument("--city", required=True)
    ap.add_argument("--state", required=True)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO)
    out = probe(Prospect(args.name, args.domain, args.category, args.city, args.state))
    print(json.dumps(out.to_dict(), indent=2))
