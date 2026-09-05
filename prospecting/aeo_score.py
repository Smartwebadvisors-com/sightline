"""
aeo_score.py -- technical AEO audit for a single prospect site.

Fetches the homepage, robots.txt, sitemap and (optionally) PageSpeed Insights,
then returns an itemized list of findings. Every finding carries a severity and
a plain-English `detail` written to be pasted straight into a gap report, so
the same run feeds both the qualification gate and the outreach copy.

Four pillars, weighted to 100:
    structured_data 30   can a machine tell what this business is?
    answerability   30   is the content shaped like answers to real questions?
    crawlability    25   are answer engines allowed in, and can they find pages?
    performance     15   PageSpeed mobile + Lighthouse SEO

Usage:
    from aeo_score import score_site
    report = score_site("ottersquadplumbing.com", psi_key=os.getenv("PSI_API_KEY"))

Env:
    PSI_API_KEY   optional; without it the performance pillar is skipped and
                  the remaining pillars are rescaled to 100.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree

import requests
from bs4 import BeautifulSoup

from aeo_types import Finding, SEVERITY_ORDER, SiteReport

LOG = logging.getLogger("aeo.score")

USER_AGENT = os.getenv(
    "AEO_USER_AGENT",
    "Mozilla/5.0 (compatible; SWA-AEO-Audit/1.0; +https://smartwebadvisors.com/bot)",
)
FETCH_TIMEOUT = 25
PSI_URL = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
PSI_TIMEOUT = 120

# The crawlers that feed answer engines. Blocking these is the single most
# actionable finding in the whole audit.
AI_CRAWLERS = (
    "GPTBot",            # ChatGPT training + browsing
    "OAI-SearchBot",     # ChatGPT search index
    "ChatGPT-User",      # ChatGPT live retrieval
    "PerplexityBot",
    "Perplexity-User",
    "ClaudeBot",
    "Claude-User",
    "anthropic-ai",
    "Google-Extended",   # Gemini / AI Overviews grounding
    "Applebot-Extended",
    "CCBot",             # Common Crawl -- feeds most training sets
    "meta-externalagent",
)

LOCAL_BUSINESS_TYPES = {
    "localbusiness", "organization", "professionalservice", "homeandconstructionbusiness",
    "plumber", "electrician", "roofingcontractor", "generalcontractor", "hvacbusiness",
    "restaurant", "dentist", "physician", "attorney", "legalservice", "realestateagent",
    "store", "autorepair", "medicalbusiness", "healthandbeautybusiness", "foodestablishment",
}

PILLAR_WEIGHTS = {
    "structured_data": 30,
    "answerability": 30,
    "crawlability": 25,
    "performance": 15,
}

# --------------------------------------------------------------------------
# fetching
# --------------------------------------------------------------------------

def _normalize_url(domain: str) -> str:
    domain = domain.strip()
    if not domain.startswith(("http://", "https://")):
        domain = "https://" + domain
    return domain


def _get(session: requests.Session, url: str, timeout: int = FETCH_TIMEOUT):
    return session.get(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
        timeout=timeout,
        allow_redirects=True,
    )


# --------------------------------------------------------------------------
# robots.txt
# --------------------------------------------------------------------------

def parse_robots(text: str) -> dict[str, list[str]]:
    """user-agent (lowercased) -> list of Disallow paths.

    Consecutive User-agent lines share the following rule block, per the
    robots.txt convention.
    """
    groups: dict[str, list[str]] = {}
    current: list[str] = []
    expecting_agents = False

    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field_name, _, value = line.partition(":")
        field_name = field_name.strip().lower()
        value = value.strip()

        if field_name == "user-agent":
            if not expecting_agents:
                current = []
                expecting_agents = True
            agent = value.lower()
            current.append(agent)
            groups.setdefault(agent, [])
        elif field_name in ("disallow", "allow"):
            expecting_agents = False
            for agent in current:
                if field_name == "disallow" and value:
                    groups.setdefault(agent, []).append(value)
        else:
            expecting_agents = False
    return groups


def blocks_agent(groups: dict[str, list[str]], agent: str) -> bool:
    """True if `agent` is disallowed from the whole site.

    A specific group wins over `*`; only a bare `Disallow: /` counts as a
    site-wide block.
    """
    specific = groups.get(agent.lower())
    rules = specific if specific is not None else groups.get("*", [])
    return "/" in [r.strip() for r in rules]


# --------------------------------------------------------------------------
# JSON-LD
# --------------------------------------------------------------------------

def extract_jsonld(soup: BeautifulSoup) -> list[dict[str, Any]]:
    """All JSON-LD nodes, @graph flattened, malformed blocks skipped."""
    nodes: list[dict[str, Any]] = []

    def walk(obj: Any) -> None:
        if isinstance(obj, list):
            for item in obj:
                walk(item)
        elif isinstance(obj, dict):
            if "@graph" in obj:
                walk(obj["@graph"])
            nodes.append(obj)

    for tag in soup.find_all("script", attrs={"type": re.compile("ld\\+json", re.I)}):
        raw = tag.string or tag.get_text() or ""
        try:
            walk(json.loads(raw))
        except (json.JSONDecodeError, TypeError):
            LOG.debug("unparseable JSON-LD block skipped")
    return nodes


def node_types(node: dict[str, Any]) -> set[str]:
    value = node.get("@type") or node.get("type") or []
    if isinstance(value, str):
        value = [value]
    return {str(v).split("/")[-1].lower() for v in value if v}


def has_type(nodes: Iterable[dict[str, Any]], wanted: set[str]) -> dict[str, Any] | None:
    for node in nodes:
        if node_types(node) & wanted:
            return node
    return None


def check_ai_crawlers(
    domain: str, session: requests.Session | None = None
) -> Finding:
    """Standalone robots.txt check for AI crawler blocking.

    Exposed on its own because it is the highest-conviction finding in the
    whole audit and SEO-era audit tools generally do not look for it. The
    Sightline adapter merges this in when Sightline's own result doesn't
    already cover it.
    """
    own_session = session is None
    session = session or requests.Session()
    try:
        robots_txt = ""
        error = ""
        try:
            r = _get(session, urljoin(_normalize_url(domain), "/robots.txt"), timeout=15)
            robots_txt = r.text if r.ok else ""
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"

        groups = parse_robots(robots_txt) if robots_txt else {}
        blocked = [a for a in AI_CRAWLERS if blocks_agent(groups, a)]
        return Finding(
            id="ai_crawlers_allowed",
            pillar="crawlability",
            label="AI crawlers allowed",
            passed=not blocked,
            severity="critical",
            weight=10,
            detail=(
                "robots.txt blocks " + ", ".join(blocked) + " -- this site is opted "
                "out of the answer engines it wants to appear in."
                if blocked else
                ("robots.txt could not be read (" + error + ")" if error
                 else "robots.txt does not block any major AI crawler.")
            ),
            evidence=", ".join(blocked),
        )
    finally:
        if own_session:
            session.close()


# --------------------------------------------------------------------------
# the audit
# --------------------------------------------------------------------------

def _question_headings(soup: BeautifulSoup) -> list[str]:
    out = []
    for tag in soup.find_all(["h2", "h3", "h4"]):
        text = tag.get_text(" ", strip=True)
        if not text:
            continue
        if text.endswith("?") or re.match(
            r"^(how|what|why|when|where|who|can|do|does|is|are|should)\b", text, re.I
        ):
            out.append(text)
    return out


def score_site(
    domain: str,
    psi_key: str | None = None,
    session: requests.Session | None = None,
) -> SiteReport:
    psi_key = psi_key or os.getenv("PSI_API_KEY")
    own_session = session is None
    session = session or requests.Session()
    url = _normalize_url(domain)
    findings: list[Finding] = []
    errors: list[str] = []

    def add(id_, pillar, label, passed, severity, weight, detail, evidence=""):
        findings.append(Finding(id_, pillar, label, passed, severity, weight,
                                detail, evidence))

    try:
        # ---- homepage -----------------------------------------------------
        try:
            resp = _get(session, url)
            html = resp.text
            status = resp.status_code
            final_url = resp.url
            reachable = resp.ok
        except Exception as exc:
            errors.append(f"homepage fetch failed: {type(exc).__name__}: {exc}")
            return SiteReport(domain=domain, url=url, reachable=False,
                              status_code=None, aeo_score=0, errors=errors)

        if not reachable:
            errors.append(f"homepage returned HTTP {status}")
            return SiteReport(domain=domain, url=url, reachable=False,
                              status_code=status, aeo_score=0, errors=errors)

        soup = BeautifulSoup(html, "html.parser")
        nodes = extract_jsonld(soup)
        text_content = soup.get_text(" ", strip=True)
        word_count = len(text_content.split())

        # ---- structured data (30) -----------------------------------------
        add("jsonld_present", "structured_data", "JSON-LD structured data present",
            bool(nodes), "critical", 8,
            "The homepage publishes no JSON-LD structured data, so answer engines "
            "have to guess what this business is, where it operates and what it sells."
            if not nodes else
            f"Found {len(nodes)} JSON-LD node(s) on the homepage.")

        biz = has_type(nodes, LOCAL_BUSINESS_TYPES)
        add("localbusiness_schema", "structured_data", "LocalBusiness/Organization schema",
            biz is not None, "critical", 8,
            "No LocalBusiness or Organization schema, so there is no machine-readable "
            "entity for this business to be cited as."
            if biz is None else
            f"Declared as {', '.join(sorted(node_types(biz)))}.")

        nap_fields = []
        if biz:
            if biz.get("name"):
                nap_fields.append("name")
            if biz.get("address"):
                nap_fields.append("address")
            if biz.get("telephone"):
                nap_fields.append("telephone")
        add("nap_complete", "structured_data", "Name/address/phone in schema",
            len(nap_fields) == 3, "high", 6,
            "Name, address and phone are not all present in the structured data, "
            "which is what answer engines match against Google Business Profile."
            if len(nap_fields) != 3 else "Name, address and phone all declared.",
            evidence=", ".join(nap_fields))

        sameas = biz.get("sameAs") if biz else None
        add("sameas_links", "structured_data", "sameAs entity links",
            bool(sameas), "medium", 3,
            "No sameAs links tying the site to its Google, Facebook or Yelp profiles, "
            "so the business reads as several unconnected entities."
            if not sameas else f"{len(sameas) if isinstance(sameas, list) else 1} sameAs link(s).")

        faq_node = has_type(nodes, {"faqpage", "question"})
        add("faq_schema", "structured_data", "FAQ / Question schema",
            faq_node is not None, "high", 5,
            "No FAQPage or Question schema anywhere on the homepage -- the format "
            "answer engines lift verbatim when they answer a question."
            if faq_node is None else "FAQ schema present.")

        # ---- answerability (30) -------------------------------------------
        title = (soup.title.get_text(strip=True) if soup.title else "")
        add("title_tag", "answerability", "Descriptive title tag",
            30 <= len(title) <= 65, "medium", 4,
            f"Title tag is {len(title)} characters ({'missing' if not title else 'outside the 30-65 range'}), "
            "so it reads poorly as an answer snippet."
            if not (30 <= len(title) <= 65) else f"Title: {title!r}",
            evidence=title)

        desc_tag = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
        desc = (desc_tag.get("content") or "").strip() if desc_tag else ""
        add("meta_description", "answerability", "Meta description",
            len(desc) >= 50, "medium", 3,
            "No usable meta description for engines to summarize from."
            if len(desc) < 50 else "Meta description present.",
            evidence=desc[:200])

        h1s = [h.get_text(" ", strip=True) for h in soup.find_all("h1")]
        add("single_h1", "answerability", "Exactly one H1",
            len(h1s) == 1, "medium", 3,
            f"Page has {len(h1s)} H1 headings; answer engines use the H1 to decide "
            "what the page is about."
            if len(h1s) != 1 else f"H1: {h1s[0]!r}",
            evidence="; ".join(h1s[:3]))

        questions = _question_headings(soup)
        add("question_headings", "answerability", "Question-shaped headings",
            len(questions) >= 3, "high", 10,
            f"Only {len(questions)} heading(s) are phrased as questions. Answer engines "
            "extract passages that sit under a question heading; pages built from "
            "marketing statements give them nothing to quote."
            if len(questions) < 3 else
            f"{len(questions)} question-shaped headings found.",
            evidence="; ".join(questions[:5]))

        add("content_depth", "answerability", "Substantive homepage content",
            word_count >= 400, "medium", 6,
            f"Homepage carries about {word_count} words -- too thin to be quoted as "
            "an authoritative answer."
            if word_count < 400 else f"About {word_count} words of content.",
            evidence=str(word_count))

        llms_ok = False
        try:
            lt = _get(session, urljoin(final_url, "/llms.txt"), timeout=10)
            llms_ok = lt.ok and "text/html" not in lt.headers.get("Content-Type", "")
        except Exception:
            llms_ok = False
        add("llms_txt", "answerability", "llms.txt present",
            llms_ok, "low", 4,
            "No llms.txt -- the emerging convention for handing answer engines a "
            "curated summary of the site. Cheap to add and still rare locally."
            if not llms_ok else "llms.txt published.")

        # ---- crawlability (25) --------------------------------------------
        robots_txt = ""
        try:
            r = _get(session, urljoin(final_url, "/robots.txt"), timeout=15)
            robots_txt = r.text if r.ok else ""
        except Exception as exc:
            errors.append(f"robots.txt fetch failed: {type(exc).__name__}: {exc}")

        groups = parse_robots(robots_txt) if robots_txt else {}
        blocked = [a for a in AI_CRAWLERS if blocks_agent(groups, a)]
        add("ai_crawlers_allowed", "crawlability", "AI crawlers allowed",
            not blocked, "critical", 10,
            "robots.txt blocks " + ", ".join(blocked) + " -- this site is opted out "
            "of the answer engines it wants to appear in."
            if blocked else
            "robots.txt does not block any major AI crawler.",
            evidence=", ".join(blocked))

        noindex = soup.find("meta", attrs={"name": re.compile("^robots$", re.I)})
        noindex_val = (noindex.get("content") or "").lower() if noindex else ""
        add("indexable", "crawlability", "Homepage indexable",
            "noindex" not in noindex_val, "critical", 5,
            "The homepage carries a noindex directive."
            if "noindex" in noindex_val else "No noindex directive.",
            evidence=noindex_val)

        sitemap_ok, sitemap_urls = False, 0
        sitemap_candidates = [
            m.group(1).strip()
            for m in re.finditer(r"(?im)^\s*sitemap:\s*(\S+)", robots_txt or "")
        ] or [urljoin(final_url, "/sitemap.xml")]
        for candidate in sitemap_candidates[:2]:
            try:
                sm = _get(session, candidate, timeout=15)
                if sm.ok and sm.text.lstrip().startswith("<"):
                    sitemap_ok = True
                    try:
                        root = ElementTree.fromstring(sm.content)
                        sitemap_urls = sum(
                            1 for el in root.iter()
                            if el.tag.endswith("}loc") or el.tag == "loc"
                        )
                    except ElementTree.ParseError:
                        pass
                    break
            except Exception:
                continue
        add("sitemap", "crawlability", "XML sitemap reachable",
            sitemap_ok, "high", 5,
            "No reachable XML sitemap, so crawlers discover pages only by following links."
            if not sitemap_ok else f"Sitemap found with {sitemap_urls} URL(s).",
            evidence=str(sitemap_urls))

        is_https = urlparse(final_url).scheme == "https"
        add("https", "crawlability", "HTTPS", is_https, "high", 3,
            "Site does not resolve over HTTPS." if not is_https else "Serves over HTTPS.")

        canonical = soup.find("link", attrs={"rel": re.compile("canonical", re.I)})
        add("canonical", "crawlability", "Canonical URL",
            canonical is not None, "low", 2,
            "No canonical tag, which invites duplicate-URL confusion."
            if canonical is None else "Canonical tag present.")

        # ---- performance (15) ---------------------------------------------
        psi_perf = psi_seo = None
        if psi_key:
            try:
                pr = session.get(
                    PSI_URL,
                    params=[("url", final_url), ("key", psi_key),
                            ("strategy", "mobile"),
                            ("category", "performance"), ("category", "seo")],
                    timeout=PSI_TIMEOUT,
                )
                pr.raise_for_status()
                cats = (pr.json().get("lighthouseResult") or {}).get("categories") or {}
                if (cats.get("performance") or {}).get("score") is not None:
                    psi_perf = int(round(cats["performance"]["score"] * 100))
                if (cats.get("seo") or {}).get("score") is not None:
                    psi_seo = int(round(cats["seo"]["score"] * 100))
            except Exception as exc:
                errors.append(f"PageSpeed failed: {type(exc).__name__}: {exc}")

        if psi_perf is not None:
            add("psi_performance", "performance", "Mobile performance",
                psi_perf >= 50, "high", 10,
                f"Mobile PageSpeed score is {psi_perf}/100."
                + (" Slow pages get crawled less and cited less."
                   if psi_perf < 50 else ""),
                evidence=str(psi_perf))
        if psi_seo is not None:
            add("psi_seo", "performance", "Lighthouse SEO",
                psi_seo >= 90, "medium", 5,
                f"Lighthouse SEO score is {psi_seo}/100.", evidence=str(psi_seo))

        # ---- roll up -------------------------------------------------------
        pillar_scores: dict[str, int] = {}
        available = 0
        earned = 0.0
        for pillar, weight in PILLAR_WEIGHTS.items():
            items = [f for f in findings if f.pillar == pillar]
            total_w = sum(f.weight for f in items)
            if not total_w:
                continue  # pillar not scored (e.g. no PSI key) -- rescale below
            got_w = sum(f.weight for f in items if f.passed)
            pct = got_w / total_w
            pillar_scores[pillar] = int(round(pct * 100))
            available += weight
            earned += pct * weight

        aeo_score = int(round(100 * earned / available)) if available else 0

        return SiteReport(
            domain=domain,
            url=final_url,
            reachable=True,
            status_code=status,
            aeo_score=aeo_score,
            pillar_scores=pillar_scores,
            findings=findings,
            performance_scored=psi_perf is not None,
            psi_performance=psi_perf,
            psi_seo=psi_seo,
            errors=errors,
        )
    finally:
        if own_session:
            session.close()


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="AEO-audit one domain")
    ap.add_argument("domain")
    ap.add_argument("--psi-key", default=os.getenv("PSI_API_KEY"))
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO)
    rep = score_site(args.domain, psi_key=args.psi_key)
    print(json.dumps(rep.to_dict(), indent=2))
