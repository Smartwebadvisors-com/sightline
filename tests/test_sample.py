"""The capped page sample: which URLs, and one card when several pages share a gap."""
from __future__ import annotations

import unittest

from unittest import mock

from sightline import aggregate, findings as F
from sightline.checks.base import Finding as CheckOutput
from sightline.checks.base import ScanContext
from sightline.fetch.sample import SamplePage, classify, document_locs, load_urls, pick
from sightline import pipeline


HOME = "https://ottersquad.com/"
SITEMAP = """<?xml version="1.0"?>
<urlset>
  <url><loc>https://ottersquad.com/</loc></url>
  <url><loc>https://ottersquad.com/services/drain-cleaning</loc></url>
  <url><loc>https://ottersquad.com/services</loc></url>
  <url><loc>https://ottersquad.com/service-area/livermore</loc></url>
  <url><loc>https://ottersquad.com/plumbing-pleasanton-ca</loc></url>
  <url><loc>https://ottersquad.com/blog</loc></url>
  <url><loc>https://ottersquad.com/blog/why-drains-clog</loc></url>
  <url><loc>https://ottersquad.com/blog/water-heater-lifespan</loc></url>
  <url><loc>https://ottersquad.com/blog/third-article</loc></url>
  <url><loc>https://other.example/services</loc></url>
  <url><loc>https://ottersquad.com/plumber-near-me</loc></url>
</urlset>
"""


class FakeFetch:
    def __init__(self, pages: dict[str, str]):
        self.pages = pages

    def __call__(self, url: str):
        text = self.pages.get(url)
        return type("R", (), {"ok": text is not None, "text": text or ""})()


class TestPick(unittest.TestCase):
    def test_homepage_service_town_and_two_articles(self):
        _kind, locs = document_locs(SITEMAP)
        chosen = pick(locs, HOME)
        roles = [role for role, _url in chosen]
        self.assertEqual(roles, ["home", "service", "town", "article", "article"])
        urls = dict(chosen)
        self.assertEqual(urls["service"], "https://ottersquad.com/services")
        self.assertEqual(urls["town"], "https://ottersquad.com/service-area/livermore")
        self.assertNotIn("https://ottersquad.com/blog/third-article", urls.values())
        self.assertNotIn("https://other.example/services", urls.values())

    def test_a_flat_site_still_yields_a_service_a_town_and_articles(self):
        urls = [
            "https://ottersquad.com/",
            "https://ottersquad.com/sitemap/",
            "https://ottersquad.com/plumbing-livermore/",
            "https://ottersquad.com/drain-cleaning-livermore/",
            "https://ottersquad.com/emergency-plumber-livermore/",
            "https://ottersquad.com/drain-cleaning-cost-2026/",
            "https://ottersquad.com/does-livermore-need-a-water-softener/",
            "https://ottersquad.com/about/",
        ]
        chosen = pick(urls, HOME)
        by_role = {}
        articles = []
        for role, url in chosen:
            if role == "article":
                articles.append(url)
            else:
                by_role[role] = url
        self.assertEqual(by_role["service"], "https://ottersquad.com/plumbing-livermore/")
        self.assertEqual(by_role["town"], "https://ottersquad.com/drain-cleaning-livermore/")
        self.assertEqual(articles, [
            "https://ottersquad.com/drain-cleaning-cost-2026/",
            "https://ottersquad.com/does-livermore-need-a-water-softener/",
        ])

    def test_near_me_is_a_service_page_not_a_town(self):
        self.assertEqual(classify("https://ottersquad.com/plumber-near-me"), "service")

    def test_state_suffix_is_a_town_page(self):
        self.assertEqual(classify("https://ottersquad.com/plumbing-pleasanton-ca"), "town")

    def test_index_is_not_fetched_as_a_child_forever(self):
        index = """<sitemapindex>
          <sitemap><loc>https://ottersquad.com/page-sitemap.xml</loc></sitemap>
        </sitemapindex>"""
        fetch = FakeFetch({
            "https://ottersquad.com/sitemap.xml": index,
            "https://ottersquad.com/page-sitemap.xml": SITEMAP,
        })
        urls = load_urls(HOME, None, fetch)
        self.assertIn("https://ottersquad.com/services", urls)
        self.assertNotIn("https://ottersquad.com/page-sitemap.xml", urls)


class TestOneCard(unittest.TestCase):
    def test_three_gaps_stay_one_finding_and_name_the_count(self):
        def gap(url, severity):
            return (url, CheckOutput(
                check_id="answer_first", item_key="preamble", severity=severity,
                examined="opening", observed="preamble",
                remediation="Answer first.",
            ))
        merged = aggregate.aggregate({
            ("answer_first", "preamble"): [
                gap("https://ottersquad.com/", "medium"),
                gap("https://ottersquad.com/services", "pass"),
                gap("https://ottersquad.com/service-area/livermore", "high"),
                gap("https://ottersquad.com/blog/why-drains-clog", "pass"),
                gap("https://ottersquad.com/blog/water-heater-lifespan", "medium"),
            ],
        }, pages_checked=5)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].severity, "high")
        self.assertEqual(merged[0].evidence["pages_with_gap"], 3)
        built = F.build(merged[0], F.ClientProfile(brand="The Otter Squad", category="plumbing company"))
        self.assertEqual(built.plain.title, "Answer your customers' questions on the page")
        self.assertIn("This showed up on 3 of 5 pages.", built.plain.why)

    def test_one_page_keeps_the_old_sentence(self):
        only = CheckOutput(
            check_id="answer_first", item_key="preamble", severity="medium",
            examined="opening", observed="preamble", remediation="Answer first.",
            evidence={"pages_checked": 1, "pages_with_gap": 1},
        )
        built = F.build(only, F.ClientProfile(brand="The Otter Squad", category="plumbing company"))
        self.assertNotIn("of 1 pages", built.plain.why)

    def test_search_footprint_is_not_a_page_count(self):
        only = CheckOutput(
            check_id="rank", item_key="keyword_count", severity="high",
            examined="keywords", observed="few", remediation="Publish pages.",
        )
        built = F.build(only, F.ClientProfile(brand="The Otter Squad", category="plumbing company"))
        self.assertNotIn("pages.", built.plain.why)


class TestSpeedPages(unittest.TestCase):
    def test_page_speed_is_not_run_on_an_article(self):
        seen = []

        class Speed:
            CHECK_ID = "pagespeed"

            def run(self, ctx):
                seen.append(ctx.url)
                severity = "high" if ctx.url.endswith("/services") else "pass"
                return [CheckOutput(
                    check_id="pagespeed", item_key="mobile:LCP", severity=severity,
                    examined="LCP", observed="slow" if severity == "high" else "fine",
                    remediation="Compress images.",
                )]

        pages = [
            SamplePage("home", "https://ottersquad.com/", object()),
            SamplePage("service", "https://ottersquad.com/services", object()),
            SamplePage("article", "https://ottersquad.com/blog/why-drains-clog", object()),
        ]
        speed = [page for page in pages if page.role in ("home", "service")]
        ctx = ScanContext(
            url="https://ottersquad.com/", origin="https://ottersquad.com",
            domain="ottersquad.com",
            profile=F.ClientProfile(brand="The Otter Squad", category="plumbing company"),
        )
        captured = []

        def _write(_scan_id, findings):
            captured.extend(findings)
            return len(captured)

        with mock.patch.object(pipeline.db, "write_findings", _write):
            pipeline._across_pages(1, ctx, Speed(), speed, None)
        self.assertEqual(seen, [
            "https://ottersquad.com/",
            "https://ottersquad.com/services",
        ])
        self.assertEqual(len(captured), 1)
        self.assertIn("This showed up on 1 of 2 pages.", captured[0].plain.why)
        self.assertNotIn("rank", pipeline._PAGE_CHECKS)
        self.assertEqual(pipeline._SPEED_CHECK, "pagespeed")


if __name__ == "__main__":
    unittest.main()
