"""
test_sourcing.py -- sourcing logic against a fixture. No API spend.

    python3 test_sourcing.py
"""

from __future__ import annotations

import sys

from sourcing import (SourcingConfig, ShapeError, build_request,
                      filter_listings, to_prospect, validate_dfs_payload)

FAILURES: list[str] = []


def check(label: str, got, want) -> None:
    if got == want:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}: got {got!r}, want {want!r}")
        FAILURES.append(label)


def listing(title, domain, city="Allentown", votes=50, rating=4.6,
            category="plumber", **extra):
    item = {
        "title": title,
        "domain": domain,
        "category": category,
        "address_info": {"city": city, "region": "PA", "country_code": "US"},
        "rating": {"value": rating, "votes_count": votes, "rating_max": 5},
        "place_id": f"place_{title.lower().replace(' ', '_')}",
        "is_claimed": True,
    }
    item.update(extra)
    return item


CFG = SourcingConfig(min_reviews=20, chain_threshold=3)

# --------------------------------------------------------------------------
print("\nrequest construction")
req = build_request("plumber", SourcingConfig(center="40.63,-75.37", radius_km=25))
check("category set", req["categories"], ["plumber"])
check("coordinate formatted", req["location_coordinate"], "40.63,-75.37,25")
check("claimed filter on", req["is_claimed"], True)
check("review floor filters server-side", req["filters"][0],
      ["rating.votes_count", ">", 20])
check("sorted by review count", req["order_by"], ["rating.votes_count,desc"])

# --------------------------------------------------------------------------
print("\nlisting -> prospect")
p = to_prospect(listing("Otter Squad Plumbing", "www.ottersquad.com",
                        votes=64, rating=4.8), "plumber")
check("name carried", p.name, "Otter Squad Plumbing")
check("domain normalized", p.domain, "ottersquad.com")
check("city carried", p.city, "Allentown")
check("state carried", p.state, "PA")
check("review count carried", p.review_count, 64)
check("rating carried", p.rating, 4.8)
check("unnamed listing rejected", to_prospect({"domain": "x.com"}, "plumber"), None)

check("url used when domain missing",
      to_prospect({"title": "X", "url": "https://sub.example.com/page"},
                  "plumber").domain, "example.com")

# --------------------------------------------------------------------------
print("\nfiltering")
BATCH = [
    (listing("Otter Squad Plumbing", "ottersquad.com", votes=64), "plumber"),
    (listing("Beaver Brigade", "beaverbrigade.com", votes=88), "plumber"),
    (listing("No Site Plumbing", "", votes=120), "plumber"),
    (listing("Facebook Only Plumbing", "facebook.com/fbplumb", votes=45), "plumber"),
    (listing("Already Emailed Co", "alreadyknown.com", votes=70), "plumber"),
    (listing("Opted Out Inc", "optedout.com", votes=95), "plumber"),
    # same domain at four locations = franchise
    (listing("BigChain Allentown", "bigchain.com", votes=200), "plumber"),
    (listing("BigChain Bethlehem", "bigchain.com", "Bethlehem", votes=180), "plumber"),
    (listing("BigChain Easton", "bigchain.com", "Easton", votes=160), "plumber"),
    (listing("BigChain Emmaus", "bigchain.com", "Emmaus", votes=140), "plumber"),
    # two listings, one real business
    (listing("Dam Fine Drains", "damfine.com", votes=33), "plumber"),
    (listing("Dam Fine Drains LLC", "damfine.com", "Bethlehem", votes=31), "plumber"),
    (listing("Tiny New Shop", "tinynew.com", votes=4), "plumber"),
]

kept, rejected = filter_listings(BATCH, CFG,
                                 known_domains={"alreadyknown.com"},
                                 suppressed_domains={"optedout.com"})

kept_domains = sorted(p.domain for p in kept)
check("keeps only real candidates", kept_domains,
      ["beaverbrigade.com", "damfine.com", "ottersquad.com"])

reasons = {r.domain or r.name: r.reason for r in rejected}
check("no website dropped", reasons["No Site Plumbing"], "no_website")
check("social page dropped", reasons["facebook.com"], "social_page_only")
check("already known dropped", reasons["alreadyknown.com"], "already_known")
check("suppressed dropped", reasons["optedout.com"], "suppressed")
check("chain dropped", reasons["bigchain.com"], "chain")
check("thin reviews dropped", reasons["tinynew.com"], "too_few_reviews")
check("in-batch duplicate dropped",
      sum(1 for r in rejected if r.reason == "duplicate_in_batch"), 1)
check("chain dropped at every location",
      sum(1 for r in rejected if r.reason == "chain"), 4)

# A two-location local business is NOT a chain at the default threshold.
two_loc = [
    (listing("Local Two A", "localtwo.com", votes=60), "plumber"),
    (listing("Local Two B", "localtwo.com", "Easton", votes=55), "plumber"),
]
kept2, rej2 = filter_listings(two_loc, CFG, set(), set())
check("two locations is not a chain", [p.domain for p in kept2], ["localtwo.com"])
check("second location is a duplicate, not a chain",
      rej2[0].reason, "duplicate_in_batch")

# --------------------------------------------------------------------------
print("\nnational franchises")
FRANCHISE_BATCH = [
    (listing("Roto-Rooter", "rotorooter.com", votes=1271, rating=4.9), "plumber"),
    (listing("Mr. Handyman of Easton, Bethlehem, Nazareth & Allentown",
             "mrhandyman.com", "Easton", votes=788), "plumber"),
    (listing("Benjamin Franklin Plumbing", "bfplumbing-lv.com",
             "Bethlehem", votes=47), "plumber"),
    (listing("UGI Heating, Cooling & Plumbing", "ugihvac.com",
             "Whitehall", votes=473, rating=4.7), "plumber"),
    (listing("Elek Plumbing", "elekplumbing.com", "Bath", votes=1077), "plumber"),
    (listing("Agentis Plumbing", "agentisplumbing.com", "Bethlehem",
             votes=1030), "plumber"),
]
kept_f, rej_f = filter_listings(FRANCHISE_BATCH, CFG, set(), set())
check("locals survive", sorted(p.domain for p in kept_f),
      ["agentisplumbing.com", "elekplumbing.com"])
check("four franchises dropped",
      sum(1 for r in rej_f if r.reason == "national_franchise"), 4)
check("franchise caught by name even on a local domain",
      any(r.reason == "national_franchise" and r.domain == "bfplumbing-lv.com"
          for r in rej_f), True)

# --------------------------------------------------------------------------
print("\nmissing city")
NO_CITY = [
    (listing("Lehigh Plumbing", "lehighplumbing.com", city="", votes=911), "plumber"),
    (listing("Zoom Drain", "zoomdrain.com", city="", votes=503), "plumber"),
    (listing("Ark Plumbing LLC", "arkplumbingpa.com", "Allentown", votes=173), "plumber"),
]
kept_c, rej_c = filter_listings(NO_CITY, CFG, set(), set())
check("no-city listings rejected",
      sum(1 for r in rej_c if r.reason == "no_city"), 2)
check("a business with a city survives",
      [p.domain for p in kept_c], ["arkplumbingpa.com"])

borough = to_prospect({"title": "Borough Plumbing", "domain": "bp.com",
                       "address_info": {"borough": "Whitehall Township",
                                        "region": "PA"},
                       "rating": {"value": 4.8, "votes_count": 60}}, "plumber")
check("city falls back to borough", borough.city, "Whitehall Township")

# The reason this matters: the query the probe would have built.
from aeo_probe import Prospect, build_queries
bad = Prospect("Lehigh Plumbing", "lehighplumbing.com", "plumber", "", "PA")
check("empty city would have produced a malformed query",
      "in , PA" in build_queries(bad)[0][0], True)

# --------------------------------------------------------------------------
print("\nresponse validation")
GOOD = {"status_code": 20000,
        "tasks": [{"status_code": 20000, "result": [{"items": []}]}]}
validate_dfs_payload(GOOD)
print("  ok   valid payload passes")


def expect_shape_error(label, payload):
    try:
        validate_dfs_payload(payload)
        check(label, "accepted", "ShapeError")
    except ShapeError:
        check(label, "ShapeError", "ShapeError")


expect_shape_error("auth failure rejected",
                   {"status_code": 40100, "status_message": "Unauthorized"})
expect_shape_error("task-level error rejected",
                   {"status_code": 20000,
                    "tasks": [{"status_code": 40501, "status_message": "bad filter"}]})
expect_shape_error("missing tasks rejected", {"status_code": 20000})
expect_shape_error("html error page rejected", "<html>502</html>")

# A task with zero results is legitimate -- that category just had no matches.
validate_dfs_payload({"status_code": 20000,
                      "tasks": [{"status_code": 20000, "result": []}]})
print("  ok   an empty result set is valid, not an error")

# --------------------------------------------------------------------------
print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
    sys.exit(1)
print("all sourcing checks passed")
