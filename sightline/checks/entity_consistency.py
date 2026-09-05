"""Entity consistency. Do the entity identifiers on the page agree with each
other? Cross-checks:
  - Organization/LocalBusiness name vs page <title> and first <h1>
  - Telephone in JSON-LD vs telephones found in visible text
  - sameAs URLs are present and well-formed
  - Canonical URL vs the URL we actually fetched

Deep cross-site profile verification (calling Google Business, LinkedIn, etc.)
is out of scope for a single-URL scan."""
from __future__ import annotations

import re
from urllib.parse import urlparse

from ..fetch.discovery import Page, flatten_jsonld, types_of, visible_text
from .base import Finding, ScanContext

CHECK_ID = "entity_consistency"

ENTITY_TYPES = {"Organization", "LocalBusiness", "Restaurant", "Store",
                "MedicalBusiness", "LegalService", "ProfessionalService",
                "AutoDealer", "Dentist", "HomeAndConstructionBusiness",
                "Corporation", "NGO", "EducationalOrganization"}

PHONE_RE = re.compile(r"(?:\+?\d[\d\-\.\s\(\)]{7,}\d)")


def _normalize_phone(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def _pick_entity(nodes: list[dict]) -> dict | None:
    for n in nodes:
        if set(types_of(n)) & ENTITY_TYPES:
            return n
    return None


def _addr_to_str(a) -> str:
    if isinstance(a, str):
        return a
    if isinstance(a, dict):
        parts = [a.get(k, "") for k in
                 ("streetAddress", "addressLocality", "addressRegion",
                  "postalCode", "addressCountry")]
        return " ".join(str(p) for p in parts if p).strip()
    return ""


def run(ctx: ScanContext) -> list[Finding]:
    findings: list[Finding] = []
    if ctx.page is None:
        findings.append(Finding(
            check_id=CHECK_ID, item_key="page",
            severity="unavailable",
            examined="Entity consistency across the page",
            observed="Page HTML could not be fetched; check skipped.",
        ))
        return findings

    page: Page = ctx.page
    nodes = flatten_jsonld(page.jsonld) if page.jsonld else []
    entity = _pick_entity(nodes)

    # Canonical URL check.
    if page.canonical:
        can_host = urlparse(page.canonical).netloc.replace("www.", "").lower()
        fetched_host = urlparse(ctx.url).netloc.replace("www.", "").lower()
        if can_host and fetched_host and can_host != fetched_host:
            findings.append(Finding(
                check_id=CHECK_ID, item_key="canonical_host",
                severity="high",
                examined="rel=canonical vs fetched URL",
                observed=(
                    f"Canonical points to '{can_host}' but the fetched URL is "
                    f"on '{fetched_host}'. Search and assistants will follow "
                    "the canonical."
                ),
                remediation=(
                    "Confirm the canonical URL is intentional. If not, update "
                    "the rel=canonical link to the correct URL."
                ),
                evidence={"canonical": page.canonical, "fetched": ctx.url},
            ))
        else:
            findings.append(Finding(
                check_id=CHECK_ID, item_key="canonical_host",
                severity="pass",
                examined="rel=canonical vs fetched URL",
                observed="Canonical host matches fetched host.",
                evidence={"canonical": page.canonical},
            ))
    else:
        findings.append(Finding(
            check_id=CHECK_ID, item_key="canonical_host",
            severity="low",
            examined="rel=canonical link on the page",
            observed="No rel=canonical link found.",
            remediation=(
                "Add <link rel=\"canonical\" href=\"...\"> to reduce duplicate-URL "
                "ambiguity when the same content is reachable under multiple paths."
            ),
        ))

    if not entity:
        findings.append(Finding(
            check_id=CHECK_ID, item_key="entity_present",
            severity="medium",
            examined="Primary entity in JSON-LD",
            observed="No Organization/LocalBusiness-family node found in JSON-LD.",
            remediation=(
                "Add an Organization (or the appropriate LocalBusiness subtype) "
                "JSON-LD node so assistants and search have a canonical entity "
                "record for your business."
            ),
        ))
        return findings

    name = (entity.get("name") or "").strip()
    telephone = (entity.get("telephone") or "").strip()
    address_str = _addr_to_str(entity.get("address"))
    same_as = entity.get("sameAs") or []
    if isinstance(same_as, str):
        same_as = [same_as]

    findings.append(Finding(
        check_id=CHECK_ID, item_key="entity_present",
        severity="pass",
        examined="Primary entity in JSON-LD",
        observed=(
            f"Found entity '{name or '(unnamed)'}' "
            f"of type {', '.join(types_of(entity))}."
        ),
        evidence={
            "name": name, "telephone": telephone,
            "address": address_str, "sameAs_count": len(same_as),
        },
    ))

    # Name vs title / h1.
    if name:
        title = (page.title or "").lower()
        h1 = (page.h1s[0] if page.h1s else "").lower()
        name_l = name.lower()
        in_title = name_l in title
        in_h1 = name_l in h1
        if in_title or in_h1:
            findings.append(Finding(
                check_id=CHECK_ID, item_key="name_match",
                severity="pass",
                examined="Entity name vs <title> and <h1>",
                observed=(
                    f"Entity name appears in "
                    f"{'title' if in_title else ''}"
                    f"{' and ' if in_title and in_h1 else ''}"
                    f"{'h1' if in_h1 else ''}."
                ),
            ))
        else:
            findings.append(Finding(
                check_id=CHECK_ID, item_key="name_match",
                severity="medium",
                examined="Entity name vs <title> and <h1>",
                observed=(
                    f"Entity name '{name}' does not appear in the page title "
                    f"'{page.title or '(none)'}' or first h1 "
                    f"'{page.h1s[0] if page.h1s else '(none)'}'."
                ),
                remediation=(
                    "Assistants and search cross-check the entity name against "
                    "visible page copy. Reconcile the entity name across your "
                    "JSON-LD, page title, and headings."
                ),
                evidence={"name": name, "title": page.title,
                          "h1": page.h1s[0] if page.h1s else ""},
            ))

    # Telephone check.
    if telephone:
        text = visible_text(page.soup, limit=8000)
        page_phones = [_normalize_phone(m) for m in PHONE_RE.findall(text)]
        page_phones = [p for p in page_phones if len(p) >= 10]
        jsonld_phone = _normalize_phone(telephone)
        if not page_phones:
            findings.append(Finding(
                check_id=CHECK_ID, item_key="phone_match",
                severity="low",
                examined="Telephone in JSON-LD vs visible text",
                observed=(
                    f"JSON-LD telephone '{telephone}' but no phone number "
                    "was found in visible page text."
                ),
                remediation=(
                    "Display the same phone number on-page (in the header, footer, "
                    "or contact block) that is declared in JSON-LD."
                ),
            ))
        elif jsonld_phone in page_phones or any(
                jsonld_phone.endswith(p[-10:]) or p.endswith(jsonld_phone[-10:])
                for p in page_phones):
            findings.append(Finding(
                check_id=CHECK_ID, item_key="phone_match",
                severity="pass",
                examined="Telephone in JSON-LD vs visible text",
                observed="Telephone in JSON-LD matches visible text on the page.",
            ))
        else:
            findings.append(Finding(
                check_id=CHECK_ID, item_key="phone_match",
                severity="medium",
                examined="Telephone in JSON-LD vs visible text",
                observed=(
                    f"JSON-LD telephone '{telephone}' does not match phone "
                    f"numbers on the page ({', '.join(page_phones[:3])})."
                ),
                remediation=(
                    "Reconcile the phone number across JSON-LD, header/footer, "
                    "and contact page. Inconsistency degrades entity confidence."
                ),
                evidence={"jsonld": telephone, "page": page_phones},
            ))

    # sameAs presence.
    bad_urls = [u for u in same_as if not urlparse(str(u)).scheme.startswith("http")]
    if not same_as:
        findings.append(Finding(
            check_id=CHECK_ID, item_key="sameAs",
            severity="medium",
            examined="sameAs external profiles",
            observed="No sameAs URLs declared on the primary entity.",
            remediation=(
                "Add sameAs entries pointing to your business's canonical profiles "
                "(Google Business Profile, LinkedIn, industry directories). These "
                "give assistants a link graph for entity disambiguation."
            ),
        ))
    elif bad_urls:
        findings.append(Finding(
            check_id=CHECK_ID, item_key="sameAs",
            severity="low",
            examined="sameAs external profiles",
            observed=f"{len(bad_urls)} sameAs entries are not valid http(s) URLs.",
            evidence={"bad": bad_urls[:5]},
        ))
    else:
        findings.append(Finding(
            check_id=CHECK_ID, item_key="sameAs",
            severity="pass",
            examined="sameAs external profiles",
            observed=f"{len(same_as)} sameAs URL(s) declared.",
            evidence={"count": len(same_as), "sample": [str(u) for u in same_as[:5]]},
        ))

    return findings
