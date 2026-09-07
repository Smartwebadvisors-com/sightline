"""Tests for the DataForSEO-sourced SEO score.

The properties that matter here are not the exact numbers — those are a
judgment call in the anchor tables — but the invariants that keep the
score honest:

  * an unmeasurable endpoint must never look like a failing site
  * a measured zero must never look like an unmeasurable endpoint
  * the score must be monotonic in every input
  * nothing in it may come from a finding or a deduction

The measured-fixture test at the bottom anchors the tables against a real
DataForSEO response so a future reweighting shows up as a diff here.
"""
from __future__ import annotations

import unittest

from sightline import dataforseo as dfs
from sightline.scoring import seo


def metrics(**kw):
    """The four inputs, defaulting to unmeasured."""
    base = {"ranked_keywords": None, "top10_keywords": None,
            "referring_domains": None, "domain_rank": None}
    base.update(kw)
    return base


FULL = metrics(ranked_keywords=120, top10_keywords=6,
               referring_domains=45, domain_rank=90)


class TestCoverage(unittest.TestCase):
    def test_nothing_measured_is_none_not_zero(self):
        """The whole point of the coverage rule. A DataForSEO outage must
        not publish a 0 against a site we never measured."""
        r = seo.score(metrics())
        self.assertIsNone(r["score"])
        self.assertEqual(r["covered_weight"], 0.0)
        self.assertEqual(len(r["unmeasured"]), 4)

    def test_empty_dict_is_none(self):
        self.assertIsNone(seo.score({})["score"])

    def test_measured_zero_scores_zero(self):
        """A brand-new domain measured at zero across the board IS a
        zero. It must not be excused as 'not measured'."""
        r = seo.score(metrics(ranked_keywords=0, top10_keywords=0,
                              referring_domains=0, domain_rank=0))
        self.assertEqual(r["score"], 0.0)
        self.assertEqual(r["covered_weight"], 1.0)
        self.assertEqual(r["unmeasured"], [])

    def test_partial_coverage_renormalizes(self):
        """Losing the backlinks endpoint must not halve the score. The
        remaining components carry it over their own weight only."""
        full = seo.score(FULL)
        partial = seo.score(metrics(ranked_keywords=120, top10_keywords=6))
        self.assertEqual(partial["covered_weight"], 0.55)
        self.assertEqual(partial["unmeasured"],
                         ["referring_domains", "domain_rank"])
        # Renormalized, not diluted: the kept components' own average.
        self.assertGreater(partial["score"], full["score"])
        self.assertAlmostEqual(
            partial["score"],
            round((0.25 * full["components"]["ranked_keywords"]["score"]
                   + 0.30 * full["components"]["top10_keywords"]["score"])
                  / 0.55, 1),
            places=1,
        )

    def test_unmeasured_component_reports_no_value(self):
        c = seo.score(metrics(ranked_keywords=10))["components"]
        self.assertIsNone(c["domain_rank"]["score"])
        self.assertIsNone(c["domain_rank"]["value"])
        self.assertEqual(c["ranked_keywords"]["value"], 10)


class TestBands(unittest.TestCase):
    def test_clamps_at_both_ends(self):
        self.assertEqual(seo.component_score("ranked_keywords", 0), 0.0)
        self.assertEqual(seo.component_score("ranked_keywords", 10 ** 9), 100.0)
        self.assertEqual(seo.component_score("domain_rank", 1000), 100.0)

    def test_negative_input_clamps_to_zero(self):
        self.assertEqual(seo.component_score("referring_domains", -5), 0.0)

    def test_anchors_are_exact(self):
        """Interpolation must pass through the table, or the documented
        thresholds are a lie."""
        for name, c in seo.COMPONENTS.items():
            for value, expected in c.anchors:
                self.assertAlmostEqual(
                    seo.component_score(name, value), float(expected),
                    places=1, msg=f"{name} at {value}",
                )

    def test_monotonic_in_every_component(self):
        for name in seo.COMPONENT_ORDER:
            scores = [seo.component_score(name, v)
                      for v in (0, 1, 5, 20, 75, 300, 1000, 5000)]
            self.assertEqual(scores, sorted(scores), msg=name)

    def test_score_monotonic_in_each_input(self):
        for name in seo.COMPONENT_ORDER:
            low = seo.score(metrics(**{**FULL, name: FULL[name]}))["score"]
            high = seo.score(metrics(**{**FULL, name: FULL[name] * 10}))["score"]
            self.assertGreaterEqual(high, low, msg=name)

    def test_geometric_anchors_discriminate_at_the_low_end(self):
        """Where the low-end discrimination actually comes from. The
        anchors are geometrically spaced, so 3 keywords and 30 keywords
        land in visibly different parts of the range. Evenly spaced
        anchors would flatten both to near zero."""
        a = seo.component_score("ranked_keywords", 3)
        b = seo.component_score("ranked_keywords", 30)
        self.assertGreater(b - a, 10.0)

        evenly_spaced = ((0, 0), (1000, 20), (2000, 40), (3000, 60),
                         (4000, 80), (5000, 100))
        flattened = (seo._interpolate(30, evenly_spaced, False)
                     - seo._interpolate(3, evenly_spaced, False))
        self.assertLess(flattened, 1.0)

    def test_log_interpolation_is_second_order(self):
        """Guards the magnitude claim in seo.py's docstring. Log space
        smooths the curve between anchors; it is NOT what separates a
        dead site from a modest one. If a future reweighting makes it
        load-bearing, this test should fail and the docstring should be
        rewritten — not the other way round."""
        anchors = seo.COMPONENTS["ranked_keywords"].anchors
        for value in (3, 30, 120, 625, 3000):
            delta = abs(seo._interpolate(value, anchors, True)
                        - seo._interpolate(value, anchors, False))
            self.assertLess(delta, 8.0, msg=f"at {value}")

    def test_domain_rank_is_not_log_scaled(self):
        self.assertFalse(seo.COMPONENTS["domain_rank"].log_scale)
        # Midpoint of the 300->500 anchor span lands halfway on a linear
        # scale; a log scale would pull it above 70.
        self.assertAlmostEqual(seo.component_score("domain_rank", 400),
                               70.0, places=1)


class TestWeights(unittest.TestCase):
    def test_weights_sum_to_one(self):
        self.assertAlmostEqual(seo.TOTAL_WEIGHT, 1.0, places=6)

    def test_top10_is_weighted_highest(self):
        """It is the only outcome metric; the rest are inputs."""
        heaviest = max(seo.COMPONENTS.items(), key=lambda kv: kv[1].weight)
        self.assertEqual(heaviest[0], "top10_keywords")

    def test_domain_rank_under_referring_domains(self):
        """Both read the link graph; domain_rank must not double-count it."""
        self.assertLess(seo.COMPONENTS["domain_rank"].weight,
                        seo.COMPONENTS["referring_domains"].weight)

    def test_every_ordered_component_exists(self):
        self.assertEqual(sorted(seo.COMPONENT_ORDER),
                         sorted(seo.COMPONENTS))

    def test_version_is_recorded_on_every_result(self):
        self.assertEqual(seo.score(FULL)["version"], seo.SEO_SCORE_VERSION)
        self.assertEqual(seo.score(metrics())["version"], seo.SEO_SCORE_VERSION)

    def test_raw_metrics_round_trip(self):
        """Stored raws are what make a future seo-v2 rescore possible
        without re-buying the API calls."""
        self.assertEqual(seo.score(FULL)["raw"], FULL)


class TestMetricsExtraction(unittest.TestCase):
    """dataforseo.metrics_from() — the single definition of how a
    response becomes the four numbers, shared by scan and backfill."""

    # Trimmed from the real domain_rank_overview response for
    # smartwebadvisors.com.
    OVERVIEW = {
        "items": [{"metrics": {"organic": {
            "pos_1": 0, "pos_2_3": 0, "pos_4_10": 0, "pos_11_20": 0,
            "pos_21_30": 0, "pos_31_40": 0, "pos_41_50": 1, "pos_51_60": 1,
            "pos_61_70": 2, "pos_71_80": 1, "pos_81_90": 3, "pos_91_100": 0,
            "etv": 1.26, "count": 8, "is_lost": 1,
        }}}]
    }
    BACKLINKS = {"target": "smartwebadvisors.com", "rank": 0,
                 "backlinks": 39, "referring_domains": 36,
                 "referring_main_domains": 35}

    def ok(self, endpoint, result):
        return dfs.DFSResult(endpoint=endpoint, http_status=200,
                             task_status=dfs.TASK_OK, result=result)

    def test_extracts_all_four(self):
        m = dfs.metrics_from(
            self.ok(dfs.EP_RANK_OVERVIEW, self.OVERVIEW),
            self.ok(dfs.EP_BACKLINKS, self.BACKLINKS),
        )
        self.assertEqual(m["ranked_keywords"], 8)
        self.assertEqual(m["top10_keywords"], 0)
        self.assertEqual(m["referring_domains"], 36)
        self.assertEqual(m["domain_rank"], 0)

    def test_top10_sums_the_page_one_bands_only(self):
        overview = {"items": [{"metrics": {"organic": {
            "pos_1": 2, "pos_2_3": 3, "pos_4_10": 7, "pos_11_20": 50,
            "count": 62,
        }}}]}
        m = dfs.metrics_from(self.ok(dfs.EP_RANK_OVERVIEW, overview),
                             self.ok(dfs.EP_BACKLINKS, self.BACKLINKS))
        self.assertEqual(m["top10_keywords"], 12)

    def test_top10_never_exceeds_total(self):
        """Guaranteed by taking both from one endpoint. If this ever
        fails, the two counts have been re-split across endpoints."""
        m = dfs.metrics_from(self.ok(dfs.EP_RANK_OVERVIEW, self.OVERVIEW),
                             self.ok(dfs.EP_BACKLINKS, self.BACKLINKS))
        self.assertLessEqual(m["top10_keywords"], m["ranked_keywords"])

    def test_unauthorized_endpoint_yields_none_not_zero(self):
        denied = dfs.DFSResult(endpoint=dfs.EP_BACKLINKS, http_status=403,
                               task_status=40300, task_message="Forbidden")
        m = dfs.metrics_from(self.ok(dfs.EP_RANK_OVERVIEW, self.OVERVIEW), denied)
        self.assertIsNone(m["referring_domains"])
        self.assertIsNone(m["domain_rank"])
        self.assertTrue(m["sources"][dfs.EP_BACKLINKS]["unauthorized"])
        self.assertFalse(m["sources"][dfs.EP_BACKLINKS]["ok"])

    def test_answered_but_empty_is_a_measured_zero(self):
        """A domain with no organic presence at all. The endpoint DID
        answer, so this is data, not a coverage gap."""
        m = dfs.metrics_from(self.ok(dfs.EP_RANK_OVERVIEW, {"items": []}),
                             self.ok(dfs.EP_BACKLINKS, {}))
        self.assertEqual(m["ranked_keywords"], 0)
        self.assertEqual(m["top10_keywords"], 0)
        self.assertEqual(m["referring_domains"], 0)
        self.assertEqual(m["domain_rank"], 0)
        self.assertEqual(seo.score(m)["score"], 0.0)

    def test_empty_result_array_is_still_a_successful_task(self):
        """DataForSEO can return an empty result array for a domain it has
        nothing on. That must read as zeros, not as an unavailable
        endpoint — otherwise the worst prospects get excused from the
        metric they fail hardest, and rank.py stops emitting the
        '0 ranked keywords' finding that is the whole pitch."""
        empty = dfs.DFSResult(endpoint=dfs.EP_RANK_OVERVIEW, http_status=200,
                              task_status=dfs.TASK_OK, result=None)
        self.assertTrue(empty.ok)
        m = dfs.metrics_from(empty, empty)
        self.assertEqual(m["ranked_keywords"], 0)
        self.assertEqual(m["referring_domains"], 0)
        self.assertEqual(seo.score(m)["score"], 0.0)

    def test_transport_failure_is_not_a_zero(self):
        down = dfs.DFSResult(endpoint=dfs.EP_RANK_OVERVIEW,
                             error="connection timed out")
        m = dfs.metrics_from(down, down)
        self.assertIsNone(seo.score(m)["score"])
        self.assertFalse(m["sources"][dfs.EP_RANK_OVERVIEW]["unauthorized"])
        self.assertIn("timed out", m["sources"][dfs.EP_RANK_OVERVIEW]["reason"])

    def test_all_position_bands_persisted(self):
        m = dfs.metrics_from(self.ok(dfs.EP_RANK_OVERVIEW, self.OVERVIEW),
                             self.ok(dfs.EP_BACKLINKS, self.BACKLINKS))
        positions = m["sources"][dfs.EP_RANK_OVERVIEW]["positions"]
        self.assertEqual(set(positions), set(dfs.POSITION_BANDS))
        self.assertEqual(positions["pos_81_90"], 3)


class TestMeasuredFixture(unittest.TestCase):
    """Anchors the tables to a real measurement. A reweighting is allowed
    to change these numbers — it must do so visibly, here."""

    def test_smartwebadvisors_scores_as_calibrated(self):
        r = seo.score(metrics(ranked_keywords=8, top10_keywords=0,
                              referring_domains=36, domain_rank=0))
        self.assertEqual(r["score"], 15.9)
        c = r["components"]
        self.assertEqual(c["ranked_keywords"]["score"], 18.3)
        self.assertEqual(c["top10_keywords"]["score"], 0.0)
        self.assertEqual(c["referring_domains"]["score"], 45.2)
        self.assertEqual(c["domain_rank"]["score"], 0.0)

    def test_calibration_ladder(self):
        cases = [
            (metrics(ranked_keywords=0, top10_keywords=0,
                     referring_domains=0, domain_rank=0), 0.0),
            (metrics(ranked_keywords=8, top10_keywords=0,
                     referring_domains=36, domain_rank=0), 15.9),
            (metrics(ranked_keywords=120, top10_keywords=6,
                     referring_domains=45, domain_rank=90), 43.1),
            (metrics(ranked_keywords=1400, top10_keywords=95,
                     referring_domains=320, domain_rank=250), 75.3),
        ]
        for m, expected in cases:
            self.assertEqual(seo.score(m)["score"], expected, msg=str(m))


class TestIndependenceFromFindings(unittest.TestCase):
    def test_scoring_module_never_reads_findings_or_deductions(self):
        """The explicit constraint on this feature: the SEO score is
        measured, not deducted."""
        import pathlib
        src = (pathlib.Path(seo.__file__).read_text(encoding="utf-8")
               .split('"""', 2)[-1])  # skip the docstring
        for banned in ("sightline_scores", "sightline_findings", "deduction",
                       "Finding", "compute_report", "db."):
            self.assertNotIn(banned, src, msg=f"seo.py references {banned}")


if __name__ == "__main__":
    unittest.main()


class TestRenderSection(unittest.TestCase):
    """The HTML section. Pure string assembly over a scan row, so the
    branches real data can't currently reach are testable here."""

    def section(self, seo_score, seo_metrics):
        from sightline.render.html import _seo_section
        return "".join(_seo_section({"seo_score": seo_score,
                                     "seo_metrics": seo_metrics}))

    def scored(self):
        from sightline.render.html import _seo_section
        return "".join(_seo_section(
            {"seo_score": 43.1, "seo_metrics": seo.score(FULL)}))

    def test_renders_every_component(self):
        out = self.scored()
        for label in ("Ranked keywords", "Top-10 keywords",
                      "Referring domains", "Domain rank"):
            self.assertIn(label, out)
        self.assertIn("43 / 100", out)
        self.assertIn("seo-v1", out)

    def test_shows_raw_value_and_weight_not_just_the_subscore(self):
        """The point of the section: three numbers exist per component."""
        out = self.scored()
        self.assertIn("weight 0.3", out)   # top10, trailing zero trimmed
        self.assertIn("weight 0.25", out)
        self.assertIn("120", out)          # ranked_keywords raw value

    def test_not_measured_is_not_a_zero(self):
        out = self.section(None, {})
        self.assertIn("not measured", out)
        self.assertIn("not a score of zero", out)
        self.assertNotIn("seo-grid", out)

    def test_measured_zero_still_renders_the_grid(self):
        zeros = seo.score(metrics(ranked_keywords=0, top10_keywords=0,
                                  referring_domains=0, domain_rank=0))
        out = self.section(0.0, zeros)
        self.assertIn("seo-grid", out)
        self.assertIn("0 / 100", out)
        self.assertNotIn("not a score of zero", out)

    def test_partial_coverage_names_what_is_missing(self):
        partial = seo.score(metrics(ranked_keywords=120, top10_keywords=6))
        out = self.section(partial["score"], partial)
        self.assertIn("55% of weight measured", out)
        self.assertIn("Referring domains", out)
        self.assertIn("&mdash;", out)          # the unmeasured tile
        self.assertIn("not measured", out)

    def test_backfilled_score_discloses_when_it_was_measured(self):
        m = seo.score(FULL)
        m["backfilled"] = True
        m["measured_at"] = "2026-09-07T21:00:00+00:00"
        out = self.section(m["score"], m)
        self.assertIn("measured 2026-09-07, after this scan ran", out)

    def test_live_score_makes_no_provenance_claim(self):
        self.assertNotIn("after this scan ran", self.scored())

    def test_component_order_is_canonical_not_jsonb_order(self):
        """Postgres jsonb sorts keys, so the renderer must impose order."""
        m = seo.score(FULL)
        m["components"] = dict(reversed(list(m["components"].items())))
        out = self.section(m["score"], m)
        positions = [out.index(seo.COMPONENTS[n].label)
                     for n in seo.COMPONENT_ORDER]
        self.assertEqual(positions, sorted(positions))

    def test_unknown_component_from_another_version_still_renders(self):
        m = seo.score(FULL)
        m["components"]["local_pack"] = {"label": "Local pack", "weight": 0.1,
                                         "value": 3, "score": 40.0}
        out = self.section(m["score"], m)
        self.assertIn("Local pack", out)

    def test_labels_are_escaped(self):
        m = seo.score(FULL)
        m["components"]["ranked_keywords"]["label"] = "<script>x</script>"
        out = self.section(m["score"], m)
        self.assertNotIn("<script>", out)
        self.assertIn("&lt;script&gt;", out)

    def test_does_not_emit_dimension_tile_classes(self):
        """Cited's dashboard parser scrapes dim-label/dim-score pairs into
        the six AEO dimensions. The SEO tiles must not be picked up as a
        seventh dimension, so they use their own class names."""
        out = self.scored()
        self.assertNotIn("dim-label", out)
        self.assertNotIn("dim-score", out)
        self.assertIn("seo-label", out)
        self.assertIn("seo-score", out)
