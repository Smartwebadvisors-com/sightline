"""DataForSEO v3 client. One place that knows how to authenticate, call,
and read the shape of a response.

Extracted from checks/rank.py so the rank check and the SEO score cannot
drift in how they read the same payloads. Both funnel through
`metrics_from()`, which means a live scan and a backfill of the same
domain produce the same four numbers from the same fields.

Every endpoint is separately priced and separately authorized on a
DataForSEO account, so a result carries `unauthorized`. Callers report
'not measured' rather than penalizing a site for our tooling gap.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any

import requests

from .config import settings

BASE = "https://api.dataforseo.com/v3"

EP_RANKED_KEYWORDS = "dataforseo_labs/google/ranked_keywords/live"
# Rank Overview returns the whole position distribution (pos_1, pos_2_3,
# pos_4_10, ...) plus the total keyword count in a single call, so the
# ranked-keyword count and the top-10 count come from one source and
# cannot contradict each other.
EP_RANK_OVERVIEW = "dataforseo_labs/google/domain_rank_overview/live"
# Backlinks Summary is the correct endpoint for backlink counts. The
# earlier whois/overview endpoint is a WHOIS record whose backlinks/
# referring_domains fields are almost always zero on our account tier —
# not the actual backlink graph.
EP_BACKLINKS = "backlinks/summary/live"

# US / English. Sightline is scoped to US businesses.
LOCATION_CODE = 2840
LANGUAGE_CODE = "en"

UNAUTHORIZED_HTTP = (401, 402, 403)
UNAUTHORIZED_TASK = (40100, 40200, 40300)
TASK_OK = 20000

# Every organic position band Rank Overview reports, in order. Persisted
# whole so a future score version can use bands this one ignores without
# re-buying the call.
POSITION_BANDS = (
    "pos_1", "pos_2_3", "pos_4_10", "pos_11_20", "pos_21_30", "pos_31_40",
    "pos_41_50", "pos_51_60", "pos_61_70", "pos_71_80", "pos_81_90",
    "pos_91_100",
)

# The bands that count as page-one visibility.
TOP10_BANDS = ("pos_1", "pos_2_3", "pos_4_10")


@dataclass
class DFSResult:
    """One endpoint call. `result` is the first element of the first
    task's result array — every endpoint we use returns exactly one."""
    endpoint: str
    http_status: int | None = None
    task_status: int | None = None
    task_message: str = ""
    result: dict | None = None
    error: str = ""

    @property
    def unauthorized(self) -> bool:
        """The account cannot call this endpoint — a billing/plan gap on
        our side, never a fact about the target."""
        return (self.http_status in UNAUTHORIZED_HTTP
                or self.task_status in UNAUTHORIZED_TASK)

    @property
    def ok(self) -> bool:
        """The task succeeded. `result` may still be None: an endpoint can
        answer about a domain it has nothing on. That is an answer of
        'nothing', not a failure to answer, and callers must read it as a
        measured zero — otherwise the domains with no keywords and no
        links, the ones worth pitching, get excused as 'not measured'."""
        return self.http_status == 200 and self.task_status == TASK_OK

    @property
    def reason(self) -> str:
        """Why this call did not produce data, phrased for a report."""
        if self.error:
            return f"request failed: {self.error}"
        if self.unauthorized:
            return (f"endpoint not available on this account: HTTP "
                    f"{self.http_status} / task status {self.task_status} "
                    f"({self.task_message})")
        return (f"unexpected response: HTTP {self.http_status} / task "
                f"{self.task_status} ({self.task_message})")


def configured() -> bool:
    return bool(settings.dataforseo_login and settings.dataforseo_password)


def auth_header() -> dict[str, str] | None:
    if not configured():
        return None
    token = base64.b64encode(
        f"{settings.dataforseo_login}:{settings.dataforseo_password}".encode()
    ).decode()
    return {"Authorization": f"Basic {token}",
            "Content-Type": "application/json"}


def call(endpoint: str, payload: list[dict],
         headers: dict[str, str] | None = None) -> DFSResult:
    """POST one endpoint. Never raises — transport and protocol failures
    come back as a DFSResult whose `ok` is False."""
    headers = headers or auth_header()
    if headers is None:
        return DFSResult(endpoint=endpoint,
                         error="DATAFORSEO_LOGIN/DATAFORSEO_PASSWORD "
                               "not configured")
    try:
        r = requests.post(f"{BASE}/{endpoint}", headers=headers,
                          json=payload, timeout=settings.http_timeout * 3)
    except requests.RequestException as e:
        return DFSResult(endpoint=endpoint, error=str(e))

    try:
        body = r.json()
    except ValueError:
        return DFSResult(endpoint=endpoint, http_status=r.status_code,
                         error=f"non-JSON response: {r.text[:200]}")

    tasks = (body or {}).get("tasks") or []
    if not tasks:
        return DFSResult(endpoint=endpoint, http_status=r.status_code,
                         error="no tasks in response")
    task = tasks[0] or {}
    result = (task.get("result") or [None])[0]
    return DFSResult(
        endpoint=endpoint,
        http_status=r.status_code,
        task_status=task.get("status_code"),
        task_message=task.get("status_message") or "",
        result=result if isinstance(result, dict) else None,
    )


def ranked_keywords(domain: str, limit: int = 10,
                    headers: dict[str, str] | None = None) -> DFSResult:
    return call(EP_RANKED_KEYWORDS, [{
        "target": domain,
        "language_code": LANGUAGE_CODE,
        "location_code": LOCATION_CODE,
        "limit": limit,
    }], headers)


def rank_overview(domain: str,
                  headers: dict[str, str] | None = None) -> DFSResult:
    return call(EP_RANK_OVERVIEW, [{
        "target": domain,
        "language_code": LANGUAGE_CODE,
        "location_code": LOCATION_CODE,
    }], headers)


def backlinks_summary(domain: str,
                      headers: dict[str, str] | None = None) -> DFSResult:
    return call(EP_BACKLINKS, [{"target": domain}], headers)


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def organic_metrics(overview: DFSResult) -> dict:
    """metrics.organic from a rank_overview result, or {}."""
    items = (overview.result or {}).get("items") or []
    first = items[0] if items and isinstance(items[0], dict) else {}
    return (first.get("metrics") or {}).get("organic") or {}


def metrics_from(overview: DFSResult, backlinks: DFSResult) -> dict:
    """The four SEO-score inputs plus a per-endpoint audit trail.

    A component is None only when the ENDPOINT did not answer. When an
    endpoint answers with nothing — a domain with no organic presence, or
    no inbound links — that is a real measurement of zero. Recording it as
    'unavailable' would quietly excuse the worst sites from the very
    metric they fail hardest, which is backwards.
    """
    metrics: dict[str, Any] = {
        "ranked_keywords": None,
        "top10_keywords": None,
        "referring_domains": None,
        "domain_rank": None,
    }
    sources: dict[str, dict] = {}

    if overview.ok:
        organic = organic_metrics(overview)
        positions = {b: _int(organic.get(b)) or 0 for b in POSITION_BANDS}
        metrics["ranked_keywords"] = _int(organic.get("count")) or 0
        metrics["top10_keywords"] = sum(positions[b] for b in TOP10_BANDS)
        sources[EP_RANK_OVERVIEW] = {
            "ok": True,
            "positions": positions,
            "etv": organic.get("etv"),
            "is_lost": _int(organic.get("is_lost")) or 0,
        }
    else:
        sources[EP_RANK_OVERVIEW] = {
            "ok": False, "reason": overview.reason,
            "unauthorized": overview.unauthorized,
        }

    if backlinks.ok:
        result = backlinks.result or {}
        metrics["referring_domains"] = _int(result.get("referring_domains")) or 0
        metrics["domain_rank"] = _int(result.get("rank")) or 0
        sources[EP_BACKLINKS] = {
            "ok": True,
            "backlinks": _int(result.get("backlinks")) or 0,
            "referring_main_domains":
                _int(result.get("referring_main_domains")) or 0,
        }
    else:
        sources[EP_BACKLINKS] = {
            "ok": False, "reason": backlinks.reason,
            "unauthorized": backlinks.unauthorized,
        }

    metrics["sources"] = sources
    return metrics


def collect_seo_metrics(domain: str) -> dict:
    """Both SEO-score endpoints for a domain, fresh.

    Used by `sightline backfill-seo`. The live scan path does NOT call
    this — checks/rank.py already makes both calls and stashes the result
    on the ScanContext, so a scan costs no extra API spend.
    """
    headers = auth_header()
    return metrics_from(rank_overview(domain, headers),
                        backlinks_summary(domain, headers))
