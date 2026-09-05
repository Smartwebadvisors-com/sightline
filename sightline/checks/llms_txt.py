"""llms.txt presence and validity. The llms.txt proposal
(https://llmstxt.org) is an unofficial community convention, not a
documented ranking signal. Sightline reports its presence and structural
validity as an operational hygiene item, not a ranking factor. The
remediation copy must not imply otherwise."""
from __future__ import annotations

import re

from .base import Finding, ScanContext

CHECK_ID = "llms_txt"


def _looks_valid(text: str) -> tuple[bool, str]:
    """Very light structural check aligned with the llmstxt.org convention:
    starts with an H1 line, has at least one section header or link block."""
    stripped = text.strip()
    if not stripped:
        return False, "file is empty"
    lines = [ln.rstrip() for ln in stripped.splitlines()]
    if not lines[0].startswith("# "):
        return False, "does not start with an H1 title line ('# Title')"
    has_link = any(re.match(r"\s*-\s*\[.+\]\(.+\)", ln) for ln in lines)
    if not has_link:
        return False, "no markdown link items found"
    return True, "starts with H1 and includes markdown link items"


def run(ctx: ScanContext) -> list[Finding]:
    r = ctx.llms_txt_fetch
    if r is None or r.error:
        findings = [Finding(
            check_id=CHECK_ID, item_key="presence",
            severity="unavailable",
            examined="/llms.txt at the origin",
            observed=("Network error while fetching /llms.txt."
                      if r and r.error else "llms.txt fetch not attempted."),
            evidence={"error": r.error if r else None},
        )]
        return findings

    if r.status == 404:
        return [Finding(
            check_id=CHECK_ID, item_key="presence",
            severity="low",
            examined="/llms.txt at the origin",
            observed="No llms.txt present.",
            remediation=(
                "Consider publishing /llms.txt to give assistants a curated map "
                "of the pages you most want them to read. Note: llms.txt is an "
                "operational hygiene file, not a documented ranking signal. "
                "Adopting it will not by itself change your ranking."
            ),
            evidence={"status": 404},
        )]

    if r.status >= 400 or not r.ok:
        return [Finding(
            check_id=CHECK_ID, item_key="presence",
            severity="low",
            examined="/llms.txt at the origin",
            observed=f"/llms.txt returned HTTP {r.status}.",
            evidence={"status": r.status},
        )]

    valid, reason = _looks_valid(r.text)
    if valid:
        return [Finding(
            check_id=CHECK_ID, item_key="presence",
            severity="pass",
            examined="/llms.txt at the origin",
            observed=f"llms.txt present and structurally valid ({reason}).",
            evidence={"bytes": len(r.text)},
        )]
    return [Finding(
        check_id=CHECK_ID, item_key="presence",
        severity="medium",
        examined="/llms.txt at the origin",
        observed=f"llms.txt present but does not match the convention: {reason}.",
        remediation=(
            "Update /llms.txt so it starts with a single H1 title, a short "
            "blockquote description, and section headings containing markdown "
            "link bullets. See https://llmstxt.org for the convention."
        ),
        evidence={"reason": reason, "excerpt": r.text[:200]},
    )]
