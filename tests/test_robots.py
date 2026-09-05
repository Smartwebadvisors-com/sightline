"""Regression tests for the robots.txt matcher used by ai_crawler.

The two live-bug tests at the top anchor this file:
  * scan-22 shipped with every rule evaluated against '/' — the
    deep-path case proves the fix triggers path-scoped Disallows.
  * The root-path case proves the fix does NOT over-block. Over-
    blocking would ship worse findings than under-blocking (we would
    tell a prospect they need to fix a problem they don't have)."""
from __future__ import annotations

import unittest

from sightline.fetch.robots import is_blocked, parse, request_target


# The robots.txt from the live test that surfaced scan-22 (verbatim).
LIVE_ROBOTS = """
User-agent: GPTBot
Disallow: /

User-agent: CCBot
Disallow: /sightline-test/

User-agent: *
Disallow: /wp-admin/
Allow: /wp-admin/admin-ajax.php
Disallow: /sightline-test/
""".strip()


# The eight assistant crawler tokens ai_crawler.py probes.
ASSISTANT_CRAWLERS = [
    "GPTBot", "OAI-SearchBot", "ClaudeBot", "PerplexityBot",
    "Google-Extended", "Applebot-Extended", "CCBot",
    "Meta-ExternalAgent",
]


class TestLiveBugFromScan22(unittest.TestCase):
    """Both cases must pass before re-scanning prospect URLs."""

    def setUp(self):
        self.robots = parse(LIVE_ROBOTS)

    def test_deep_path_blocks_all_eight_crawlers(self):
        """/sightline-test/: GPTBot (via 'Disallow: /'), CCBot (via named
        rule), and every unnamed crawler (via '* Disallow: /sightline-
        test/') must all be blocked."""
        for ua in ASSISTANT_CRAWLERS:
            blocked, rule = is_blocked(self.robots, ua, "/sightline-test/")
            self.assertTrue(
                blocked,
                f"{ua}: expected BLOCKED on /sightline-test/, got allowed "
                f"(matched rule={rule!r})",
            )

    def test_root_path_blocks_only_gptbot(self):
        """/: only GPTBot's 'Disallow: /' matches. Any over-block here
        would ship false-positive findings to a prospect."""
        blocked, _ = is_blocked(self.robots, "GPTBot", "/")
        self.assertTrue(blocked, "GPTBot on /: expected blocked")
        for ua in [c for c in ASSISTANT_CRAWLERS if c != "GPTBot"]:
            blocked, rule = is_blocked(self.robots, ua, "/")
            self.assertFalse(
                blocked,
                f"{ua} on /: expected allowed, got BLOCKED by rule={rule!r} "
                "(over-block would ship false-positive findings)",
            )


class TestScan17Case(unittest.TestCase):
    """Live consequence of scan-22: lvtvnetwork.com had 'Disallow:
    /lvtv-network/' under 'User-agent: *' and scored AI=100 anyway."""

    def test_path_scoped_disallow_under_wildcard_group(self):
        r = parse("User-agent: *\nDisallow: /lvtv-network/\n")
        # Homepage is not blocked.
        for ua in ASSISTANT_CRAWLERS:
            self.assertFalse(is_blocked(r, ua, "/")[0])
        # But the deep path IS blocked for every crawler (no named groups).
        for ua in ASSISTANT_CRAWLERS:
            blocked, _ = is_blocked(r, ua, "/lvtv-network/")
            self.assertTrue(blocked, f"{ua} should be blocked on /lvtv-network/")
        # A URL under the disallowed prefix is also blocked.
        for ua in ASSISTANT_CRAWLERS:
            self.assertTrue(is_blocked(r, ua, "/lvtv-network/watch/1")[0])


class TestRequestTarget(unittest.TestCase):
    """RFC 9309: request-target is path + '?query'. Full URLs include
    scheme + host which the matcher must not see."""

    def test_path_only(self):
        self.assertEqual(request_target("https://example.com/foo/bar"),
                         "/foo/bar")

    def test_empty_path_becomes_slash(self):
        self.assertEqual(request_target("https://example.com"), "/")
        self.assertEqual(request_target("https://example.com/"), "/")

    def test_query_included(self):
        self.assertEqual(
            request_target("https://example.com/page?ref=abc&x=1"),
            "/page?ref=abc&x=1",
        )

    def test_fragment_stripped(self):
        # Fragments are client-side only; robots never sees them.
        self.assertEqual(
            request_target("https://example.com/page#section"),
            "/page",
        )


class TestRfc9309Semantics(unittest.TestCase):
    """The rules the fix relies on. If any of these regress, the fix's
    correctness on the live-bug tests is coincidental."""

    def test_named_group_beats_wildcard_when_present(self):
        r = parse("User-agent: GPTBot\nDisallow: /\n\n"
                  "User-agent: *\nAllow: /\n")
        # GPTBot uses named group only — blocked.
        self.assertTrue(is_blocked(r, "GPTBot", "/")[0])
        # Unnamed crawler falls back to * — allowed.
        self.assertFalse(is_blocked(r, "ClaudeBot", "/")[0])

    def test_allow_beats_disallow_on_equal_length(self):
        r = parse("User-agent: *\nDisallow: /a/b\nAllow: /a/b\n")
        blocked, _ = is_blocked(r, "Bot", "/a/b")
        self.assertFalse(blocked, "equal-length allow must beat disallow")

    def test_longest_matching_rule_wins(self):
        r = parse("User-agent: *\n"
                  "Disallow: /wp-admin/\n"
                  "Allow: /wp-admin/admin-ajax.php\n")
        # Allow is longer — wins for the exact ajax path.
        blocked, rule = is_blocked(r, "Bot", "/wp-admin/admin-ajax.php")
        self.assertFalse(blocked, f"expected allow, matched rule={rule!r}")
        # Other paths under /wp-admin/ only match disallow.
        self.assertTrue(is_blocked(r, "Bot", "/wp-admin/foo.php")[0])

    def test_user_agent_matching_case_insensitive(self):
        r = parse("User-agent: GPTBOT\nDisallow: /\n")
        self.assertTrue(is_blocked(r, "gptbot", "/")[0])
        self.assertTrue(is_blocked(r, "GPTBot", "/")[0])
        self.assertTrue(is_blocked(r, "GpTbOt", "/")[0])

    def test_path_matching_case_sensitive(self):
        r = parse("User-agent: *\nDisallow: /Private/\n")
        self.assertTrue(is_blocked(r, "Bot", "/Private/")[0])
        self.assertFalse(is_blocked(r, "Bot", "/private/")[0])

    def test_dollar_anchors_end(self):
        r = parse("User-agent: *\nDisallow: /foo$\n")
        self.assertTrue(is_blocked(r, "Bot", "/foo")[0])
        self.assertFalse(is_blocked(r, "Bot", "/foobar")[0])
        self.assertFalse(is_blocked(r, "Bot", "/foo/")[0])

    def test_star_wildcard_matches_any_run(self):
        r = parse("User-agent: *\nDisallow: /*.pdf\n")
        self.assertTrue(is_blocked(r, "Bot", "/anything.pdf")[0])
        self.assertTrue(is_blocked(r, "Bot", "/deep/nested/path.pdf")[0])
        self.assertFalse(is_blocked(r, "Bot", "/anything.html")[0])

    def test_query_string_matched_via_wildcard(self):
        r = parse("User-agent: *\nDisallow: /*?ref=\n")
        self.assertTrue(is_blocked(r, "Bot", "/page?ref=abc")[0])
        self.assertFalse(is_blocked(r, "Bot", "/page?other=abc")[0])

    def test_empty_disallow_allows_all(self):
        # Per RFC, 'Disallow:' (empty) means the crawler has no restriction.
        r = parse("User-agent: *\nDisallow:\n")
        self.assertFalse(is_blocked(r, "Bot", "/")[0])
        self.assertFalse(is_blocked(r, "Bot", "/anywhere/deep")[0])

    def test_no_rules_at_all_allows_all(self):
        r = parse("")
        for ua in ASSISTANT_CRAWLERS:
            self.assertFalse(is_blocked(r, ua, "/")[0])
            self.assertFalse(is_blocked(r, ua, "/anywhere")[0])


if __name__ == "__main__":
    unittest.main()
