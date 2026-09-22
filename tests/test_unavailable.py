"""COPY.md rule 10: a failed measurement is never a finding.

Two failures, both of which used to reach a client as text nobody could
act on:

  * the page will not load, and `str(requests_exception)` gets rendered;
  * a third-party check fails, and it becomes a finding with a severity, a
    price and a fix — including, via Cited's scraper, a recommendation
    reading "Fix the finding on the page, then rescan in Sightline."

Every test here is offline. The renderer and the exporter are driven
through fake db modules, so the suite makes no network call and touches no
database. (`test_render_every_stored_scan.py` is the read-only companion
that runs the same assertions over the real data.)
"""
from __future__ import annotations

import datetime as dt
import json
import re
import unittest
from types import SimpleNamespace
from unittest import mock

import requests

from sightline import findings as F
from sightline import unavailable
from sightline.checks import pagespeed
from sightline.checks.base import Finding as CheckOutput

PROFILE = F.ClientProfile(brand="Keli Renovation", category="contractor")

# The shapes we must never render. Sampled from what the code actually
# produced: requests' urllib3 repr, and Google's error payload.
DNS_EXCEPTION_TEXT = (
    "HTTPSConnectionPool(host='kelirenovationcorp.com', port=443): Max "
    "retries exceeded with url: / (Caused by NameResolutionError("
    "\"<urllib3.connection.HTTPSConnection object at 0x7f3c1a2b4d90>: "
    "Failed to resolve 'kelirenovationcorp.com' ([Errno -2] Name or "
    "service not known)\"))"
)
PSI_ERROR_PAGE = (
    'HTTP 500: {\n  "error": {\n    "code": 500,\n    "message": '
    '"Lighthouse returned error: ERRORED_DOCUMENT_REQUEST. Required '
    '"\n  }\n}'
)
LEAK_MARKERS = (
    "Traceback", "urllib3", "HTTPSConnectionPool", "Errno", "NameResolution",
    "0x7f", "Max retries", "ERRORED_DOCUMENT_REQUEST", "Lighthouse returned",
    "object at", "requests.exceptions",
)


def assert_no_exception_text(case: unittest.TestCase, blob: str, where: str):
    for marker in LEAK_MARKERS:
        case.assertNotIn(marker, blob, f"{where} leaks {marker!r}")


class FakeResponse:
    def __init__(self, status_code, payload=None, text="", headers=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


# --------------------------------------------------------------------------
# case 2: a third-party check fails
# --------------------------------------------------------------------------

class TestPageSpeedFailure(unittest.TestCase):
    """A faked PSI 500: retried, then recorded as not measured."""

    def _run_psi(self, responses=None, raises=None):
        """Run the check with a stubbed transport. Returns (findings, calls)."""
        calls = []

        def fake_get(*a, **kw):
            calls.append(kw.get("params", {}).get("strategy"))
            if raises is not None:
                raise raises
            return responses.pop(0) if responses else FakeResponse(500)

        ctx = mock.Mock(url="https://example.com/", domain="example.com")
        # settings is a frozen dataclass, so swap the object, not a field.
        fake_settings = SimpleNamespace(pagespeed_api_key="k", http_timeout=20)
        with mock.patch.object(pagespeed.requests, "get", fake_get), \
             mock.patch.object(pagespeed, "settings", fake_settings), \
             mock.patch.object(unavailable.time, "sleep") as slept:
            out = pagespeed.run(ctx)
        return out, calls, slept

    def test_a_500_is_retried_then_recorded_as_unmeasured(self):
        out, calls, slept = self._run_psi()
        # 3 attempts per strategy, two strategies.
        self.assertEqual(len(calls), 6, "429/5xx must be retried")
        self.assertTrue(slept.called, "retries must back off")
        self.assertEqual(len(out), 2)
        for f in out:
            self.assertEqual(f.severity, "unavailable")
            self.assertEqual(f.remediation, "", "rule 10: no fix to offer")
            self.assertIn("returned HTTP 500 after 3 attempts", f.observed)

    def test_no_severity_and_no_finding_survive_the_failure(self):
        """Nothing that scores, ranks or reads as a fault."""
        out, _, _ = self._run_psi()
        built = F.build_all(out, PROFILE)
        for f in built:
            self.assertEqual(f.outcome, F.UNMEASURED)
            self.assertIsNone(f.impact)
            self.assertIsNone(f.effort_minutes)
            self.assertEqual(f.owner, "swa")
            self.assertEqual(f.remediation, "")
            self.assertEqual(f.plain.title, "Not measured this scan")

    def test_the_error_page_body_never_reaches_the_finding(self):
        """Regression: `observed` was f"HTTP {code}: {r.text[:300]}"."""
        out, _, _ = self._run_psi(
            responses=[FakeResponse(500, text=PSI_ERROR_PAGE)] * 6)
        for f in out:
            assert_no_exception_text(self, f.observed, "psi observed")

    def test_a_timeout_is_classified_not_quoted(self):
        out, calls, _ = self._run_psi(raises=requests.Timeout("timed out"))
        self.assertEqual(len(calls), 6)
        for f in out:
            self.assertIn("did not respond in time", f.observed)
            assert_no_exception_text(self, f.observed, "psi timeout observed")

    def test_a_400_is_not_retried(self):
        """A bad key is a statement about our request. Retrying it spends
        quota to be told the same thing three times."""
        out, calls, _ = self._run_psi(
            responses=[FakeResponse(400, text="API key not valid")] * 2)
        self.assertEqual(len(calls), 2, "one call per strategy, no retries")
        for f in out:
            self.assertEqual(f.severity, "unavailable")
            self.assertIn("returned HTTP 400", f.observed)
            self.assertNotIn("attempts", f.observed)

    def test_retry_after_is_honoured_on_429(self):
        sleeps = []
        r, why = unavailable.send_with_retry(
            lambda: FakeResponse(429, headers={"Retry-After": "2"}),
            service="PSI", attempts=2, sleep=sleeps.append)
        self.assertIsNone(r)
        self.assertEqual(sleeps, [2.0])
        self.assertIn("returned HTTP 429 after 2 attempts", why)

    def test_a_missing_key_is_our_gap_not_a_client_task(self):
        ctx = mock.Mock(url="https://example.com/", domain="example.com")
        fake_settings = SimpleNamespace(pagespeed_api_key=None, http_timeout=20)
        with mock.patch.object(pagespeed, "settings", fake_settings):
            out = pagespeed.run(ctx)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].severity, "unavailable")
        self.assertEqual(out[0].remediation, "")


class TestRuleTenIsEnforcedAtTheSeams(unittest.TestCase):
    def test_a_check_cannot_author_a_remediation_on_an_unavailable_finding(self):
        with self.assertRaises(ValueError) as caught:
            CheckOutput(check_id="pagespeed", severity="unavailable",
                        examined="x", observed="y",
                        remediation="Enable the endpoint on your plan.")
        self.assertIn("rule 10", str(caught.exception))

    def test_an_unmeasured_finding_cannot_carry_impact_or_effort(self):
        good = F.build(CheckOutput(check_id="pagespeed",
                                   severity="unavailable", examined="x",
                                   observed="y"), PROFILE)
        for field, value in (("impact", "high"), ("effort_minutes", 30)):
            with self.assertRaises(ValueError, msg=field):
                F.replace(good, **{field: value})

    def test_re_measuring_is_never_the_clients_job(self):
        good = F.build(CheckOutput(check_id="pagespeed",
                                   severity="unavailable", examined="x",
                                   observed="y"), PROFILE)
        with self.assertRaises(ValueError):
            F.replace(good, owner="client")


class TestStoredRowsSurviveTheRule(unittest.TestCase):
    """The 86 rows already on disk: severity='unavailable', backfilled with
    impact='low', effort_minutes=15, owner='swa', and an `observed` holding
    Google's error payload. `replace()` re-runs __post_init__, so letting
    any of that win would raise on the READ path — every historical report
    holding a PageSpeed timeout would 500 instead of rendering."""

    ROW = {
        "check_id": "pagespeed", "item_key": "mobile:error",
        "severity": "unavailable",
        "examined": "PageSpeed Insights (mobile)",
        "observed": PSI_ERROR_PAGE,
        "remediation": "", "evidence": {},
        "impact": "low", "effort_minutes": 15, "owner": "swa",
        "deduction": 0.0,
    }

    def test_stored_task_fields_do_not_resurrect_the_finding(self):
        f = F.from_row(dict(self.ROW), PROFILE)
        self.assertEqual(f.outcome, F.UNMEASURED)
        self.assertIsNone(f.impact)
        self.assertIsNone(f.effort_minutes)
        self.assertEqual(f.owner, "swa")

    def test_a_stored_client_owner_is_forced_back_to_us(self):
        f = F.from_row(dict(self.ROW, owner="client", impact="high",
                            effort_minutes=240), PROFILE)
        self.assertEqual(f.owner, "swa")
        self.assertIsNone(f.impact)

    def test_a_stored_remediation_is_dropped_not_raised_on(self):
        """rank.py used to attach one. Scrub on read; never 500 a report
        over what a check stored last month."""
        f = F.from_row(dict(self.ROW, remediation="Enable the endpoint."),
                       PROFILE)
        self.assertEqual(f.remediation, "")

    def test_the_stored_error_payload_is_rebuilt_not_printed(self):
        f = F.from_row(dict(self.ROW), PROFILE)
        assert_no_exception_text(self, f.technical.detail, "rebuilt detail")
        self.assertIn("returned HTTP 500", f.technical.detail)


# --------------------------------------------------------------------------
# the score must not notice
# --------------------------------------------------------------------------

class TestUnmeasuredDoesNotMoveTheScore(unittest.TestCase):
    """The dimension must read exactly as it would if the check had not
    run — not 'scored zero', not 'scored full marks'."""

    WEIGHTS = {
        "dimensions": {"performance": ["pagespeed"],
                       "authority": ["rank"]},
        "dimension_labels": {"performance": "Performance",
                             "authority": "Authority"},
        "dimension_order": ["performance", "authority"],
        "deductions": {"pagespeed": {"critical": 30, "high": 12, "medium": 6,
                                     "low": 2, "pass": 0, "info": 0,
                                     "unavailable": 0},
                       "rank": {"critical": 30, "high": 12, "pass": 0,
                                "unavailable": 0}},
    }

    def _report(self, rows):
        from sightline.scoring import report as report_mod

        class FakeDb:
            @staticmethod
            def get_weight_version_by_id(_):
                return {"id": 1, "version": "v2", "weights": self.WEIGHTS}

            @staticmethod
            def scores_for_scan(*_):
                return rows

        with mock.patch.object(report_mod, "db", FakeDb):
            return report_mod.compute_report(1, 1)

    @staticmethod
    def _row(check_id, severity, deduction=0.0, item_key=""):
        return {"check_id": check_id, "severity": severity,
                "deduction": deduction, "item_key": item_key}

    def test_a_dimension_scores_identically_with_and_without_the_failure(self):
        ran = [self._row("pagespeed", "high", 12.0, "LCP"),
               self._row("pagespeed", "pass", 0.0, "CLS"),
               self._row("rank", "pass", 0.0)]
        failed = ran + [self._row("pagespeed", "unavailable", 0.0,
                                  "mobile:error"),
                        self._row("pagespeed", "unavailable", 0.0,
                                  "desktop:error")]

        without = self._report(ran)
        with_failure = self._report(failed)
        self.assertEqual(with_failure["dimensions"], without["dimensions"])
        self.assertEqual(with_failure["overall"], without["overall"])
        self.assertEqual(with_failure["dim_data"]["performance"]["available"],
                         without["dim_data"]["performance"]["available"])

    def test_a_wholly_unmeasured_dimension_is_none_not_zero(self):
        report = self._report([self._row("pagespeed", "unavailable"),
                               self._row("rank", "pass", 0.0)])
        self.assertIsNone(report["dimensions"]["performance"])
        # and it does not drag the mean down
        self.assertEqual(report["overall"], 100.0)

    def test_coverage_still_reports_what_was_missed(self):
        """Excluded from the score is not the same as hidden."""
        report = self._report([self._row("pagespeed", "unavailable"),
                               self._row("rank", "pass", 0.0)])
        self.assertEqual(report["coverage"]["unavailable"], 1)


# --------------------------------------------------------------------------
# nothing leaks into a rendered view
# --------------------------------------------------------------------------

SCAN = {
    "id": 169, "url": "https://example.com/", "domain": "example.com",
    "requested_at": dt.datetime(2026, 9, 22, 12, 0),
    "meta": {"profile": {"brand": "Keli Renovation",
                         "category": "contractor"}},
    "seo_score": None, "seo_metrics": {},
}

STORED_ROWS = [
    {"check_id": "pagespeed", "item_key": "LCP", "severity": "high",
     "examined": "LCP (mobile)", "observed": "LCP = 5.1 s.",
     "remediation": "Preload the hero image.", "evidence": {},
     "impact": "medium", "effort_minutes": 240, "owner": "client",
     "deduction": 12.0},
    {"check_id": "pagespeed", "item_key": "mobile:error",
     "severity": "unavailable", "examined": "PageSpeed Insights (mobile)",
     "observed": PSI_ERROR_PAGE, "remediation": "", "evidence": {},
     "impact": "low", "effort_minutes": 15, "owner": "swa",
     "deduction": 0.0},
    {"check_id": "rank", "item_key": "endpoint:backlinks",
     "severity": "unavailable", "examined": "DataForSEO backlinks/summary",
     "observed": f"request failed: {DNS_EXCEPTION_TEXT}",
     "remediation": "Enable the endpoint on your DataForSEO plan.",
     "evidence": {}, "impact": "low", "effort_minutes": 15, "owner": "swa",
     "deduction": 0.0},
    {"check_id": "rank", "item_key": "keyword_count", "severity": "pass",
     "examined": "Organic keyword footprint (US, en)",
     "observed": "312 ranked keywords in Google US.", "remediation": "",
     "evidence": {}, "impact": "low", "effort_minutes": 0,
     "owner": "client", "deduction": 0.0},
]

WEIGHTS = {
    "version": "v2", "max_score": 100.0,
    "dimensions": {"performance": ["pagespeed"], "authority": ["rank"]},
    "dimension_labels": {"performance": "Performance",
                         "authority": "Authority"},
    "dimension_order": ["performance", "authority"],
    "deductions": {"pagespeed": {"critical": 30, "high": 12,
                                 "pass": 0, "unavailable": 0},
                   "rank": {"critical": 30, "high": 12, "pass": 0,
                            "unavailable": 0}},
}


class FakeDb:
    @staticmethod
    def scan(_):
        return dict(SCAN)

    @staticmethod
    def get_weight_version_by_id(_):
        return {"id": 1, "version": "v2", "weights": WEIGHTS}

    @staticmethod
    def scores_for_scan(*_):
        return [dict(r) for r in STORED_ROWS]

    @staticmethod
    def peer_overall(*_):
        return None

    @staticmethod
    def peer_seo_score(*_):
        return None

    @staticmethod
    def av_summary(_):
        return {"n_samples": 0, "window_days": 30, "visibility_rate": 0.0}


def render_fixture() -> str:
    from sightline.render import html as html_mod
    from sightline.scoring import report as report_mod

    with mock.patch.object(html_mod, "db", FakeDb), \
         mock.patch.object(report_mod, "db", FakeDb):
        return html_mod.render_scan(169, 1)


def export_fixture() -> dict:
    from sightline.render import json_export
    from sightline.scoring import report as report_mod

    with mock.patch.object(json_export, "db", FakeDb), \
         mock.patch.object(report_mod, "db", FakeDb):
        return json_export.export_scan(169, 1)


class TestNothingLeaksIntoARenderedView(unittest.TestCase):
    def test_no_exception_text_in_the_rendered_report(self):
        assert_no_exception_text(self, render_fixture(), "rendered html")

    def test_no_exception_text_in_findings_json(self):
        assert_no_exception_text(self, json.dumps(export_fixture()),
                                 "findings.json")

    def test_the_report_says_what_failed_in_the_technical_view(self):
        html = render_fixture()
        self.assertIn("Not measured this scan", html)
        self.assertIn("returned HTTP 500", html)

    def test_findings_json_gives_cited_nulls_not_a_task(self):
        entries = [f for f in export_fixture()["findings"]
                   if f["outcome"] == "unmeasured"]
        self.assertEqual(len(entries), 2)
        for entry in entries:
            self.assertIsNone(entry["impact"])
            self.assertIsNone(entry["effort_minutes"])
            self.assertEqual(entry["owner"], "swa")
            self.assertEqual(entry["plain"]["title"], "Not measured this scan")

    def test_the_failure_page_never_prints_a_stored_exception(self):
        """Legacy rows hold raw tracebacks; the view whitelists ours."""
        legacy = {"id": 169, "error": f"page fetch failed: {DNS_EXCEPTION_TEXT}"}
        message = unavailable.failure_message(legacy)
        assert_no_exception_text(self, message, "failure_message")
        self.assertIn("scan #169", message)

    def test_our_own_sentence_survives_the_scrub(self):
        scan = {"id": 170,
                "error": str(unavailable.SiteUnreachable(
                    "kelirenovationcorp.com"))}
        self.assertEqual(
            unavailable.failure_message(scan),
            "We couldn't reach a website at kelirenovationcorp.com. Check "
            "the spelling, or confirm the domain is live.")


class TestUnreachablePageIsNotScored(unittest.TestCase):
    """Case 1. The scan aborts, the row stores a readable sentence, and the
    traceback goes to the log."""

    def _run(self, fetch_result):
        from sightline import pipeline

        completed = {}

        class Db:
            @staticmethod
            def complete_scan(scan_id, status="complete", error=None):
                completed.update(scan_id=scan_id, status=status, error=error)

        with mock.patch.object(pipeline.fetch_http, "fetch_as_browser",
                               lambda _: fetch_result), \
             mock.patch.object(pipeline, "db", Db):
            with self.assertRaises(unavailable.ScanAborted) as caught:
                pipeline.run_scan_for(169, "https://kelirenovationcorp.com/")
        return completed, caught.exception

    def test_a_dns_failure_renders_one_plain_line(self):
        from sightline.fetch.http import FetchResult

        completed, exc = self._run(FetchResult(
            url="https://kelirenovationcorp.com/",
            final_url="https://kelirenovationcorp.com/",
            status=0, error=DNS_EXCEPTION_TEXT))

        self.assertEqual(completed["status"], "failed")
        self.assertEqual(
            str(exc),
            "We couldn't reach a website at kelirenovationcorp.com. Check "
            "the spelling, or confirm the domain is live.")
        assert_no_exception_text(self, completed["error"], "stored error")
        self.assertEqual(unavailable.failure_message(
            {"id": 169, "error": completed["error"]}), str(exc))

    def test_an_error_status_says_so_without_the_body(self):
        from sightline.fetch.http import FetchResult

        completed, exc = self._run(FetchResult(
            url="https://example.com/", final_url="https://example.com/",
            status=503, error=None))
        self.assertIn("HTTP 503", str(exc))
        self.assertIn("example.com", str(exc))
        assert_no_exception_text(self, completed["error"], "stored error")

    def test_an_unexpected_crash_stores_a_readable_line_not_a_stack(self):
        from sightline import pipeline

        completed = {}

        class Db:
            @staticmethod
            def complete_scan(scan_id, status="complete", error=None):
                completed.update(status=status, error=error)

        def boom(_):
            raise RuntimeError(DNS_EXCEPTION_TEXT)

        with mock.patch.object(pipeline, "_build_context", boom), \
             mock.patch.object(pipeline, "db", Db):
            with self.assertRaises(RuntimeError):
                pipeline.run_scan_for(169, "https://example.com/")

        assert_no_exception_text(self, completed["error"], "stored error")
        self.assertIn("scan #169", completed["error"])


# --------------------------------------------------------------------------
# the Cited shim
# --------------------------------------------------------------------------

class TestCitedReadsUnmeasuredAsNotMeasured(unittest.TestCase):
    """Cited's ACTUAL regexes and mapping functions, copied from
    /opt/cited/src/lib/sightline/dashboard.ts, run against a real rendered
    report. See tests/test_findings_copy.py::TestCitedScrapingCoupling for
    the severity span these tests are the other half of.

    The trap: `statusOf` maps 'unavailable' to 'partial', which is not
    green — but `findingsToRecs` filters on !/pass|info/, which
    'unavailable' does not match. So while an unmeasured result was a
    finding block, Cited turned every PageSpeed timeout into a
    recommendation reading "Fix the finding on the page, then rescan in
    Sightline." No severity string escapes both: the only value that keeps
    Cited off green is the one that produces the CTA. Hence a section
    Cited's block regex cannot start on.

    Delete this class only when Cited reads findings.json.
    """

    # dashboard.ts:82
    BLOCK = re.compile(
        r"<div class='finding[^']*'>([\s\S]*?)</div>\s*"
        r"(?=<div class='finding|<h2|</body>)")
    SEV = re.compile(r"""class=['"]sev['"][^>]*>([\s\S]*?)</span>""", re.I)
    BODY = re.compile(r"""class=['"]body['"]>([\s\S]*?)</div>""", re.I)
    REMED = re.compile(r"""class=['"]remed['"]>([\s\S]*?)</div>""", re.I)

    @staticmethod
    def _status_of(severity: str) -> str:
        """findingsToChecks.statusOf"""
        s = severity.lower()
        if s in ("pass", "info"):
            return "pass"
        if s in ("low", "medium", "unavailable"):
            return "partial"
        return "fail"

    @staticmethod
    def _is_recommendation(severity: str) -> bool:
        """findingsToRecs' filter"""
        return not re.search(r"pass|info", severity, re.I)

    def _parsed(self):
        html = render_fixture()
        out = []
        for chunk in self.BLOCK.findall(html):
            match = self.SEV.search(chunk)
            # Cited's silent default. A miss here is not an error: it
            # reads as 'info' -> 'pass' -> green.
            severity = match.group(1).strip() if match else "info"
            out.append({"severity": severity, "defaulted": match is None,
                        "chunk": chunk})
        return html, out

    def test_no_unmeasured_block_is_parsed_as_a_finding(self):
        _, parsed = self._parsed()
        self.assertTrue(parsed, "the scored findings must still be parsed")
        for entry in parsed:
            self.assertNotEqual(entry["severity"], "unavailable")

    def test_nothing_defaults_to_green(self):
        """The failure mode of the shim: a missed match reads as 'pass'.

        A finding that really did pass may of course read as pass. What
        must not happen is a block whose severity Cited could not find,
        because that is indistinguishable from a clean site.
        """
        _, parsed = self._parsed()
        for entry in parsed:
            self.assertFalse(entry["defaulted"],
                             "Cited would default this block to info/pass")
        gaps = [e for e in parsed
                if e["severity"] in ("critical", "high", "medium", "low")]
        self.assertTrue(gaps, "fixture must contain a scored gap")
        for entry in gaps:
            self.assertNotEqual(self._status_of(entry["severity"]), "pass")

    def test_no_unmeasured_result_becomes_a_recommendation(self):
        """The CTA rule 10 forbids, at the only boundary that can emit it."""
        _, parsed = self._parsed()
        recs = [e for e in parsed if self._is_recommendation(e["severity"])]
        for entry in recs:
            self.assertNotIn("Not measured", entry["chunk"])
            self.assertNotIn("nothing was measured", entry["chunk"])
            self.assertNotEqual(entry["severity"], "unavailable")

    def test_the_not_measured_section_leaks_into_no_finding_block(self):
        """The <h2> is load-bearing. Inline between two finding divs, the
        lookahead makes the PRECEDING block swallow this content, and its
        `remed` becomes that finding's fix."""
        html, parsed = self._parsed()
        self.assertIn("<div class='nm'>", html, "fixture must exercise it")
        for entry in parsed:
            self.assertNotIn("nm-body", entry["chunk"])
            self.assertNotIn("returned HTTP 500", entry["chunk"])

    def test_the_not_measured_markup_avoids_every_class_cited_reads(self):
        html = render_fixture()
        section = html.split("Not measured this scan", 1)[1].split("<h2", 1)[0]
        self.assertNotIn("class='finding", section)
        self.assertIsNone(self.SEV.search(section))
        self.assertIsNone(self.BODY.search(section))
        self.assertIsNone(self.REMED.search(section))

    def test_the_reason_is_documented_where_someone_would_rename_it(self):
        import inspect
        from sightline.render import html as html_mod

        src = inspect.getsource(html_mod.render_scan)
        self.assertIn("Cited", src)
        self.assertIn("findings.json", src)


if __name__ == "__main__":
    unittest.main()
