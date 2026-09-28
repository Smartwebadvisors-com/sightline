"""A capped set of pages for one scan.

The homepage is the page the client pasted. From the sitemap we add the main
service page, one town page, and up to two articles. Search presence stays a
domain reading. Page speed is not decided here — the pipeline runs it only
on the homepage and the service page.
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass
from urllib.parse import urlparse

from . import discovery
from . import http as fetch_http
from . import robots as robots_mod

log = logging.getLogger(__name__)

MAX_SITEMAP_URLS = 400
MAX_CHILD_SITEMAPS = 3
MAX_ARTICLES = 2

_LOC = re.compile(r"<loc>\s*([^<]+?)\s*</loc>", re.I)
_YEAR = re.compile(r"(?:^|/)20\d{2}(?:/|$)")

_ARTICLE = {"blog", "news", "article", "articles", "post", "posts", "resources", "insights"}
_TOWN = {
    "service-area", "service-areas", "locations", "location",
    "areas-we-serve", "areas", "cities", "city",
}
_SKIP = {
    "about", "contact", "privacy", "terms", "cart", "account", "wp-admin",
    "tag", "author", "category", "feed", "login", "search",
}
_SERVICE_WORDS = (
    "service", "services", "plumbing", "plumber", "repair", "drain", "sewer",
    "installation", "install", "commercial", "residential", "backflow",
    "faucet", "jetting", "pump", "softener",
)
_QUESTION = ("does-", "what-", "why-", "how-", "is-", "can-", "when-", "every-", "should-")
_ARTICLE_BITS = ("-explained", "-checklist", "-cost-", "what-it-means", "-guide", "-vs-")
_PLACE_STOP = _SKIP | _ARTICLE | {
    "area", "county", "explained", "rule", "minutes", "program", "pipe",
    "checklist", "means", "first", "replacement", "water", "snaking", "cost",
    "heater", "sitemap", "home", "page", "service", "services", "plumbing",
    "repair", "installation",
}
# A trailing -me is "near me", not Maine.
_STATES = {
    "al", "ak", "az", "ar", "ca", "co", "ct", "dc", "de", "fl", "ga", "hi",
    "ia", "id", "il", "in", "ks", "ky", "la", "ma", "md", "mi", "mn", "mo",
    "ms", "mt", "nc", "nd", "ne", "nh", "nj", "nm", "nv", "ny", "oh", "ok",
    "or", "pa", "ri", "sc", "sd", "tn", "tx", "ut", "va", "vt", "wa", "wi",
    "wv", "wy",
}


@dataclass
class SamplePage:
    role: str
    url: str
    page: discovery.Page | None


def _path(url: str) -> str:
    path = (urlparse(url).path or "/").lower()
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    return path


def _segments(path: str) -> list[str]:
    return [part for part in path.split("/") if part]


def same_page(left: str, right: str) -> bool:
    return (
        fetch_http.domain(left) == fetch_http.domain(right)
        and _path(left) == _path(right)
    )


def _ends_with_state(segment: str) -> bool:
    if "near-me" in segment:
        return False
    stem, _, suffix = segment.rpartition("-")
    return bool(stem) and suffix in _STATES


def _slug(url: str) -> str:
    segments = _segments(_path(url))
    return segments[-1] if segments else ""


def _looks_like_article(segments: list[str]) -> bool:
    if any(part in _ARTICLE for part in segments):
        return not (len(segments) == 1 and segments[0] in _ARTICLE)
    blob = "/".join(segments)
    slug = segments[-1]
    if _YEAR.search(blob):
        return True
    if slug.startswith(_QUESTION):
        return True
    return any(bit in slug for bit in _ARTICLE_BITS)


def _has_service_word(blob: str) -> bool:
    return any(word in blob for word in _SERVICE_WORDS)


def classify(url: str) -> str:
    """home, service, town, article, or other. Articles win over a service word in the path."""
    segments = _segments(_path(url))
    if not segments:
        return "home"
    if segments[0] in _SKIP or segments[-1] in {"sitemap"}:
        return "other"
    if _looks_like_article(segments):
        return "article"
    if any(part in _TOWN for part in segments) or _ends_with_state(segments[-1]):
        return "town"
    blob = "/".join(segments)
    if "near-me" in segments[-1] or _has_service_word(blob):
        return "service"
    return "other"


def _shorter(url: str) -> tuple[int, int]:
    return (len(_segments(_path(url))), len(url))


def _dominant_place(urls: list[str]) -> str | None:
    """The town a flat site repeats at the end of its page names."""
    counts: Counter[str] = Counter()
    for url in urls:
        slug = _slug(url)
        if not slug or "-" not in slug:
            continue
        last = slug.rsplit("-", 1)[-1]
        if len(last) < 4 or not last.isalpha() or last in _PLACE_STOP:
            continue
        if classify(url) == "article":
            continue
        counts[last] += 1
    if not counts:
        return None
    token, count = counts.most_common(1)[0]
    return token if count >= 3 else None


def _ends_with_place(url: str, place: str) -> bool:
    return _slug(url).endswith("-" + place)


def document_locs(xml: str) -> tuple[str, list[str]]:
    """('index'|'urlset', locations). An index lists other sitemaps."""
    kind = "index" if re.search(r"<sitemapindex\b", xml or "", re.I) else "urlset"
    return kind, [loc.strip() for loc in _LOC.findall(xml or "")]


def _main_service(services: list[str], place: str | None) -> str | None:
    """A hub page if the site has one, otherwise the broadest service in the main town."""
    if not services:
        return None
    hubs = [
        url for url in services
        if any(part in {"services", "service", "plumbing"} for part in _segments(_path(url)))
    ]
    if hubs:
        return min(hubs, key=_shorter)

    def rank(url: str) -> tuple:
        slug = _slug(url)
        placed = 0 if place and _ends_with_place(url, place) else 1
        if "plumb" in slug:
            family = 0
        elif "drain" in slug:
            family = 1
        else:
            family = 2
        return (placed, family, *_shorter(url))

    return min(services, key=rank)


def pick(urls: list[str], homepage: str) -> list[tuple[str, str]]:
    """Homepage, then one service page, one town page, and up to two articles."""
    onsite = [
        url for url in urls
        if url and fetch_http.domain(url) == fetch_http.domain(homepage)
        and not same_page(url, homepage)
    ]
    services: list[str] = []
    towns: list[str] = []
    states: list[str] = []
    articles: list[str] = []
    for url in onsite:
        role = classify(url)
        if role == "article":
            articles.append(url)
        elif role == "service":
            services.append(url)
        elif role == "town":
            if any(part in _TOWN for part in _segments(_path(url))):
                towns.append(url)
            else:
                states.append(url)
    chosen: list[tuple[str, str]] = [("home", homepage)]
    place = _dominant_place(onsite)
    service = _main_service(services, place)
    if service:
        chosen.append(("service", service))
    flat_towns = [
        url for url in onsite
        if place and _ends_with_place(url, place) and url != service and classify(url) != "article"
    ]
    town_pool = towns or states or flat_towns
    if town_pool:
        chosen.append(("town", min(town_pool, key=_shorter)))
    for url in articles[:MAX_ARTICLES]:
        chosen.append(("article", url))
    return chosen


def _same_site(url: str, homepage: str) -> bool:
    return fetch_http.domain(url) == fetch_http.domain(homepage)


def load_urls(homepage: str, robots_text: str | None, fetch) -> list[str]:
    """fetch(url) -> object with .ok and .text. Sitemap trouble returns []."""
    candidates: list[str] = []
    if robots_text:
        candidates.extend(robots_mod.parse(robots_text).sitemaps)
    if not candidates:
        candidates.append(fetch_http.origin(homepage).rstrip("/") + "/sitemap.xml")

    found: list[str] = []
    seen: set[str] = set()
    pending = [url for url in candidates if _same_site(url, homepage)][:MAX_CHILD_SITEMAPS]
    children = 0
    while pending and len(found) < MAX_SITEMAP_URLS:
        url = pending.pop(0)
        if url in seen:
            continue
        seen.add(url)
        try:
            result = fetch(url)
        except Exception:
            log.warning("sitemap fetch failed for %s", url, exc_info=True)
            continue
        if not getattr(result, "ok", False):
            continue
        kind, locs = document_locs(getattr(result, "text", "") or "")
        locs = [loc for loc in locs if _same_site(loc, homepage)]
        if kind == "index":
            if children >= MAX_CHILD_SITEMAPS:
                continue
            room = MAX_CHILD_SITEMAPS - children
            pending.extend(locs[:room])
            children += min(len(locs), room)
            continue
        for loc in locs:
            if loc not in found:
                found.append(loc)
            if len(found) >= MAX_SITEMAP_URLS:
                break
    return found


def collect(ctx) -> list[SamplePage]:
    """Homepage plus the capped sample. A missing sitemap leaves the homepage."""
    home = SamplePage("home", ctx.url, ctx.page)
    try:
        urls = load_urls(ctx.url, ctx.robots_text, fetch_http.fetch)
    except Exception:
        log.warning("sitemap sample failed for %s", ctx.url, exc_info=True)
        return [home]
    pages = [home]
    for role, url in pick(urls, ctx.url):
        if role == "home":
            continue
        fetched = fetch_http.fetch_as_browser(url)
        if not fetched.ok:
            log.info("sample page skipped %s status=%s", url, fetched.status)
            continue
        final = fetched.final_url or url
        if not _same_site(final, ctx.url):
            continue
        pages.append(SamplePage(role, final, discovery.parse_page(final, fetched.text)))
    log.info("sample for %s: %s", ctx.domain, [(page.role, page.url) for page in pages])
    return pages


def context_for(base, sample: SamplePage):
    """A context whose page is this sample. Domain readings stay on the base."""
    from ..checks.base import ScanContext

    return ScanContext(
        url=sample.url,
        origin=base.origin,
        domain=base.domain,
        page=sample.page,
        robots_text=base.robots_text,
        robots_status=base.robots_status,
        llms_txt_fetch=base.llms_txt_fetch,
        profile=base.profile,
    )
