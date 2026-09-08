"""COPY.md, enforced. The judgement rules (6, 7) still need review; these
catch the mechanical violations, which are the ones that actually ship."""
from __future__ import annotations

import re
import unittest

from sightline import findings as F
from sightline.checks.base import Finding as CheckOutput
from sightline.pipeline import CHECKS

PROFILE = F.ClientProfile(brand="Otter Squad Plumbing",
                          category="plumbing company")

# Rule 2: formats, specs and markup languages. Rule 1: acronyms we use
# internally that mean nothing to a client.
BANNED_IN_PLAIN = [
    "json-ld", "json", "schema.org", "robots.txt", "llms.txt", "sitemap",
    "html", "xml", "rel=canonical", "canonical", "meta tag", "markup",
    "http", "aeo", "geo", "cwv", "psi", "nap", "ld+json",
    "structured data", "lighthouse", "core web vitals",
]


def _all_findings():
    for check_id in F.PLAIN_GAP:
        for severity in ("critical", "pass", "unavailable"):
            yield F.build(CheckOutput(
                check_id=check_id, severity=severity,
                examined="Examined something", observed="Observed something",
                item_key="", evidence={}), PROFILE)


class TestEveryFindingHasBothBlocks(unittest.TestCase):
    def test_every_check_in_the_pipeline_has_gap_copy_and_a_task(self):
        """A check that ships without plain copy would raise mid-scan."""
        for mod in CHECKS:
            self.assertIn(mod.CHECK_ID, F.PLAIN_GAP, mod.CHECK_ID)
            self.assertIn(mod.CHECK_ID, F.TASK_GAP, mod.CHECK_ID)
            self.assertIn(mod.CHECK_ID, F.SUBJECT, mod.CHECK_ID)

    def test_build_produces_both_blocks_in_one_call(self):
        for f in _all_findings():
            self.assertTrue(f.technical.title and f.technical.detail)
            self.assertTrue(f.plain.title and f.plain.why
                            and f.plain.do and f.plain.payoff)

    def test_unknown_check_raises_rather_than_shipping_half_a_finding(self):
        with self.assertRaises(KeyError):
            F.build(CheckOutput(check_id="brand_new_check", severity="high",
                                examined="x", observed="y"), PROFILE)


class TestPlainCopyRules(unittest.TestCase):
    def test_plain_names_no_format_or_bare_acronym(self):
        """Rules 1 and 2."""
        for f in _all_findings():
            blob = " ".join([f.plain.title, f.plain.why, f.plain.do,
                             f.plain.payoff]).lower()
            for banned in BANNED_IN_PLAIN:
                self.assertNotIn(banned, blob,
                                 f"{f.check_id}/{f.outcome}: {banned!r}")

    def test_plain_titles_are_not_negations(self):
        """Rule 6: a title names an action, it does not correct a belief."""
        for f in _all_findings():
            t = f.plain.title.lower()
            for shape in ("you are not", "you're not", "it is not",
                          "isn't", "aren't", "no need to worry",
                          "contrary to"):
                self.assertNotIn(shape, t, f"{f.check_id}: {f.plain.title}")

    def test_why_names_the_brand_and_category(self):
        """Interpolation, so plain.why cannot read generic."""
        for f in _all_findings():
            self.assertIn(PROFILE.brand, f.plain.why, f.check_id)
        for f in _all_findings():
            if f.outcome == F.GAP:
                self.assertIn(PROFILE.category, f.plain.why, f.check_id)

    def test_possessive_survives_a_brand_ending_in_s(self):
        """Regression: {brand}'s produced "Smart Web Advisors's"."""
        prof = F.ClientProfile(brand="Smart Web Advisors",
                               category="professional services firm")
        for check_id in F.PLAIN_GAP:
            f = F.build(CheckOutput(check_id=check_id, severity="critical",
                                    examined="x", observed="y"), prof)
            blob = " ".join([f.plain.title, f.plain.why, f.plain.do,
                             f.plain.payoff])
            self.assertNotIn("s's", blob, check_id)
        self.assertEqual(F._possessive("Smart Web Advisors"),
                         "Smart Web Advisors'")
        self.assertEqual(F._possessive("Otter Squad"), "Otter Squad's")

    def test_no_template_placeholder_survives(self):
        for f in _all_findings():
            blob = " ".join([f.plain.title, f.plain.why, f.plain.do,
                             f.plain.payoff])
            self.assertNotRegex(blob, r"\{[a-z_]+\}")


class TestTaskFields(unittest.TestCase):
    def test_impact_effort_and_owner_are_three_values(self):
        """Rule 4. No fused field, and severity is not one of them."""
        for f in _all_findings():
            self.assertIn(f.impact, F.IMPACTS)
            self.assertIsInstance(f.effort_minutes, int)
            self.assertIn(f.owner, F.OWNERS)

    def test_no_lift_style_fused_field_exists(self):
        for name in ("lift", "priority", "quick_win"):
            self.assertNotIn(name, F.Finding.__dataclass_fields__)

    def test_unmeasured_findings_are_ours_not_the_clients(self):
        f = F.build(CheckOutput(check_id="pagespeed", severity="unavailable",
                                examined="x", observed="y"), PROFILE)
        self.assertEqual(f.owner, "swa")
        self.assertIn("We re-run", f.plain.title)

    def test_swa_ownership_survives_a_passing_measurement(self):
        """Regression: prompt_testing emits severity='info', which maps to the
        PASS outcome. Ownership must be resolved from the check before
        outcome, or the one task we perform ships as a client instruction
        with generic pass copy."""
        for severity in ("info", "pass", "unavailable", "high"):
            f = F.build(CheckOutput(check_id="prompt_testing",
                                    severity=severity, examined="x",
                                    observed="y"), PROFILE)
            self.assertEqual(f.owner, "swa", severity)
            self.assertEqual(f.effort_minutes, 90, severity)
            self.assertTrue(f.plain.title.startswith("We "), severity)
            self.assertTrue(f.plain.do.startswith("We "), severity)

    def test_client_checks_still_get_outcome_driven_copy(self):
        """The ownership short-circuit must not leak gap copy onto a pass."""
        f = F.build(CheckOutput(check_id="pagespeed", severity="pass",
                                examined="x", observed="y"), PROFILE)
        self.assertEqual(f.owner, "client")
        self.assertIn("Keep", f.plain.title)
        self.assertNotIn("Speed up", f.plain.title)

    def test_prompt_testing_is_one_finding_that_we_own(self):
        """It was two client instructions; it is now one task we perform."""
        self.assertEqual(F.TASK_GAP["prompt_testing"].owner, "swa")
        self.assertTrue(F.PLAIN_GAP["prompt_testing"].title.startswith("We "))
        self.assertTrue(F.PLAIN_GAP["prompt_testing"].do.startswith("We "))

    def test_stored_task_fields_win_over_the_tables(self):
        """A finding sold at 15 minutes stays 15 minutes."""
        row = {"check_id": "pagespeed", "item_key": "", "severity": "high",
               "examined": "x", "observed": "y", "remediation": "",
               "evidence": {}, "impact": "low", "effort_minutes": 15,
               "owner": "client", "deduction": 3.0}
        f = F.from_row(row, PROFILE)
        self.assertEqual((f.impact, f.effort_minutes), ("low", 15))
        self.assertEqual(f.deduction, 3.0)


class TestScorePresentation(unittest.TestCase):
    def test_score_carries_its_scale_and_band(self):
        """Rule 5, first half."""
        self.assertEqual(F.score_phrase(62), "62 out of 100 — mixed")
        self.assertEqual(F.score_phrase(0), "0 out of 100 — critical gaps")
        self.assertEqual(F.score_phrase(None), "not measured")

    def test_peer_line_is_silent_without_enough_peers(self):
        """Rule 5, second half: no comparison we cannot evidence."""
        self.assertEqual(F.peer_sentence(62, None), "")
        self.assertEqual(
            F.peer_sentence(62, {"n_domains": 4, "median": 55.0}), "")
        self.assertIn(
            "above the median of 55 for the 9 other sites",
            F.peer_sentence(62, {"n_domains": 9, "median": 55.0}))

    def test_one_disclaimer_mentions_the_brand_and_no_acronym(self):
        d = F.disclaimer(PROFILE).lower()
        self.assertIn("otter squad plumbing", d)
        for banned in ("aeo", "cwv", "psi", "json"):
            self.assertNotIn(banned, d)


class TestExportShape(unittest.TestCase):
    def test_export_keeps_impact_and_effort_separate(self):
        """Rule 4 at the boundary: three keys, and no fused phrase anywhere
        in the serialized payload."""
        import json
        from sightline.render import json_export

        f = F.build(CheckOutput(check_id="ai_crawler", severity="critical",
                                examined="x", observed="y"), PROFILE)
        entry = {"check_id": f.check_id, "item_key": f.item_key,
                 "outcome": f.outcome,
                 "plain": {"title": f.plain.title, "why": f.plain.why,
                           "do": f.plain.do, "payoff": f.plain.payoff},
                 "impact": f.impact, "effort_minutes": f.effort_minutes,
                 "owner": f.owner}
        self.assertEqual(
            sorted(entry), ["check_id", "effort_minutes", "impact",
                            "item_key", "outcome", "owner", "plain"])
        blob = json.dumps(entry).lower()
        for fused in ("quick win", "high lift", "low effort high",
                      "high impact low"):
            self.assertNotIn(fused, blob)
        # technical must not reach Cited at all.
        self.assertNotIn("technical", entry)
        self.assertIn("plain", json_export.__doc__)


class TestReportDisclaimerDiscipline(unittest.TestCase):
    """Rule 8 at the renderer, not just in the copy module. Regression: the
    new disclaimer was added under the score block while the old three-hedge
    block was left at the bottom, so the report shipped two."""

    @staticmethod
    def _emitted_source() -> str:
        """html.py with comment lines stripped. The comments deliberately
        quote the hedges they removed, which is documentation, not output."""
        import inspect
        from sightline.render import html as html_mod
        return "\n".join(
            line for line in inspect.getsource(html_mod).splitlines()
            if not line.strip().startswith("#"))

    def test_report_states_one_disclaimer_once(self):
        src = self._emitted_source()
        self.assertEqual(src.count("class='disclaimer'>"), 1)
        for hedge in ("How to read this report", "not as ranks",
                      "third-party", "free to disagree", "not a rank"):
            self.assertNotIn(hedge, src, hedge)

    def test_renderer_writes_no_client_facing_copy_of_its_own(self):
        """The renderer may label fields; it may not author plain sentences."""
        src = self._emitted_source()
        self.assertIn("findings_mod.disclaimer(profile)", src)
        self.assertIn("findings_mod.score_phrase", src)
        self.assertIn("findings_mod.peer_sentence", src)


class TestProfileResolutionPathsAgree(unittest.TestCase):
    """Regression: the live path and the backfill path resolved category
    differently for the same site, so a rescan silently changed the client's
    copy. They read different sources (a parsed page vs stored evidence) and
    must still agree."""

    TYPES = ["Article", "ImageObject", "Organization", "Person", "Place",
             "ProfessionalService", "WebPage", "WebSite"]

    def test_both_paths_pick_the_same_category(self):
        class FakePage:
            title = "Smart Web Advisors | Marketing"
            jsonld = [{"@type": "Organization", "name": "Smart Web Advisors"},
                      {"@type": "ProfessionalService",
                       "name": "Smart Web Advisors"}]
        from_page = F.profile_from_page(FakePage(), "smartwebadvisors.com")
        from_rows = F.profile_from_rows(
            [{"check_id": "structured_data", "evidence": {"types": self.TYPES}},
             {"check_id": "entity_consistency",
              "evidence": {"name": "Smart Web Advisors"}}],
            "smartwebadvisors.com")
        self.assertEqual(from_page.category, from_rows.category)
        self.assertEqual(from_page.brand, from_rows.brand)
        self.assertEqual(from_rows.category, "professional services firm")

    def test_specific_type_beats_generic_regardless_of_sort_order(self):
        """'Organization' sorts before 'Plumber' but must not win."""
        p = F.profile_from_rows(
            [{"check_id": "structured_data",
              "evidence": {"types": ["Organization", "Plumber", "WebSite"]}}],
            "example.com")
        self.assertEqual(p.category, "plumbing company")

    def test_unknown_types_fall_back_to_business(self):
        p = F.profile_from_rows(
            [{"check_id": "structured_data",
              "evidence": {"types": ["WebSite", "BreadcrumbList"]}}],
            "example.com")
        self.assertEqual(p.category, "business")


class TestCitedScrapingCoupling(unittest.TestCase):
    """Cited parses Sightline's HTML report and defaults severity to "info"
    when its regex misses. "info" maps to status 'pass' and is filtered out
    of the recommendation list, so a missed match does not error -- it tells
    every Cited client their site is clean.

    These tests use Cited's ACTUAL regexes, copied from
    /opt/cited/src/lib/sightline/dashboard.ts, so a class rename here fails
    here instead of silently in Cited. Delete this class only when Cited
    reads /report/<id>/findings.json instead of scraping.
    """

    # dashboard.ts:81
    SEV_RE = re.compile(r"""class=['"]sev['"][^>]*>([\s\S]*?)</span>""")

    def _finding(self, severity="high"):
        from sightline.render.html import _task_meta
        return _task_meta(F.build(CheckOutput(
            check_id="ai_crawler", severity=severity,
            examined="x", observed="y"), PROFILE))

    def test_severity_is_still_scrapable(self):
        for severity in ("critical", "high", "medium", "low", "pass",
                         "info", "unavailable"):
            m = self.SEV_RE.search(self._finding(severity))
            self.assertIsNotNone(m, f"{severity}: Cited would default to info")
            self.assertEqual(m.group(1).strip(), severity)

    def test_impact_pill_does_not_satisfy_the_severity_regex(self):
        """The impact pill is a different axis; it must not be mistaken for
        severity, or Cited would score business impact as severity."""
        from sightline.render.html import _impact_pill
        self.assertIsNone(self.SEV_RE.search(_impact_pill("high")))

    def test_the_reason_is_documented_where_someone_would_delete_it(self):
        from sightline.render.html import _task_meta
        self.assertIn("Cited", _task_meta.__doc__)
        self.assertIn("findings.json", _task_meta.__doc__)
