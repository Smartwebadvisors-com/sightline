"""Structured data coverage. Reports which JSON-LD @types are present,
whether required properties are populated, and — for a detected business
type — flags common gaps.

We do not run full schema.org validation. We check the properties that
assistants and search rich-results parsers actually look at."""
from __future__ import annotations

from ..fetch.discovery import Page, flatten_jsonld, types_of
from .base import Finding, ScanContext

CHECK_ID = "structured_data"

# Types that suggest the page/site's "business kind." First match wins.
BUSINESS_TYPES = [
    "LocalBusiness", "Restaurant", "Store", "MedicalBusiness",
    "LegalService", "ProfessionalService", "AutoDealer", "Dentist",
    "HomeAndConstructionBusiness",
    "Product", "Service", "Article", "NewsArticle", "BlogPosting",
    "Organization", "Person", "Event", "Course", "FAQPage",
]

# Required-ish properties per type. Missing → medium/high depending.
REQUIRED = {
    "LocalBusiness":       {"high": ["name", "address"], "medium": ["telephone", "url"], "low": ["openingHours"]},
    "Restaurant":          {"high": ["name", "address"], "medium": ["telephone", "url", "servesCuisine"], "low": ["openingHours", "priceRange"]},
    "Store":               {"high": ["name", "address"], "medium": ["telephone", "url"], "low": ["openingHours"]},
    "Organization":        {"high": ["name"], "medium": ["url", "logo"], "low": ["sameAs"]},
    "Product":             {"high": ["name"], "medium": ["image", "description", "offers"], "low": ["brand", "aggregateRating"]},
    "Service":             {"high": ["name", "provider"], "medium": ["areaServed", "description"], "low": []},
    "Article":             {"high": ["headline", "author"], "medium": ["datePublished", "image"], "low": ["dateModified"]},
    "NewsArticle":         {"high": ["headline", "author"], "medium": ["datePublished", "image"], "low": ["dateModified"]},
    "BlogPosting":         {"high": ["headline", "author"], "medium": ["datePublished"], "low": ["dateModified"]},
    "FAQPage":             {"high": ["mainEntity"], "medium": [], "low": []},
    "Event":               {"high": ["name", "startDate", "location"], "medium": ["endDate", "description"], "low": []},
}


def _has(node: dict, prop: str) -> bool:
    v = node.get(prop)
    if v is None:
        return False
    if isinstance(v, str):
        return bool(v.strip())
    if isinstance(v, (list, dict)):
        return bool(v)
    return True


def _detect_business_type(nodes: list[dict]) -> str | None:
    seen_types: set[str] = set()
    for n in nodes:
        for t in types_of(n):
            seen_types.add(t)
    for t in BUSINESS_TYPES:
        if t in seen_types:
            return t
    return None


def run(ctx: ScanContext) -> list[Finding]:
    findings: list[Finding] = []
    if ctx.page is None:
        findings.append(Finding(
            check_id=CHECK_ID, item_key="page",
            severity="unavailable",
            examined="JSON-LD structured data on the page",
            observed="Page HTML could not be fetched; check skipped.",
            evidence={},
        ))
        return findings

    page: Page = ctx.page

    if not page.jsonld:
        findings.append(Finding(
            check_id=CHECK_ID, item_key="presence",
            severity="high",
            examined="JSON-LD structured data on the page",
            observed="No JSON-LD structured data blocks were found on the page.",
            remediation=(
                "Add at least Organization and WebSite JSON-LD to your site-wide "
                "template. Add page-type-appropriate schema (Product, Article, "
                "LocalBusiness, etc.) to matching page templates."
            ),
            evidence={"jsonld_count": 0},
        ))
        return findings

    nodes = flatten_jsonld(page.jsonld)
    parse_errors = [b for b in page.jsonld if isinstance(b, dict)
                    and b.get("_sightline_parse_error")]
    findings.append(Finding(
        check_id=CHECK_ID, item_key="presence",
        severity="pass",
        examined="JSON-LD structured data on the page",
        observed=(
            f"{len(page.jsonld)} JSON-LD block(s) found, "
            f"expanding to {len(nodes)} node(s)."
        ),
        evidence={
            "jsonld_blocks": len(page.jsonld),
            "nodes": len(nodes),
            "types": sorted({t for n in nodes for t in types_of(n)}),
        },
    ))

    if parse_errors:
        findings.append(Finding(
            check_id=CHECK_ID, item_key="parse",
            severity="high",
            examined="JSON-LD parseability",
            observed=f"{len(parse_errors)} JSON-LD block(s) failed to parse as JSON.",
            remediation=(
                "Validate JSON-LD blocks with a linter. Common causes: unescaped "
                "quotes in strings, trailing commas, comments (JSON does not permit them)."
            ),
            evidence={"parse_errors": len(parse_errors)},
        ))

    detected_type = _detect_business_type(nodes)
    findings.append(Finding(
        check_id=CHECK_ID, item_key="detected_type",
        severity="info",
        examined="Business/content type inferred from schema",
        observed=(
            f"Detected type: {detected_type}." if detected_type else
            "Could not infer a business or content type from schema present."
        ),
        evidence={"detected_type": detected_type},
    ))

    # Per-type required-property checks.
    checked_types = set()
    for n in nodes:
        for t in types_of(n):
            if t not in REQUIRED or t in checked_types:
                continue
            checked_types.add(t)
            spec = REQUIRED[t]
            missing = {sev: [p for p in props if not _has(n, p)]
                       for sev, props in spec.items()}
            for sev, props in missing.items():
                if not props:
                    continue
                findings.append(Finding(
                    check_id=CHECK_ID, item_key=f"props:{t}:{sev}",
                    severity=sev,
                    examined=f"Required properties on {t}",
                    observed=f"{t} is missing: {', '.join(props)}.",
                    remediation=(
                        f"Populate {', '.join(props)} on your {t} JSON-LD. "
                        "These are the fields assistants and rich-result parsers "
                        "commonly rely on."
                    ),
                    evidence={"type": t, "missing": props},
                ))
            if not any(missing.values()):
                findings.append(Finding(
                    check_id=CHECK_ID, item_key=f"props:{t}:pass",
                    severity="pass",
                    examined=f"Required properties on {t}",
                    observed=f"All checked properties present on {t}.",
                    evidence={"type": t},
                ))

    # Site-wide entities that are almost always worth having.
    all_types = {t for n in nodes for t in types_of(n)}
    for expected in ("Organization", "WebSite"):
        if expected not in all_types:
            findings.append(Finding(
                check_id=CHECK_ID, item_key=f"missing:{expected}",
                severity="medium",
                examined=f"Site-wide {expected} schema",
                observed=f"No {expected} node found in JSON-LD on this page.",
                remediation=(
                    f"Add {expected} JSON-LD to your site-wide template. It gives "
                    "assistants a stable anchor for who you are."
                ),
                evidence={"missing": expected},
            ))

    return findings
