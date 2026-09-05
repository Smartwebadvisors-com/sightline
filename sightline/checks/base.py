"""Check → Finding contract. Checks describe what they examined and what
they observed. They do not assign numbers. Scoring lives elsewhere so the
same finding can be re-scored under a new weight model without re-fetching."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Severities. 'pass' and 'info' record something we checked but is fine, so
# the report can show the full audit trail. 'unavailable' means we could not
# measure — never counted against the site.
SEVERITIES = ("pass", "info", "low", "medium", "high", "critical", "unavailable")


@dataclass
class Finding:
    check_id: str
    severity: str
    examined: str
    observed: str
    remediation: str = ""
    item_key: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.severity not in SEVERITIES:
            raise ValueError(
                f"invalid severity {self.severity!r} for {self.check_id}"
            )


@dataclass
class ScanContext:
    """Everything a check might need for the target URL. Populated once per
    scan by the runner so checks don't refetch."""
    url: str
    origin: str
    domain: str
    page: Any = None            # fetch.discovery.Page or None
    page_fetch: Any = None      # fetch.http.FetchResult or None
    robots_text: str | None = None
    robots_status: int = 0
    llms_txt_fetch: Any = None
    sitemap_fetch: Any = None
