"""COPY.md, enforced. The judgement rules (6, 7) still need review; these
catch the mechanical violations, which are the ones that actually ship."""
from __future__ import annotations

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
        """Rule 4. No fused field, and severity is not one of them.

        Unmeasured findings are the one exception, and it is rule 10, not a
        loophole: there is no work, so there is no impact to rate and no
        effort to quote. The owner is still required.
        """
        for f in _all_findings():
            self.assertIn(f.owner, F.OWNERS)
            if f.outcome == F.UNMEASURED:
                self.assertIsNone(f.impact, f.check_id)
                self.assertIsNone(f.effort_minutes, f.check_id)
            else:
                self.assertIn(f.impact, F.IMPACTS)
                self.assertIsInstance(f.effort_minutes, int)

    def test_no_lift_style_fused_field_exists(self):
        for name in ("lift", "priority", "quick_win"):
            self.assertNotIn(name, F.Finding.__dataclass_fields__)

    def test_unmeasured_findings_are_ours_not_the_clients(self):
        f = F.build(CheckOutput(check_id="pagespeed", severity="unavailable",
                                examined="x", observed="y"), PROFILE)
        self.assertEqual(f.owner, "swa")
        self.assertEqual(f.plain.title, "Your page speed")
        self.assertIn("was not measured", f.plain.why)
        self.assertNotIn("One of the readings", f.plain.why)

    def test_swa_ownership_survives_a_passing_measurement(self):
        """Regression: prompt_testing emits severity='info', which maps to the
        PASS outcome. Ownership must be resolved from the check before
        outcome, or the one task we perform ships as a client instruction
        with generic pass copy."""
        for severity in ("info", "pass", "high"):
            f = F.build(CheckOutput(check_id="prompt_testing",
                                    severity=severity, examined="x",
                                    observed="y"), PROFILE)
            self.assertEqual(f.owner, "swa", severity)
            self.assertEqual(f.effort_minutes, 90, severity)
            self.assertTrue(f.plain.title.startswith("We "), severity)
            self.assertTrue(f.plain.do.startswith("We "), severity)

    def test_a_measurement_we_failed_to_take_is_not_the_standing_task(self):
        """Rule 10 outranks the ownership short-circuit above.

        Ownership still holds — unmeasured work is ours either way — but a
        prompt_testing run we could not complete must not ship as the full
        90-minute task described as though we had done it. That is the
        standing copy attached to a measurement that never happened.
        """
        f = F.build(CheckOutput(check_id="prompt_testing",
                                severity="unavailable", examined="x",
                                observed="y"), PROFILE)
        self.assertEqual(f.owner, "swa")
        self.assertIsNone(f.impact)
        self.assertIsNone(f.effort_minutes)
        self.assertEqual(f.plain.title, "Our visibility testing")
        self.assertIn("was not measured", f.plain.why)
        self.assertNotIn("fixed set of buyer questions", f.plain.do)

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

    def test_stored_effort_and_owner_win_impact_follows_severity(self):
        """A finding sold at 15 minutes stays 15 minutes. The stored impact
        does not: this row is severity high with impact 'low' left over from
        when every pagespeed gap was medium and a backfill could say low."""
        row = {"check_id": "pagespeed", "item_key": "", "severity": "high",
               "examined": "x", "observed": "y", "remediation": "",
               "evidence": {}, "impact": "low", "effort_minutes": 15,
               "owner": "client", "deduction": 3.0}
        f = F.from_row(row, PROFILE)
        self.assertEqual((f.impact, f.effort_minutes, f.owner),
                         ("high", 15, "client"))
        self.assertEqual(f.deduction, 3.0)

    def test_impact_follows_how_bad_the_reading_is(self):
        """The same check can rank two readings differently."""
        def impact(check, severity, item=""):
            return F.build(CheckOutput(
                check_id=check, item_key=item, severity=severity,
                examined="x", observed="y"), PROFILE).impact

        self.assertEqual(impact("pagespeed", "high", "mobile:LCP"), "high")
        self.assertEqual(impact("pagespeed", "medium", "mobile:LCP"), "medium")
        self.assertEqual(impact("pagespeed", "low", "mobile:LCP"), "low")
        self.assertEqual(impact("pagespeed", "pass", "mobile:CLS"), "low")
        self.assertEqual(impact("answer_first", "low"), "low")
        self.assertEqual(impact("answer_first", "high"), "high")
        self.assertEqual(impact("llms_txt", "medium"), "medium")
        self.assertEqual(impact("llms_txt", "low"), "low")
        self.assertEqual(impact("prompt_testing", "info"), "low")
        self.assertNotEqual(
            impact("pagespeed", "high", "desktop:TBT"),
            impact("pagespeed", "medium", "mobile:TBT"))

    def test_desktop_gap_does_not_use_the_phone_title(self):
        phone = F.build(CheckOutput(
            check_id="pagespeed", item_key="mobile:TBT", severity="high",
            examined="x", observed="y"), PROFILE)
        desk = F.build(CheckOutput(
            check_id="pagespeed", item_key="desktop:TBT", severity="high",
            examined="x", observed="y"), PROFILE)
        self.assertIn("phone", phone.plain.title.lower())
        self.assertIn("phone", phone.plain.why.lower())
        self.assertIn("computer", desk.plain.title.lower())
        self.assertIn("computer", desk.plain.why.lower())
        self.assertNotIn("phone", desk.plain.title.lower())
        self.assertNotIn("phone", desk.plain.why.lower())
        # A desktop pass is still a pass, not a speed-up task.
        passed = F.build(CheckOutput(
            check_id="pagespeed", item_key="desktop:CLS", severity="pass",
            examined="x", observed="y"), PROFILE)
        self.assertIn("Keep", passed.plain.title)
        self.assertNotIn("computer", passed.plain.title.lower())

    def test_unmeasured_plain_names_the_failed_reading(self):
        desktop = F.build(CheckOutput(
            check_id="pagespeed", item_key="desktop:error",
            severity="unavailable", examined="x", observed="y"), PROFILE)
        mobile = F.build(CheckOutput(
            check_id="pagespeed", item_key="mobile:error",
            severity="unavailable", examined="x", observed="y"), PROFILE)
        links = F.build(CheckOutput(
            check_id="rank", item_key="endpoint:backlinks",
            severity="unavailable", examined="x", observed="y"), PROFILE)
        self.assertEqual(desktop.plain.title, "Page speed on a computer")
        self.assertEqual(mobile.plain.title, "Page speed on a phone")
        self.assertEqual(links.plain.title, "Which other sites link to you")
        for f in (desktop, mobile, links):
            self.assertIn(f.plain.title, f.plain.why)
            self.assertIn("was not measured", f.plain.why)
            self.assertNotIn("One of the readings", f.plain.why)
            self.assertNotEqual(f.plain.title, "Not measured this scan")


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


class TestPeerMedianIsCached(unittest.TestCase):
    """apply_to_scan is the only place overall is produced, so it is the only
    place that can cache it. Without the cache write, peer_overall falls back
    to one compute_report() per peer domain per render (~3.5s, growing with
    scan history), or silently suppresses the peer line because the cache is
    empty. Neither failure is visible from the report."""

    def test_apply_to_scan_caches_the_overall_it_computed(self):
        from sightline.scoring import apply as apply_mod

        calls = []
        fake_report = {"overall": 61.5, "dimensions": {}, "dim_data": {},
                       "unmapped_findings": [], "coverage": {}}

        class FakeDb:
            @staticmethod
            def findings_for_scan(scan_id):
                return [{"id": 1, "check_id": "ai_crawler", "severity": "high"}]
            @staticmethod
            def write_scores(rows):
                pass
            @staticmethod
            def write_scan_overall(scan_id, wv_id, overall):
                calls.append((scan_id, wv_id, overall))

        real_db, real_report = apply_mod.db, apply_mod.report_mod
        try:
            apply_mod.db = FakeDb
            apply_mod.report_mod = type("R", (), {
                "compute_report": staticmethod(lambda *a: fake_report)})
            out = apply_mod.apply_to_scan(7, {"deductions": {}}, 3)
        finally:
            apply_mod.db, apply_mod.report_mod = real_db, real_report

        self.assertEqual(calls, [(7, 3, 61.5)],
                         "apply_to_scan must cache the overall it computed")
        self.assertIs(out, fake_report, "return value must be unchanged")

    def test_a_null_overall_is_cached_rather_than_skipped(self):
        """'scored zero' and 'never scored' must stay distinguishable."""
        from sightline.scoring import apply as apply_mod
        calls = []

        class FakeDb:
            @staticmethod
            def findings_for_scan(scan_id):
                return []
            @staticmethod
            def write_scores(rows):
                pass
            @staticmethod
            def write_scan_overall(scan_id, wv_id, overall):
                calls.append(overall)

        real_db, real_report = apply_mod.db, apply_mod.report_mod
        try:
            apply_mod.db = FakeDb
            apply_mod.report_mod = type("R", (), {
                "compute_report": staticmethod(lambda *a: {"overall": None})})
            apply_mod.apply_to_scan(9, {"deductions": {}}, 3)
        finally:
            apply_mod.db, apply_mod.report_mod = real_db, real_report
        self.assertEqual(calls, [None])
