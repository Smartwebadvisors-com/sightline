"""PageSpeed Insights. Mobile + desktop. We emit findings for Core Web
Vitals thresholds and for crawlability/indexation signals PSI surfaces.
"""
from __future__ import annotations

import requests

from .. import unavailable
from ..config import settings
from .base import Finding, ScanContext

CHECK_ID = "pagespeed"
PSI_URL = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"

# Core Web Vitals thresholds. Lower is better for LCP, FCP, TBT, CLS.
# Values from the CWV documented "Good / Needs Improvement / Poor" bands.
CWV_BANDS = {
    "largest-contentful-paint": {"good": 2500, "poor": 4000, "unit": "ms", "label": "LCP"},
    "first-contentful-paint":   {"good": 1800, "poor": 3000, "unit": "ms", "label": "FCP"},
    "total-blocking-time":      {"good": 200,  "poor": 600,  "unit": "ms", "label": "TBT"},
    "cumulative-layout-shift":  {"good": 0.1,  "poor": 0.25, "unit": "",   "label": "CLS"},
    "interaction-to-next-paint": {"good": 200, "poor": 500,  "unit": "ms", "label": "INP"},
}


def _fetch(url: str, strategy: str) -> tuple[dict | None, str]:
    """Returns (payload, "") or (None, reason).

    Never raises, and never hands back response text. PSI answers a bad
    key or a server fault with a full HTML error page, and `r.text[:300]`
    of that used to land verbatim in the rendered report.
    """
    service = f"PageSpeed Insights ({strategy})"
    params = {"url": url, "strategy": strategy,
              "key": settings.pagespeed_api_key,
              "category": ["performance", "seo"]}
    r, why = unavailable.send_with_retry(
        lambda: requests.get(PSI_URL, params=params,
                             timeout=settings.http_timeout * 3),
        service=service,
    )
    if r is None:
        return None, why
    if r.status_code != 200:
        # Not retried: a 4xx is a statement about our request, not a blip.
        return None, unavailable.reason(service, status=r.status_code)
    try:
        return r.json(), ""
    except ValueError:
        return None, unavailable.reason(
            service, detail="returned a response we could not read")


def _band(metric_key: str, value: float) -> str:
    band = CWV_BANDS.get(metric_key)
    if not band:
        return "info"
    if value <= band["good"]:
        return "pass"
    if value <= band["poor"]:
        return "medium"
    return "high"


def run(ctx: ScanContext) -> list[Finding]:
    findings: list[Finding] = []
    if not settings.pagespeed_api_key:
        return [unavailable.unmeasured(
            CHECK_ID, "unavailable",
            "Core Web Vitals via PageSpeed Insights",
            unavailable.not_configured("PageSpeed Insights",
                                       "PAGESPEED_API_KEY"),
        )]

    for strategy in ("mobile", "desktop"):
        data, why = _fetch(ctx.url, strategy)
        if data is None:
            findings.append(unavailable.unmeasured(
                CHECK_ID, f"{strategy}:error",
                f"PageSpeed Insights ({strategy})", why,
            ))
            continue

        lh = data.get("lighthouseResult", {})
        audits = lh.get("audits", {})
        categories = lh.get("categories", {})
        perf_score = categories.get("performance", {}).get("score")
        seo_score = categories.get("seo", {}).get("score")

        # Category-level score for context (info, no deduction).
        if perf_score is not None:
            perf_pct = round(perf_score * 100)
            sev = "pass" if perf_pct >= 90 else "info"
            findings.append(Finding(
                check_id=CHECK_ID, item_key=f"{strategy}:perf_category",
                severity=sev,
                examined=f"PSI Performance category ({strategy})",
                observed=f"Lighthouse Performance score: {perf_pct}/100.",
                evidence={"score": perf_pct, "strategy": strategy},
            ))

        # Per-metric CWV.
        for metric_key, band in CWV_BANDS.items():
            audit = audits.get(metric_key)
            if not audit or audit.get("numericValue") is None:
                continue
            value = audit["numericValue"]
            display = audit.get("displayValue", "")
            sev = _band(metric_key, value)
            item = f"{strategy}:{band['label']}"
            if sev == "pass":
                findings.append(Finding(
                    check_id=CHECK_ID, item_key=item,
                    severity="pass",
                    examined=f"{band['label']} ({strategy})",
                    observed=f"{band['label']} = {display} — within the 'good' threshold.",
                    evidence={"value": value, "strategy": strategy,
                              "good": band["good"], "poor": band["poor"]},
                ))
            else:
                findings.append(Finding(
                    check_id=CHECK_ID, item_key=item,
                    severity=sev,
                    examined=f"{band['label']} ({strategy})",
                    observed=(
                        f"{band['label']} = {display}. "
                        f"'Good' threshold is {band['good']}{band['unit']}, "
                        f"'poor' begins at {band['poor']}{band['unit']}."
                    ),
                    remediation={
                        "largest-contentful-paint": (
                            "Reduce LCP by serving the hero image at the right size, "
                            "preloading it, and cutting critical-path JS/CSS."),
                        "first-contentful-paint": (
                            "Reduce FCP by cutting render-blocking resources and "
                            "inlining critical CSS."),
                        "total-blocking-time": (
                            "Reduce TBT by splitting long JS tasks and deferring "
                            "non-critical third-party scripts."),
                        "cumulative-layout-shift": (
                            "Reduce CLS by reserving space for images/embeds and "
                            "avoiding late-inserted content above the fold."),
                        "interaction-to-next-paint": (
                            "Reduce INP by profiling long JS handlers on interaction "
                            "and moving work off the main thread."),
                    }.get(metric_key, ""),
                    evidence={"value": value, "strategy": strategy,
                              "good": band["good"], "poor": band["poor"]},
                ))

        # Indexation / crawlability signals from the SEO category.
        if strategy == "mobile":
            for audit_id in ("is-crawlable", "robots-txt", "canonical",
                             "hreflang", "http-status-code",
                             "viewport", "document-title", "meta-description"):
                a = audits.get(audit_id)
                if not a:
                    continue
                score = a.get("score")
                if score is None:
                    continue
                if score >= 0.9:
                    sev = "pass"
                elif score >= 0.5:
                    sev = "low"
                else:
                    sev = "medium"
                # Only surface non-pass to keep the report tight; but keep
                # crawlability at pass since it's the most important.
                if sev == "pass" and audit_id != "is-crawlable":
                    continue
                findings.append(Finding(
                    check_id=CHECK_ID, item_key=f"seo:{audit_id}",
                    severity=sev,
                    examined=f"PSI SEO audit: {a.get('title', audit_id)}",
                    observed=(a.get("description") or "")[:400],
                    evidence={"score": score, "audit": audit_id},
                ))

        # Category info line.
        if seo_score is not None:
            findings.append(Finding(
                check_id=CHECK_ID, item_key=f"{strategy}:seo_category",
                severity="info",
                examined=f"PSI SEO category ({strategy})",
                observed=f"Lighthouse SEO score: {round(seo_score * 100)}/100.",
                evidence={"score": round(seo_score * 100)},
            ))

    return findings
