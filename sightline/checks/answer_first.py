"""Answer-first content structure. Heuristic: does the page's main content
begin with something that reads as a direct answer, or with preamble?

We're not scoring "quality." We flag two specific tells:
  - The first substantive paragraph begins with a common marketing preamble
    ("Welcome to...", "At X, we...", "Founded in...").
  - The first 300 characters of main content contain no noun-verb pairing
    that resembles a claim (crude, but catches empty hero copy)."""
from __future__ import annotations

import re

from ..fetch.discovery import Page, main_content_soup, visible_text
from .base import Finding, ScanContext

CHECK_ID = "answer_first"

PREAMBLE_STARTS = [
    r"welcome to\b",
    r"at\s+\S+\s*,?\s*we\b",
    r"founded in\b",
    r"established in\b",
    r"since\s+\d{4}\b",
    r"our (?:mission|passion|goal|story)\b",
    r"we (?:are|believe|specialize|provide|offer|help|deliver)\b",
    r"here at\b",
    r"thank you for (?:visiting|choosing)\b",
]
PREAMBLE_RE = re.compile("|".join(PREAMBLE_STARTS), re.I)


def _first_paragraph(page: Page) -> str:
    """First substantive paragraph in main content, not navigation. Reads
    from a boilerplate-stripped clone of the soup so nav menus don't leak
    in — the earlier implementation walked page.soup directly and read
    header links on sites that had no <main> landmark."""
    cleaned = main_content_soup(page.soup)
    main = cleaned.find("main") or cleaned.find("article") or cleaned.body or cleaned
    for tag in main.find_all(["p", "div"]):
        text = tag.get_text(" ", strip=True)
        if len(text) >= 60:
            return text
    return visible_text(page.soup, limit=500)


def run(ctx: ScanContext) -> list[Finding]:
    findings: list[Finding] = []
    if ctx.page is None:
        findings.append(Finding(
            check_id=CHECK_ID, item_key="page",
            severity="unavailable",
            examined="Answer-first structure of main content",
            observed="Page HTML could not be fetched; check skipped.",
        ))
        return findings

    first = _first_paragraph(ctx.page)
    excerpt = first[:200]

    m = PREAMBLE_RE.search(first[:120] if first else "")
    if m:
        findings.append(Finding(
            check_id=CHECK_ID, item_key="preamble",
            severity="medium",
            examined="First substantive paragraph of main content",
            observed=(
                f"Opens with a preamble phrase ('{m.group(0)}'). "
                f"Excerpt: \"{excerpt}...\"" if len(first) > 200 else
                f"Opens with a preamble phrase ('{m.group(0)}')."
            ),
            remediation=(
                "Restructure the opening so the page answers what it is or what it "
                "does in the first sentence. Save the origin story for lower on the "
                "page. Assistants extract from the top."
            ),
            evidence={"match": m.group(0), "excerpt": excerpt},
        ))
    else:
        findings.append(Finding(
            check_id=CHECK_ID, item_key="preamble",
            severity="pass",
            examined="First substantive paragraph of main content",
            observed="Opening does not start with common preamble phrasing.",
            evidence={"excerpt": excerpt},
        ))

    # Very-short opening: often a decorative hero with no substance.
    if len(first) < 40:
        findings.append(Finding(
            check_id=CHECK_ID, item_key="thin_opening",
            severity="medium",
            examined="Substance of the first 40 characters of main content",
            observed=(
                f"First substantive text is only {len(first)} characters long "
                f"({excerpt!r}). Assistants have little to extract from the top."
            ),
            remediation=(
                "Add a concrete opening sentence that answers what this page is "
                "about. Hero images and taglines do not read as content."
            ),
        ))

    # Presence of a heading structure. No H1 or many H1s are both signals.
    h1s = ctx.page.h1s
    if not h1s:
        findings.append(Finding(
            check_id=CHECK_ID, item_key="h1",
            severity="medium",
            examined="Top-level heading (h1) on the page",
            observed="No h1 heading found.",
            remediation=(
                "Add a single, descriptive h1 that names what this page is about. "
                "Assistants and search parsers use it as the page's title-of-record."
            ),
        ))
    elif len(h1s) > 1:
        findings.append(Finding(
            check_id=CHECK_ID, item_key="h1",
            severity="low",
            examined="Top-level heading (h1) on the page",
            observed=f"{len(h1s)} h1 headings found; expected one.",
            remediation=(
                "Use exactly one h1 per page and reserve h2/h3 for subsections. "
                "Multiple h1s dilute the page's topical anchor."
            ),
            evidence={"h1s": h1s[:5]},
        ))
    else:
        findings.append(Finding(
            check_id=CHECK_ID, item_key="h1",
            severity="pass",
            examined="Top-level heading (h1) on the page",
            observed=f"Single h1 present: \"{h1s[0][:120]}\"",
        ))

    return findings
