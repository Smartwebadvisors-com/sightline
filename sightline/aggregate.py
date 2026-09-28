"""One finding per problem across the sampled pages.

A check that fails on three pages is one card, not three, and it is scored
once. The count of pages rides on the evidence so the client copy can say
how many pages showed it.
"""
from __future__ import annotations

from dataclasses import replace

from .checks.base import Finding

_RANK = {
    "unavailable": 0,
    "pass": 1,
    "info": 2,
    "low": 3,
    "medium": 4,
    "high": 5,
    "critical": 6,
}
_GAP = {"low", "medium", "high", "critical"}


def aggregate(
    grouped: dict[tuple[str, str], list[tuple[str, Finding]]],
    pages_checked: int,
) -> list[Finding]:
    merged: list[Finding] = []
    for rows in grouped.values():
        measured = [(url, finding) for url, finding in rows if finding.severity != "unavailable"]
        if not measured:
            merged.append(rows[0][1])
            continue
        _url, worst = max(measured, key=lambda row: _RANK.get(row[1].severity, 0))
        gaps = {url for url, finding in measured if finding.severity in _GAP}
        evidence = dict(worst.evidence)
        evidence["pages_checked"] = pages_checked
        evidence["pages_with_gap"] = len(gaps)
        merged.append(replace(worst, evidence=evidence))
    return merged
