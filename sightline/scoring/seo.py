"""SEO score: an absolute measurement of organic search presence.

Deliberately NOT derived from finding deductions. The AEO composite in
report.py answers "how much of what we checked is wrong" — a checklist
question about a page we fetched. This answers "how much organic presence
does this domain actually have" — a level question about the market. The
two are meant to move independently, and a site can score well on one
while scoring badly on the other.

Four inputs, all measured by DataForSEO:

    ranked_keywords     organic keywords in Google US top 100
    top10_keywords      of those, how many rank in positions 1-10
    referring_domains   distinct domains linking in
    domain_rank         DataForSEO's own 0-1000 authority rank

Each is mapped to 0-100 through an anchor table, then combined as a
weighted mean over the components that were actually measured.

The discrimination at the low end comes from the GEOMETRIC SPACING of
the anchors (0, 10, 50, 250, 1000, 5000), not from the interpolation
between them. These metrics are power-law distributed across the
population this tool scans, so evenly spaced anchors would drop every
local business into the bottom band and the score could not tell a dead
site from a modest one — the only distinction that matters when
qualifying a prospect.

Interpolation within a segment is then done in log space so the curve
stays smooth across the anchors instead of kinking at each one. Measured
against the 120 scans on record, that shifts scores 3-5 points versus
linear interpolation and moves the median by 1.4. Real, but second
order — do not mistake it for the thing doing the work.

Coverage: an unmeasured component is dropped from BOTH the numerator and
the denominator, exactly as weights.py does for unavailable findings. A
DataForSEO endpoint we cannot call must never read as a site that scores
zero on it. If nothing could be measured the score is None, never 0.

The raw inputs are persisted next to the score in
sightline_scans.seo_metrics, so a future version can rescore all of
history without re-buying a single API call.

When you change the anchor tables or weights, BUMP SEO_SCORE_VERSION.
Stored scores record the version that produced them; if the tables move
underneath a version string, history stops being interpretable.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

SEO_SCORE_VERSION = "seo-v1"


@dataclass(frozen=True)
class Component:
    label: str
    weight: float
    # (metric value, subscore) ascending by value. Values between anchors
    # interpolate; values past either end clamp.
    anchors: tuple[tuple[float, float], ...]
    log_scale: bool = True
    rationale: str = ""


COMPONENTS: dict[str, Component] = {
    "ranked_keywords": Component(
        label="Ranked keywords",
        weight=0.25,
        anchors=((0, 0), (10, 20), (50, 40), (250, 60), (1000, 80),
                 (5000, 100)),
        rationale=(
            "Breadth of topical coverage that Google has actually indexed "
            "and ranked. An input to visibility, not visibility itself."
        ),
    ),
    "top10_keywords": Component(
        label="Top-10 keywords",
        weight=0.30,
        anchors=((0, 0), (1, 20), (5, 40), (20, 60), (75, 80), (300, 100)),
        rationale=(
            "The only outcome metric of the four. Positions 11+ earn "
            "effectively no clicks, so a domain can rank for hundreds of "
            "keywords and receive nothing. Weighted highest for that reason."
        ),
    ),
    "referring_domains": Component(
        label="Referring domains",
        weight=0.25,
        anchors=((0, 0), (5, 20), (25, 40), (100, 60), (500, 80),
                 (2500, 100)),
        rationale=(
            "Breadth of the inbound link graph. Distinct domains rather "
            "than raw backlink count, which one spammy source can inflate."
        ),
    ),
    "domain_rank": Component(
        label="Domain rank",
        weight=0.20,
        anchors=((0, 0), (50, 20), (150, 40), (300, 60), (500, 80),
                 (750, 100)),
        # Interpolated on the raw value: DataForSEO's 0-1000 rank is
        # already log-compressed by construction, and logging it a second
        # time flattens the top of the range into noise.
        log_scale=False,
        rationale=(
            "A vendor composite derived largely from the same link graph "
            "as referring_domains, so it is weighted lowest to avoid "
            "double-counting authority. It is also coarse at the low end: "
            "a domain with dozens of real referring domains can still "
            "read 0."
        ),
    ),
}

# Display order: breadth, then outcome, then the two authority signals.
COMPONENT_ORDER = ["ranked_keywords", "top10_keywords",
                   "referring_domains", "domain_rank"]

TOTAL_WEIGHT = sum(c.weight for c in COMPONENTS.values())


def _interpolate(value: float, anchors: tuple[tuple[float, float], ...],
                 log_scale: bool) -> float:
    """Piecewise-linear interpolation through `anchors`, optionally in
    log10(1+x) space. Clamps outside the table."""
    scale = (lambda x: math.log10(1.0 + x)) if log_scale else float
    v = max(0.0, float(value))
    xs = [scale(a) for a, _ in anchors]
    ys = [float(s) for _, s in anchors]
    x = scale(v)
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    for i in range(1, len(xs)):
        if x <= xs[i]:
            span = xs[i] - xs[i - 1]
            t = (x - xs[i - 1]) / span if span else 0.0
            return ys[i - 1] + t * (ys[i] - ys[i - 1])
    return ys[-1]


def component_score(name: str, value: float) -> float:
    c = COMPONENTS[name]
    return round(_interpolate(value, c.anchors, c.log_scale), 1)


def score(metrics: dict) -> dict:
    """Score the four inputs in `metrics` (as produced by
    dataforseo.metrics_from).

    Returns:
      version         SEO_SCORE_VERSION
      score           0-100, or None if nothing could be measured
      components      {name: {label, weight, value, score}} — value and
                      score are None for an unmeasured component
      covered_weight  fraction of total weight measured, 0.0-1.0
      measured        component names with data
      unmeasured      component names without
      raw             the metrics dict verbatim, for rescoring later
    """
    components: dict[str, dict] = {}
    numerator = 0.0
    covered = 0.0
    measured: list[str] = []
    unmeasured: list[str] = []

    for name in COMPONENT_ORDER:
        c = COMPONENTS[name]
        value = (metrics or {}).get(name)
        if value is None:
            components[name] = {"label": c.label, "weight": c.weight,
                                "value": None, "score": None}
            unmeasured.append(name)
            continue
        s = component_score(name, value)
        components[name] = {"label": c.label, "weight": c.weight,
                            "value": value, "score": s}
        numerator += c.weight * s
        covered += c.weight
        measured.append(name)

    return {
        "version": SEO_SCORE_VERSION,
        # Renormalized over measured weight only.
        "score": round(numerator / covered, 1) if covered else None,
        "components": components,
        "covered_weight": (round(covered / TOTAL_WEIGHT, 3)
                           if TOTAL_WEIGHT else 0.0),
        "measured": measured,
        "unmeasured": unmeasured,
        "raw": metrics or {},
    }


def snapshot() -> dict:
    """The full scoring definition, for documentation and for diffing two
    versions when SEO_SCORE_VERSION changes."""
    return {
        "version": SEO_SCORE_VERSION,
        "component_order": COMPONENT_ORDER,
        "components": {
            name: {"label": c.label, "weight": c.weight,
                   "anchors": [list(a) for a in c.anchors],
                   "log_scale": c.log_scale, "rationale": c.rationale}
            for name, c in COMPONENTS.items()
        },
    }
