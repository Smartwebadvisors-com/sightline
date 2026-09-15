"""COPY.md rule 9, enforced: no finding may assert the absence of something
another check on the same scan reports as present.

Scan 178 (mallofhope.com) shipped all three of these in one report:

    structured_data     "No Organization node found in JSON-LD on this page"
    entity_consistency  "Found entity 'Mall of Hope' of type NGO"     (pass)
    structured_data     "2 JSON-LD block(s) found, expanding to 5 node(s)"

...because one check matched the literal string "Organization" while the
other kept its own subtype list. A client reading that cannot tell which
sentence to believe, and the wrong one cost them 6 points.

The rule is general, so the enforcement is too: CONTRADICTIONS pairs each
absence claim we make with the presence claim that would contradict it, and
contradictions_in() is what a new pair gets checked by. Add the pair here
when you add a check that asserts something is missing.
"""
from __future__ import annotations

import unittest
from dataclasses import dataclass

from sightline.checks import entity_consistency, structured_data
from sightline.checks.base import ScanContext
from sightline.checks.schema_org import (ORGANIZATION_ALIASES,
                                         ORGANIZATION_TYPES,
                                         SCHEMA_ORG_VERSION, is_organization,
                                         organization_subtype)
from sightline.fetch.discovery import parse_page

# Severities that assert a gap. 'pass'/'info' record something we checked and
# found fine; 'unavailable' means we could not measure and never contradicts
# anything.
GAP = ("low", "medium", "high", "critical")
PRESENT = ("pass", "info")


@dataclass(frozen=True)
class Claim:
    check_id: str
    item_key: str


@dataclass(frozen=True)
class Contradiction:
    absence: Claim     # asserts the subject is not there
    presence: Claim    # reports the subject is there
    subject: str


CONTRADICTIONS = (
    Contradiction(
        absence=Claim("structured_data", "missing:Organization"),
        presence=Claim("entity_consistency", "entity_present"),
        subject="an Organization-family node in the page's JSON-LD",
    ),
)


def contradictions_in(findings) -> list[str]:
    """Every rule-9 violation in one scan's findings, as readable strings.

    Takes anything with .check_id/.item_key/.severity, so it runs on live
    check output and on rows read back from the database.
    """
    by_key = {(f.check_id, f.item_key): f for f in findings}
    out: list[str] = []
    for c in CONTRADICTIONS:
        gap = by_key.get((c.absence.check_id, c.absence.item_key))
        rep = by_key.get((c.presence.check_id, c.presence.item_key))
        if gap is None or gap.severity not in GAP:
            continue
        if rep is None or rep.severity not in PRESENT:
            continue
        out.append(
            f"{c.absence.check_id}/{c.absence.item_key} ({gap.severity}) "
            f"denies {c.subject}, which "
            f"{c.presence.check_id}/{c.presence.item_key} "
            f"({rep.severity}) reports as present: "
            f"{gap.observed!r} vs {rep.observed!r}"
        )
    return out


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

def _page_html(jsonld: str, title: str = "Mall of Hope",
               h1: str = "Mall of Hope") -> str:
    return f"""<!doctype html><html lang="en"><head><title>{title}</title>
<link rel="canonical" href="https://example.com/">
<script type="application/ld+json">{jsonld}</script>
</head><body><h1>{h1}</h1><p>Call us on (555) 010-2030.</p></body></html>"""


# Shaped like scan 178: an NGO node plus a WebSite node, no literal
# "Organization" anywhere.
NGO_JSONLD = """{"@context":"https://schema.org","@graph":[
  {"@type":"NGO","name":"Mall of Hope","url":"https://example.com/",
   "logo":"https://example.com/logo.png","telephone":"(555) 010-2030",
   "sameAs":["https://www.facebook.com/mallofhope",
             "https://www.linkedin.com/company/mallofhope"]},
  {"@type":"WebSite","name":"Mall of Hope","url":"https://example.com/"}]}"""

PLUMBER_JSONLD = """{"@context":"https://schema.org","@graph":[
  {"@type":["LocalBusiness","Plumber"],"name":"Otter Squad Plumbing",
   "url":"https://example.com/","telephone":"(555) 010-2030",
   "logo":"https://example.com/logo.png",
   "address":{"@type":"PostalAddress","streetAddress":"1 Main St",
              "addressLocality":"Springfield","addressRegion":"IL",
              "postalCode":"62701"},
   "sameAs":["https://www.facebook.com/ottersquad"]},
  {"@type":"WebSite","name":"Otter Squad Plumbing","url":"https://example.com/"}]}"""

# No organization of any kind — the finding MUST still fire here.
NO_ORG_JSONLD = """{"@context":"https://schema.org","@graph":[
  {"@type":"WebSite","name":"A Blog","url":"https://example.com/"},
  {"@type":"BreadcrumbList","itemListElement":[]}]}"""


def _run_checks(jsonld: str, title: str = "Mall of Hope",
                h1: str = "Mall of Hope"):
    page = parse_page("https://example.com/", _page_html(jsonld, title, h1))
    ctx = ScanContext(url="https://example.com/", origin="https://example.com",
                      domain="example.com", page=page)
    return structured_data.run(ctx) + entity_consistency.run(ctx)


def _keys(findings) -> set[str]:
    return {f.item_key for f in findings}


def _one(findings, check_id, item_key):
    for f in findings:
        if f.check_id == check_id and f.item_key == item_key:
            return f
    return None


# --------------------------------------------------------------------------
# rule 9
# --------------------------------------------------------------------------

class TestNoFindingDeniesWhatAnotherReports(unittest.TestCase):
    def test_the_ngo_site_that_shipped_the_contradiction(self):
        """Regression for scan 178."""
        findings = _run_checks(NGO_JSONLD)
        self.assertEqual(contradictions_in(findings), [])

    def test_the_absence_claim_is_simply_gone_not_downgraded(self):
        findings = _run_checks(NGO_JSONLD)
        self.assertNotIn("missing:Organization", _keys(findings))

    def test_the_entity_check_still_reports_the_ngo_as_present(self):
        """Half the pair. If this regressed, the contradiction test would
        pass for the wrong reason — both checks silently saying nothing."""
        f = _one(_run_checks(NGO_JSONLD), "entity_consistency",
                 "entity_present")
        self.assertIsNotNone(f)
        self.assertEqual(f.severity, "pass")
        self.assertIn("NGO", f.observed)

    def test_a_localbusiness_subtype_site_passes(self):
        findings = _run_checks(PLUMBER_JSONLD, title="Otter Squad Plumbing",
                               h1="Otter Squad Plumbing")
        self.assertEqual(contradictions_in(findings), [])
        self.assertNotIn("missing:Organization", _keys(findings))

    def test_a_site_with_no_organization_still_gets_the_finding(self):
        """The fix must not gag the true finding — 13 of the 56 domains
        really had no organization node."""
        findings = _run_checks(NO_ORG_JSONLD, title="A Blog", h1="A Blog")
        f = _one(findings, "structured_data", "missing:Organization")
        self.assertIsNotNone(f)
        self.assertEqual(f.severity, "medium")
        self.assertEqual(contradictions_in(findings), [])

    def test_the_detector_finds_the_contradiction_when_one_exists(self):
        """The detector itself, against the exact shape scan 178 stored."""
        from sightline.checks.base import Finding
        findings = [
            Finding(check_id="structured_data", item_key="missing:Organization",
                    severity="medium", examined="x",
                    observed="No Organization node found in JSON-LD on this page."),
            Finding(check_id="entity_consistency", item_key="entity_present",
                    severity="pass", examined="x",
                    observed="Found entity 'Mall of Hope' of type NGO."),
        ]
        violations = contradictions_in(findings)
        self.assertEqual(len(violations), 1)
        self.assertIn("NGO", violations[0])

    def test_the_website_finding_is_untouched_by_the_subtype_change(self):
        """Organization matches a branch; WebSite is still an exact match,
        and the NGO fixture declares one."""
        self.assertNotIn("missing:WebSite", _keys(_run_checks(NGO_JSONLD)))
        self.assertIn("missing:WebSite",
                      _keys(_run_checks(
                          """{"@type":"NGO","name":"Mall of Hope"}""")))


class TestOneSharedSubtypeSet(unittest.TestCase):
    def test_both_checks_read_the_same_set(self):
        """The second list is the bug. Keep there being one."""
        self.assertIs(entity_consistency.ENTITY_TYPES, ORGANIZATION_TYPES)

    def test_the_branch_is_the_published_one_not_our_scan_data(self):
        """Curating from what we happened to have seen just moves the false
        finding to the next prospect."""
        self.assertGreater(len(ORGANIZATION_TYPES), 150)
        self.assertTrue(SCHEMA_ORG_VERSION)
        for t in ("Organization", "LocalBusiness", "NGO", "Attorney",
                  "Dentist", "AutoRepair", "FinancialService",
                  "RealEstateAgent", "Store", "Restaurant", "Plumber",
                  "RoofingContractor", "HVACBusiness", "Electrician",
                  "EducationalOrganization", "MedicalOrganization",
                  "Corporation", "GovernmentOrganization", "SportsTeam",
                  "OnlineStore", "Hotel", "Library"):
            self.assertIn(t, ORGANIZATION_TYPES, t)

    def test_out_of_vocabulary_aliases_are_accepted_and_labelled(self):
        """NonprofitOrganization is not a schema.org class; sites ship it."""
        self.assertIn("NonprofitOrganization", ORGANIZATION_TYPES)
        self.assertIn("NonprofitOrganization", ORGANIZATION_ALIASES)
        self.assertTrue(ORGANIZATION_ALIASES.isdisjoint(
            ORGANIZATION_TYPES - ORGANIZATION_ALIASES - {"Organization"}))

    def test_non_organizations_are_not_swept_in(self):
        for t in ("WebSite", "WebPage", "BreadcrumbList", "Person", "Product",
                  "Article", "FAQPage", "ImageObject", "Service", "Place",
                  "PostalAddress", "Event"):
            self.assertNotIn(t, ORGANIZATION_TYPES, t)
        self.assertFalse(is_organization(["WebSite", "BreadcrumbList"]))

    def test_every_subtype_satisfies_the_presence_check(self):
        """Each of the 166 on its own, so no prospect hits this again."""
        for t in sorted(ORGANIZATION_TYPES):
            self.assertTrue(is_organization([t]), t)
            findings = _run_checks(
                '{"@type":"%s","name":"Example","url":"https://example.com/"}' % t)
            self.assertNotIn("missing:Organization", _keys(findings), t)


class TestBusinessTypeInference(unittest.TestCase):
    def test_a_subtype_only_site_infers_a_type(self):
        """_detect_business_type returned None on an NGO site, which shipped
        as "Could not infer a business or content type"."""
        f = _one(_run_checks(NGO_JSONLD), "structured_data", "detected_type")
        self.assertEqual(f.evidence["detected_type"], "NGO")
        self.assertIn("NGO", f.observed)
        self.assertNotIn("Could not infer", f.observed)

    def test_the_existing_precedence_list_still_wins(self):
        """The fallback is a fallback: BUSINESS_TYPES order is unchanged."""
        f = _one(_run_checks(PLUMBER_JSONLD, title="Otter Squad Plumbing",
                             h1="Otter Squad Plumbing"),
                 "structured_data", "detected_type")
        self.assertEqual(f.evidence["detected_type"], "LocalBusiness")

    def test_non_organization_schema_still_infers_nothing(self):
        f = _one(_run_checks(NO_ORG_JSONLD, title="A Blog", h1="A Blog"),
                 "structured_data", "detected_type")
        self.assertIsNone(f.evidence["detected_type"])
        self.assertIn("Could not infer", f.observed)

    def test_the_root_alone_never_beats_a_subtype(self):
        self.assertIsNone(organization_subtype(["Organization"]))
        self.assertEqual(organization_subtype(["Organization", "NGO"]), "NGO")
        self.assertIsNone(organization_subtype(["WebSite"]))


class TestProfileCategoryFollowsTheSubtype(unittest.TestCase):
    """The second half of the reported bug: the category fell back to generic,
    so plain.why called a charity "a business"."""

    def test_an_ngo_gets_a_plain_word_of_its_own(self):
        from sightline import findings as F
        page = parse_page("https://example.com/", _page_html(NGO_JSONLD))
        self.assertEqual(F.profile_from_page(page, "mallofhope.com").category,
                         "nonprofit")

    def test_both_profile_paths_still_agree(self):
        """Live path vs backfill path, the invariant test_findings_copy.py
        already protects, re-checked for the new key."""
        from sightline import findings as F
        page = parse_page("https://example.com/", _page_html(NGO_JSONLD))
        from_page = F.profile_from_page(page, "mallofhope.com")
        from_rows = F.profile_from_rows(
            [{"check_id": "structured_data",
              "evidence": {"types": ["NGO", "WebSite"]}},
             {"check_id": "entity_consistency",
              "evidence": {"name": "Mall of Hope"}}],
            "mallofhope.com")
        self.assertEqual(from_page.category, from_rows.category)
        self.assertEqual(from_page.brand, from_rows.brand)


if __name__ == "__main__":
    unittest.main()
