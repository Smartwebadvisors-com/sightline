"""
sourcing.py -- the SENSE stage: find Lehigh Valley businesses worth scanning.

Pulls Google Business Profile listings from DataForSEO by category and map
radius, filters them down to businesses that could plausibly become clients,
and enqueues the survivors for scanning.

The filtering here matters more than the fetching. Every prospect that reaches
the scan stage costs a Sightline run and five Perplexity calls, so anything we
can reject on the listing data alone -- no website, a chain, someone we already
contacted -- is money not spent and a scan slot freed for a real candidate.

    python3 sourcing.py --dry-run                    # see what it would queue
    python3 sourcing.py --categories plumber roofing_contractor
    python3 sourcing.py --enqueue                    # push to the sense queue

Env:
    DATAFORSEO_LOGIN / DATAFORSEO_PASSWORD   required
    DATABASE_URL                             for dedupe + suppression
    LV_CENTER      default "40.6300,-75.3700"   (between Allentown and Easton)
    LV_RADIUS_KM   default 25                   (covers Allentown-Bethlehem-Easton)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

import requests

from aeo_probe import Prospect, registrable_domain
from resilience import Budget, BudgetExhausted, Guard, RetryPolicy, ShapeError, require

LOG = logging.getLogger("aeo.sourcing")

DFS_URL = "https://api.dataforseo.com/v3/business_data/business_listings/search/live"
DFS_TIMEOUT = 120

# Centered between Allentown and Easton so a single 25km radius covers the
# whole Allentown-Bethlehem-Easton corridor plus Emmaus, Whitehall, Nazareth,
# Hellertown and Macungie. Widen to 35 to reach Quakertown and Kutztown.
LV_CENTER = os.getenv("LV_CENTER", "40.6300,-75.3700")
LV_RADIUS_KM = float(os.getenv("LV_RADIUS_KM", "25"))

# Local service businesses that actually buy marketing. Edit freely -- these
# are DataForSEO category slugs, up to 10 per request.
DEFAULT_CATEGORIES = [
    "plumber",
    "roofing_contractor",
    "electrician",
    "hvac_contractor",
    "general_contractor",
    "landscaper",
    "dentist",
    "chiropractor",
    "personal_injury_attorney",
    "auto_repair_shop",
]

# National franchises and utilities. Their marketing is decided at a corporate
# office in another state, so the local manager cannot act on a gap report even
# if they agree with it. Domain-frequency alone does not catch these: a brand
# with one location inside the search radius looks exactly like a local shop.
FRANCHISE_DOMAINS = {
    "rotorooter.com", "mrhandyman.com", "benjaminfranklinplumbing.com",
    "onehourheatandair.com", "mistersparky.com", "aireserv.com", "mrrooter.com",
    "rooterman.com", "servpro.com", "servicemaster.com", "chemdry.com",
    "mollymaid.com", "twomenandatruck.com", "terminix.com", "orkin.com",
    "mosquitojoe.com", "trugreen.com", "lawndoctor.com", "weedman.com",
    "bathfitter.com", "leaffilter.com", "leafguard.com", "windowworld.com",
    "championwindow.com", "renewalbyandersen.com", "budgetblinds.com",
    "precisiondoor.net", "midas.com", "jiffylube.com", "aamco.com",
    "meineke.com", "acehardware.com", "aptivepest.com", "ugihvac.com",
    "ugi.com", "pplelectric.com",
}

# Matched against the normalized business name, for franchises whose local
# operator registered their own domain.
FRANCHISE_NAMES = (
    "roto rooter", "mr handyman", "mister handyman", "benjamin franklin plumbing",
    "one hour heating", "mister sparky", "mr sparky", "aire serv", "mr rooter",
    "mister rooter", "rooter man", "servpro", "servicemaster", "chem dry",
    "molly maid", "two men and a truck", "terminix", "orkin", "mosquito joe",
    "trugreen", "true green", "lawn doctor", "weed man", "bath fitter",
    "leaffilter", "leaf filter", "leafguard", "window world", "champion windows",
    "renewal by andersen", "budget blinds", "precision garage door",
    "midas", "jiffy lube", "aamco", "meineke", "ace hardware",
    "ugi ", "ppl electric", "home depot", "lowes", "sears",
)

# A listing whose "website" is a social or directory page has no site to audit.
# Different pitch, different conversation -- not this pipeline.
NON_SITE_DOMAINS = {
    "facebook.com", "instagram.com", "twitter.com", "x.com", "linkedin.com",
    "yelp.com", "google.com", "sites.google.com", "business.site", "wixsite.com",
    "linktr.ee", "nextdoor.com", "tiktok.com", "youtube.com", "square.site",
    "godaddysites.com", "weebly.com", "blogspot.com", "wordpress.com",
}


@dataclass
class SourcingConfig:
    center: str = LV_CENTER
    radius_km: float = LV_RADIUS_KM
    categories: list[str] = field(default_factory=lambda: list(DEFAULT_CATEGORIES))
    min_reviews: int = 20          # matches GateConfig -- don't fetch what we'd skip
    min_rating: float = 3.8
    limit_per_category: int = 100
    require_claimed: bool = True   # unclaimed listings are usually stale or defunct
    chain_threshold: int = 3       # same domain at N+ locations = franchise, skip
    max_requests: int = 20         # hard cap on DataForSEO calls per run


@dataclass
class Rejection:
    name: str
    domain: str
    reason: str


@dataclass
class SourcingResult:
    prospects: list[Prospect] = field(default_factory=list)
    rejected: list[Rejection] = field(default_factory=list)
    requests_made: int = 0
    raw_items: int = 0

    def summary(self) -> str:
        counts: dict[str, int] = {}
        for r in self.rejected:
            counts[r.reason] = counts.get(r.reason, 0) + 1
        drops = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "none"
        return (f"{self.raw_items} listings -> {len(self.prospects)} queued "
                f"({self.requests_made} API calls)\n  dropped: {drops}")


# ---------------------------------------------------------------------------
# DataForSEO
# ---------------------------------------------------------------------------

def _auth() -> tuple[str, str]:
    login = os.getenv("DATAFORSEO_LOGIN")
    password = os.getenv("DATAFORSEO_PASSWORD")
    if not login or not password:
        raise RuntimeError("DATAFORSEO_LOGIN / DATAFORSEO_PASSWORD are not set")
    return login, password


def build_request(category: str, cfg: SourcingConfig) -> dict[str, Any]:
    """One DataForSEO task.

    Filters run server-side so we are not billed for, and do not page through,
    businesses the gate would reject anyway.
    """
    filters: list[Any] = [
        ["rating.votes_count", ">", cfg.min_reviews],
        "and",
        ["rating.value", ">=", cfg.min_rating],
    ]
    payload: dict[str, Any] = {
        "categories": [category],
        "location_coordinate": f"{cfg.center},{cfg.radius_km:g}",
        "filters": filters,
        "order_by": ["rating.votes_count,desc"],
        "limit": cfg.limit_per_category,
    }
    if cfg.require_claimed:
        payload["is_claimed"] = True
    return payload


def validate_dfs_payload(payload: Any) -> None:
    """Same principle as the probe: a wrong-shaped 200 must not read as 'no results'."""
    dep = "dataforseo"
    require(isinstance(payload, dict), dep, f"not an object ({type(payload).__name__})")
    require(payload.get("status_code") == 20000, dep,
            f"status_code={payload.get('status_code')} "
            f"{payload.get('status_message', '')}".strip())
    tasks = payload.get("tasks")
    require(isinstance(tasks, list) and bool(tasks), dep, "no tasks[]")
    task = tasks[0] or {}
    require(task.get("status_code") == 20000, dep,
            f"task status={task.get('status_code')} {task.get('status_message', '')}".strip())
    require("result" in task, dep, "task has no result key")


def fetch_category(category: str, cfg: SourcingConfig, guard: Guard,
                   session: requests.Session) -> list[dict[str, Any]]:
    login, password = _auth()
    body = [build_request(category, cfg)]

    def call() -> dict[str, Any]:
        resp = session.post(DFS_URL, auth=(login, password), json=body,
                            timeout=DFS_TIMEOUT)
        resp.raise_for_status()
        return resp.json()

    payload = guard.call("dataforseo", call, validate=validate_dfs_payload)
    result = (payload["tasks"][0].get("result") or [])
    items: list[dict[str, Any]] = []
    for block in result:
        items.extend((block or {}).get("items") or [])
    LOG.info("%s: %d listing(s)", category, len(items))
    return items


# ---------------------------------------------------------------------------
# listing -> prospect
# ---------------------------------------------------------------------------

def is_franchise(prospect: "Prospect") -> bool:
    """National brand or utility, by domain or by name."""
    if prospect.domain in FRANCHISE_DOMAINS:
        return True
    name = _normalize_name(prospect.name)
    return any(f.strip() in name for f in FRANCHISE_NAMES)


def _normalize_name(name: str) -> str:
    import re as _re
    n = (name or "").lower().replace("&", " and ")
    n = _re.sub(r"[^a-z0-9]+", " ", n)
    return " " + _re.sub(r"\s+", " ", n).strip() + " "


def to_prospect(item: dict[str, Any], fallback_category: str) -> Prospect | None:
    domain = registrable_domain(item.get("domain") or item.get("url") or "")
    name = (item.get("title") or item.get("original_title") or "").strip()
    if not name:
        return None

    address = item.get("address_info") or {}
    rating = item.get("rating") or {}

    # Many listings have no city on the address. That matters more than it
    # looks: the probe builds queries like "best plumber in {city}, {state}",
    # and an empty city produces "best plumber in , PA" -- a malformed query
    # whose answer means nothing. Fall back to borough, then to nothing, and
    # let the filter reject it rather than probing on garbage.
    city = (address.get("city") or address.get("borough") or "").strip()

    return Prospect(
        name=name,
        domain=domain,
        category=(item.get("category") or fallback_category or "").strip(),
        city=city,
        state=(address.get("region") or "").strip(),
        place_id=item.get("place_id") or item.get("cid"),
        review_count=rating.get("votes_count"),
        rating=rating.get("value"),
    )


def filter_listings(
    items: Iterable[tuple[dict[str, Any], str]],
    cfg: SourcingConfig,
    known_domains: set[str],
    suppressed_domains: set[str],
) -> tuple[list[Prospect], list[Rejection]]:
    """Everything we can reject before spending a scan.

    Order matters only for which reason gets recorded; each listing is dropped
    once, with the reason kept so the source list can be tuned rather than
    guessed at.
    """
    candidates: list[Prospect] = []
    rejected: list[Rejection] = []
    domain_counts: dict[str, int] = {}

    # First pass: build prospects and count domain frequency for chain detection.
    for item, category in items:
        prospect = to_prospect(item, category)
        if prospect is None:
            rejected.append(Rejection("(unnamed)", "", "no_name"))
            continue
        if prospect.domain:
            domain_counts[prospect.domain] = domain_counts.get(prospect.domain, 0) + 1
        candidates.append(prospect)

    seen: set[str] = set()
    keep: list[Prospect] = []
    for p in candidates:
        d = p.domain

        if not d:
            # No website at all. A real prospect for a different offer, but
            # there is nothing here to audit or to put in a gap report.
            rejected.append(Rejection(p.name, "", "no_website"))
            continue
        if d in NON_SITE_DOMAINS:
            rejected.append(Rejection(p.name, d, "social_page_only"))
            continue
        if is_franchise(p):
            rejected.append(Rejection(p.name, d, "national_franchise"))
            continue
        if not p.city:
            # Cannot build a valid buyer query without a town.
            rejected.append(Rejection(p.name, d, "no_city"))
            continue
        if d in suppressed_domains:
            rejected.append(Rejection(p.name, d, "suppressed"))
            continue
        if d in known_domains:
            rejected.append(Rejection(p.name, d, "already_known"))
            continue
        if domain_counts.get(d, 0) >= cfg.chain_threshold:
            # One domain across several locations is a franchise or a chain.
            # Their marketing decisions are not made in the Lehigh Valley.
            rejected.append(Rejection(p.name, d, "chain"))
            continue
        if d in seen:
            rejected.append(Rejection(p.name, d, "duplicate_in_batch"))
            continue
        if (p.review_count or 0) < cfg.min_reviews:
            rejected.append(Rejection(p.name, d, "too_few_reviews"))
            continue

        seen.add(d)
        keep.append(p)

    return keep, rejected


# ---------------------------------------------------------------------------
# dedupe sources
# ---------------------------------------------------------------------------

def load_known_domains(dsn: str | None = None) -> tuple[set[str], set[str]]:
    """(already in the database, suppressed) -- empty sets if no DB configured."""
    dsn = dsn or os.getenv("DATABASE_URL")
    if not dsn:
        LOG.warning("DATABASE_URL not set -- no dedupe against existing prospects")
        return set(), set()
    try:
        import psycopg
        with psycopg.connect(dsn, connect_timeout=10) as conn, conn.cursor() as cur:
            cur.execute("SELECT domain, suppressed FROM sightline_prospects;")
            rows = cur.fetchall()
        known = {r[0] for r in rows}
        suppressed = {r[0] for r in rows if r[1]}
        return known, suppressed
    except Exception as exc:
        # Failing open here would re-queue everyone we have already emailed.
        raise RuntimeError(
            f"could not load existing prospects for dedupe: {exc}. "
            "Refusing to source without it -- re-contacting people is worse "
            "than sourcing nothing."
        ) from exc


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------

def source(cfg: SourcingConfig | None = None, guard: Guard | None = None,
           session: requests.Session | None = None) -> SourcingResult:
    cfg = cfg or SourcingConfig()
    guard = guard or Guard(
        budget=Budget(max_calls={"dataforseo": cfg.max_requests}),
        retry=RetryPolicy(attempts=3, base_delay_s=2),
    )
    own_session = session is None
    session = session or requests.Session()

    known, suppressed = load_known_domains()
    LOG.info("dedupe: %d known domain(s), %d suppressed", len(known), len(suppressed))

    collected: list[tuple[dict[str, Any], str]] = []
    requests_made = 0
    try:
        for category in cfg.categories:
            try:
                items = fetch_category(category, cfg, guard, session)
                requests_made += 1
                collected.extend((item, category) for item in items)
            except BudgetExhausted as exc:
                LOG.error("stopping: %s", exc)
                break
            except ShapeError as exc:
                LOG.error("dataforseo response shape changed (%s) -- stopping "
                          "rather than treating it as an empty result", exc.message)
                break
            except Exception as exc:
                LOG.warning("category %s failed: %s", category, exc)
    finally:
        if own_session:
            session.close()

    prospects, rejected = filter_listings(collected, cfg, known, suppressed)
    return SourcingResult(prospects=prospects, rejected=rejected,
                          requests_made=requests_made, raw_items=len(collected))


def enqueue(prospects: Iterable[Prospect]) -> int:
    """Push onto the prospect queue that autoscan drains."""
    import queue_pg
    return queue_pg.enqueue(prospects, stage=queue_pg.STAGE_PROSPECT)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Source Lehigh Valley prospects")
    ap.add_argument("--categories", nargs="*", default=None,
                    help=f"default: {' '.join(DEFAULT_CATEGORIES)}")
    ap.add_argument("--center", default=LV_CENTER, help='"lat,lng"')
    ap.add_argument("--radius-km", type=float, default=LV_RADIUS_KM)
    ap.add_argument("--min-reviews", type=int, default=SourcingConfig.min_reviews)
    ap.add_argument("--limit", type=int, default=SourcingConfig.limit_per_category)
    ap.add_argument("--max-requests", type=int, default=SourcingConfig.max_requests)
    ap.add_argument("--enqueue", action="store_true",
                    help="push results onto the sense queue")
    ap.add_argument("--out", help="write prospects.json here")
    ap.add_argument("--dry-run", action="store_true",
                    help="fetch and filter, but queue nothing")
    ap.add_argument("--from-file", help="parse a saved API response, no spend")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

    cfg = SourcingConfig(
        center=args.center,
        radius_km=args.radius_km,
        categories=args.categories or list(DEFAULT_CATEGORIES),
        min_reviews=args.min_reviews,
        limit_per_category=args.limit,
        max_requests=args.max_requests,
    )

    if args.from_file:
        payload = json.load(open(args.from_file, encoding="utf-8"))
        validate_dfs_payload(payload)
        items = []
        for block in payload["tasks"][0].get("result") or []:
            items.extend((i, cfg.categories[0]) for i in (block or {}).get("items") or [])
        known, suppressed = (set(), set())
        prospects, rejected = filter_listings(items, cfg, known, suppressed)
        result = SourcingResult(prospects, rejected, 0, len(items))
    else:
        result = source(cfg)

    print("\n" + result.summary())
    print("\n--- queued ---")
    for p in result.prospects[:50]:
        print(f"  {p.review_count or 0:>4} reviews  {p.rating or 0:>3}  "
              f"{p.domain:<34} {p.name} ({p.city})")
    if len(result.prospects) > 50:
        print(f"  ... and {len(result.prospects) - 50} more")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump([asdict(p) for p in result.prospects], fh, indent=2)
        print(f"\nwrote {args.out}")

    if args.enqueue and not args.dry_run:
        enqueue(result.prospects)
    elif args.dry_run:
        print("\ndry run -- nothing queued")

    return 0


if __name__ == "__main__":
    sys.exit(main())
