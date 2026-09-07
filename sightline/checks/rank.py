"""DataForSEO rank/keyword check with an early coverage gate.

Every DataForSEO endpoint has its own pricing tier and per-account
authorization. Rather than fail an entire scan when one endpoint is not
covered, we probe each endpoint we intend to use with a tiny live call and
emit severity='unavailable' findings for the ones that come back 40x. The
prospect is never penalized for our tooling gap.

This check also feeds the SEO score. It makes one extra call (Rank
Overview) and stashes the raw numbers on ctx.seo_metrics; the pipeline
scores them in scoring/seo.py. That call deliberately emits NO finding:
the SEO score is a separate number, and adding findings here would shift
the AEO composite's `authority` dimension as a side effect of building it.
The audit trail for those numbers lives in sightline_scans.seo_metrics.
"""
from __future__ import annotations

from .. import dataforseo as dfs
from .base import Finding, ScanContext

CHECK_ID = "rank"


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


def _status_evidence(res: dfs.DFSResult) -> dict:
    return {"http_status": res.http_status, "task_status": res.task_status}


def run(ctx: ScanContext) -> list[Finding]:
    findings: list[Finding] = []
    headers = dfs.auth_header()
    if headers is None:
        ctx.seo_metrics = dfs.metrics_from(
            dfs.DFSResult(endpoint=dfs.EP_RANK_OVERVIEW,
                          error="credentials not configured"),
            dfs.DFSResult(endpoint=dfs.EP_BACKLINKS,
                          error="credentials not configured"),
        )
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

    # Ranked keywords probe. Supplies the keyword NAMES for the report;
    # the SEO score takes its counts from Rank Overview instead so that
    # top-10 can never exceed the total.
    kw = dfs.ranked_keywords(ctx.domain, limit=10, headers=headers)
    if not kw.ok:
        findings.append(_unavailable(
            "endpoint:keywords", dfs.EP_RANKED_KEYWORDS, kw.reason,
            _status_evidence(kw),
        ))
    else:
        result = kw.result or {}
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

    # Rank Overview: the position distribution behind the SEO score. No
    # finding either way — see the module docstring.
    overview = dfs.rank_overview(ctx.domain, headers=headers)

    # Backlinks Summary probe.
    bl = dfs.backlinks_summary(ctx.domain, headers=headers)
    if not bl.ok:
        findings.append(_unavailable(
            "endpoint:backlinks", dfs.EP_BACKLINKS, bl.reason,
            _status_evidence(bl),
        ))
    else:
        result = bl.result or {}
        backlinks = int(result.get("backlinks") or 0)
        ref = int(result.get("referring_domains") or 0)
        ref_main = int(result.get("referring_main_domains") or 0)
        domain_rank = int(result.get("rank") or 0)

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
                      "referring_main_domains": ref_main,
                      "domain_rank": domain_rank},
        ))

    # Hand the raw numbers to the pipeline. Both endpoints are read here
    # so a scan makes no extra calls for the SEO score.
    ctx.seo_metrics = dfs.metrics_from(overview, bl)

    return findings
