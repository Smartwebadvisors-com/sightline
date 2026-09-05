"""DataForSEO rank/keyword check with an early coverage gate.

Every DataForSEO endpoint has its own pricing tier and per-account
authorization. Rather than fail an entire scan when one endpoint is not
covered, we probe each endpoint we intend to use with a tiny live call and
emit severity='unavailable' findings for the ones that come back 40x. The
prospect is never penalized for our tooling gap.
"""
from __future__ import annotations

import base64

import requests

from ..config import settings
from .base import Finding, ScanContext

CHECK_ID = "rank"

BASE = "https://api.dataforseo.com/v3"
# Endpoints we depend on, tagged for clarity in evidence.
DEP_KEYWORDS = "dataforseo_labs/google/ranked_keywords/live"
# Backlinks Summary is the correct endpoint for backlink counts. The
# earlier whois/overview endpoint is a WHOIS record whose backlinks/
# referring_domains fields are almost always zero on our account tier —
# not the actual backlink graph.
DEP_BACKLINKS = "backlinks/summary/live"


def _auth_header() -> dict[str, str] | None:
    if not (settings.dataforseo_login and settings.dataforseo_password):
        return None
    token = base64.b64encode(
        f"{settings.dataforseo_login}:{settings.dataforseo_password}".encode()
    ).decode()
    return {"Authorization": f"Basic {token}",
            "Content-Type": "application/json"}


def _call(endpoint: str, payload: list[dict], headers: dict[str, str]) -> dict:
    try:
        r = requests.post(f"{BASE}/{endpoint}", headers=headers,
                          json=payload, timeout=settings.http_timeout * 3)
        try:
            body = r.json()
        except ValueError:
            body = {"_raw": r.text[:400]}
        return {"http_status": r.status_code, "body": body}
    except requests.RequestException as e:
        return {"http_status": 0, "error": str(e)}


def _task_status(body: dict) -> tuple[int | None, str]:
    tasks = (body or {}).get("tasks") or []
    if not tasks:
        return None, "no tasks in response"
    t = tasks[0]
    return t.get("status_code"), (t.get("status_message") or "")


def _unavailable(item_key: str, endpoint: str, reason: str,
                 evidence: dict) -> Finding:
    return Finding(
        check_id=CHECK_ID, item_key=item_key,
        severity="unavailable",
        examined=f"DataForSEO {endpoint}",
        observed=f"Endpoint not available on this account: {reason}",
        remediation=(
            "Enable the endpoint on your DataForSEO plan, or leave this "
            "section marked as not measured."
        ),
        evidence={"endpoint": endpoint, **evidence},
    )


def run(ctx: ScanContext) -> list[Finding]:
    findings: list[Finding] = []
    headers = _auth_header()
    if headers is None:
        return [Finding(
            check_id=CHECK_ID, item_key="unavailable",
            severity="unavailable",
            examined="DataForSEO rank and keyword measurement",
            observed="DATAFORSEO_LOGIN/DATAFORSEO_PASSWORD not configured; "
                     "check skipped.",
            remediation=(
                "Set DataForSEO credentials to enable rank and keyword findings."
            ),
        )]

    # Ranked keywords probe.
    kw = _call(DEP_KEYWORDS, [{
        "target": ctx.domain,
        "language_code": "en",
        "location_code": 2840,
        "limit": 10,
    }], headers)
    kw_status, kw_msg = _task_status(kw.get("body", {}))
    if kw.get("http_status") in (401, 402, 403) or kw_status in (40100, 40200, 40300):
        findings.append(_unavailable(
            "endpoint:keywords", DEP_KEYWORDS,
            f"HTTP {kw.get('http_status')} / task status {kw_status} ({kw_msg})",
            {"http_status": kw.get("http_status"), "task_status": kw_status},
        ))
    elif kw.get("http_status") != 200 or kw_status not in (20000,):
        findings.append(_unavailable(
            "endpoint:keywords", DEP_KEYWORDS,
            f"unexpected response HTTP {kw.get('http_status')} / task {kw_status} ({kw_msg})",
            {"http_status": kw.get("http_status"), "task_status": kw_status},
        ))
    else:
        result = ((kw["body"].get("tasks") or [{}])[0]
                  .get("result") or [{}])[0] or {}
        items = result.get("items") or []
        total = result.get("total_count") or 0
        top = [{
            "keyword": (it.get("keyword_data") or {}).get("keyword"),
            "position": ((it.get("ranked_serp_element") or {})
                         .get("serp_item") or {}).get("rank_absolute"),
            "search_volume": (((it.get("keyword_data") or {})
                               .get("keyword_info") or {}).get("search_volume")),
        } for it in items[:10]]

        if total == 0:
            findings.append(Finding(
                check_id=CHECK_ID, item_key="keyword_count",
                severity="high",
                examined="Organic keyword footprint (US, en)",
                observed=(
                    f"DataForSEO returns 0 ranked keywords for {ctx.domain}. "
                    "The domain is not visibly ranking in Google's US index."
                ),
                remediation=(
                    "Investigate indexation and content coverage. A domain with "
                    "zero ranked keywords is either new, deindexed, or heavily "
                    "outranked. Diagnose which."
                ),
                evidence={"total": 0},
            ))
        elif total < 25:
            findings.append(Finding(
                check_id=CHECK_ID, item_key="keyword_count",
                severity="medium",
                examined="Organic keyword footprint (US, en)",
                observed=f"{total} ranked keywords in Google US.",
                remediation=(
                    "A small keyword footprint suggests thin topical coverage. "
                    "Expand content aligned to search intent in your service area."
                ),
                evidence={"total": total},
            ))
        else:
            findings.append(Finding(
                check_id=CHECK_ID, item_key="keyword_count",
                severity="pass",
                examined="Organic keyword footprint (US, en)",
                observed=f"{total} ranked keywords in Google US.",
                evidence={"total": total},
            ))

        findings.append(Finding(
            check_id=CHECK_ID, item_key="top_keywords",
            severity="info",
            examined="Top ranked keywords",
            observed=(
                f"Top {len(top)} keyword(s) by rank." if top else
                "No top keywords returned."
            ),
            evidence={"top": top},
        ))

    # Backlinks Summary probe.
    bl = _call(DEP_BACKLINKS, [{"target": ctx.domain}], headers)
    bl_status, bl_msg = _task_status(bl.get("body", {}))
    if bl.get("http_status") in (401, 402, 403) or bl_status in (40100, 40200, 40300):
        findings.append(_unavailable(
            "endpoint:backlinks", DEP_BACKLINKS,
            f"HTTP {bl.get('http_status')} / task status {bl_status} ({bl_msg})",
            {"http_status": bl.get("http_status"), "task_status": bl_status},
        ))
    elif bl.get("http_status") != 200 or bl_status not in (20000,):
        findings.append(_unavailable(
            "endpoint:backlinks", DEP_BACKLINKS,
            f"unexpected response HTTP {bl.get('http_status')} / task {bl_status} ({bl_msg})",
            {"http_status": bl.get("http_status"), "task_status": bl_status},
        ))
    else:
        result = ((bl["body"].get("tasks") or [{}])[0]
                  .get("result") or [{}])[0] or {}
        backlinks = int(result.get("backlinks") or 0)
        ref = int(result.get("referring_domains") or 0)
        ref_main = int(result.get("referring_main_domains") or 0)

        # Band by referring-domain count. Backlink volume alone is noisy
        # (one spammy source can inflate it); referring-domain count is the
        # authority signal.
        if ref == 0:
            sev = "high"
            note = (
                f"DataForSEO reports 0 referring domains for {ctx.domain}. "
                "The domain has no measurable inbound link graph."
            )
            remed = (
                "Investigate: is the domain new, deindexed from major "
                "backlink crawlers, or genuinely not linked to? Established "
                "sites with zero referring domains are unusual."
            )
        elif ref < 10:
            sev = "medium"
            note = f"{ref} referring domain(s), {backlinks:,} total backlink(s)."
            remed = (
                "A thin backlink graph limits authority signals. Earn links "
                "from industry publications, directories, and partners."
            )
        elif ref < 50:
            sev = "low"
            note = f"{ref} referring domains, {backlinks:,} backlinks."
            remed = "Continue link acquisition; the graph is thin but present."
        else:
            sev = "pass"
            note = f"{ref} referring domains, {backlinks:,} backlinks."
            remed = ""
        findings.append(Finding(
            check_id=CHECK_ID, item_key="backlinks",
            severity=sev,
            examined="Backlink graph (referring domains + total backlinks)",
            observed=note,
            remediation=remed,
            evidence={"backlinks": backlinks,
                      "referring_domains": ref,
                      "referring_main_domains": ref_main},
        ))

    return findings
