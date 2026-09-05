"""HTML report renderer. Standalone file — inline CSS, no external assets,
prints cleanly. This is the artifact you hand to a prospect.

Under v2 the headline is six per-dimension scores; the overall score is a
smaller line beneath. Coverage is explicit so a scan with an unmeasured
dimension doesn't read as if it just failed."""
from __future__ import annotations

import html as html_lib

from .. import db
from ..scoring.report import compute_report


SEVERITY_ORDER = ["critical", "high", "medium", "low", "info", "pass", "unavailable"]
SEVERITY_COLOR = {
    "critical": "#8a1616", "high": "#c04b1a", "medium": "#b3841c",
    "low": "#4a7a3a",      "info": "#3a5a7a",  "pass": "#2f6a3a",
    "unavailable": "#6a6a6a",
}
CHECK_LABEL = {
    "ai_crawler":         "AI crawler accessibility",
    "structured_data":    "Structured data",
    "entity_consistency": "Entity consistency",
    "answer_first":       "Answer-first content",
    "llms_txt":           "llms.txt",
    "pagespeed":          "Core Web Vitals & technical SEO",
    "rank":               "Keyword footprint & backlinks",
}


def _esc(s) -> str:
    if s is None:
        return ""
    return html_lib.escape(str(s))


def _score_color(score) -> str:
    if score is None:
        return "#888"
    if score >= 90: return "#2f6a3a"
    if score >= 75: return "#4a7a3a"
    if score >= 60: return "#b3841c"
    if score >= 40: return "#c04b1a"
    return "#8a1616"


def _sev_pill(sev: str) -> str:
    c = SEVERITY_COLOR.get(sev, "#666")
    return (f'<span class="sev" style="background:{c}">{_esc(sev)}</span>')


def render_scan(scan_id: int, weight_version_id: int) -> str:
    scan = db.scan(scan_id)
    if not scan:
        raise ValueError(f"scan {scan_id} not found")
    wv = db.get_weight_version_by_id(weight_version_id)
    if not wv:
        raise ValueError(f"weight version id {weight_version_id} not found")

    report = compute_report(scan_id, weight_version_id)
    dim_scores = report["dimensions"]
    dim_data = report["dim_data"]
    overall = report["overall"]
    coverage = report["coverage"]
    unmapped = report["unmapped_findings"]

    dim_labels = wv["weights"].get("dimension_labels", {})
    dim_order = (wv["weights"].get("dimension_order")
                 or list(dim_scores.keys()))

    # Top drivers: findings with the largest deduction across all scored
    # dimensions.
    drivers: list[dict] = []
    for dim in dim_data.values():
        for f in dim["findings"]:
            if (f["deduction"] or 0) > 0 and f["severity"] != "unavailable":
                drivers.append(f)
    drivers.sort(key=lambda f: -float(f["deduction"] or 0))
    drivers = drivers[:6]

    css = """
    body { font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
           color: #222; max-width: 920px; margin: 40px auto; padding: 0 24px; }
    h1, h2, h3 { line-height: 1.2; }
    h1 { font-size: 28px; margin: 0 0 4px; }
    h2 { font-size: 20px; margin: 32px 0 8px; border-bottom: 1px solid #ddd;
         padding-bottom: 4px; }
    h2 .capscore { color: #666; font-size: 14px; font-weight: 400; }
    .sub { color: #666; margin: 0 0 24px; }
    .dim-grid { display: grid; grid-template-columns: repeat(6, 1fr);
                gap: 8px; margin: 24px 0 8px; }
    .dim-tile { border: 1px solid #ddd; border-radius: 6px; padding: 14px 10px;
                text-align: center; background: #fafafa; }
    .dim-tile .dim-label { font-size: 11px; color: #666;
                           text-transform: uppercase; letter-spacing: 0.03em;
                           font-weight: 600; }
    .dim-tile .dim-score { font-size: 32px; font-weight: 700; line-height: 1.1;
                           margin-top: 4px; font-variant-numeric: tabular-nums; }
    .dim-tile .dim-cov { font-size: 11px; color: #888; margin-top: 4px; }
    .overall-line { display: flex; justify-content: space-between; align-items: baseline;
                    padding: 12px 4px; color: #444; font-size: 14px; }
    .overall-line .overall-num { font-size: 20px; font-weight: 600; color: #222;
                                 font-variant-numeric: tabular-nums; }
    table { border-collapse: collapse; width: 100%; margin: 8px 0 16px; }
    th, td { border-bottom: 1px solid #eee; padding: 6px 8px; text-align: left; vertical-align: top; }
    th { background: #f6f6f6; font-weight: 600; font-size: 13px; }
    td.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
    .sev { display: inline-block; padding: 1px 8px; border-radius: 10px;
           color: white; font-size: 11px; font-weight: 600; text-transform: uppercase;
           letter-spacing: 0.02em; }
    .finding { border: 1px solid #eee; border-radius: 6px; padding: 12px 14px;
               margin: 8px 0; }
    .finding.pass { background: #f7fbf7; }
    .finding.unavailable { background: #f5f5f5; color: #555; }
    .finding.info { background: #f6f8fb; }
    .finding .row { display: flex; gap: 8px; align-items: baseline; }
    .finding .head { font-weight: 600; }
    .finding .dedn { color: #8a1616; font-variant-numeric: tabular-nums; margin-left: auto; }
    .finding .body { color: #333; margin-top: 6px; }
    .finding .remed { color: #444; margin-top: 6px; font-style: italic; font-size: 14px; }
    .disclaimer { color: #666; font-size: 13px; border-left: 3px solid #ddd;
                  padding: 6px 12px; margin: 24px 0; }
    .muted { color: #888; }
    .footer-note { color: #999; font-size: 12px; margin-top: 32px; }
    """

    p: list[str] = []
    p.append("<!doctype html><html><head><meta charset='utf-8'>")
    p.append(f"<title>Sightline report — {_esc(scan['domain'])}</title>")
    p.append(f"<style>{css}</style></head><body>")

    p.append("<h1>Sightline report</h1>")
    p.append(
        f"<p class='sub'>{_esc(scan['url'])} &middot; scanned "
        f"{scan['requested_at'].strftime('%Y-%m-%d %H:%M UTC')} &middot; "
        f"weights <code>{_esc(wv['version'])}</code></p>"
    )

    # Dimension grid — the headline.
    p.append("<div class='dim-grid'>")
    for dim in dim_order:
        if dim not in dim_data:
            continue
        entry = dim_data[dim]
        score = dim_scores.get(dim)
        color = _score_color(score)
        label = _esc(dim_labels.get(dim, dim))
        n_scored = entry["n_scored"]
        n_unavail = entry["n_unavailable"]
        cov = (f"{n_scored} scored"
               + (f", {n_unavail} n/a" if n_unavail else ""))
        score_txt = ("—" if score is None else f"{score:.0f}")
        p.append(
            f"<div class='dim-tile'>"
            f"<div class='dim-label'>{label}</div>"
            f"<div class='dim-score' style='color:{color}'>{score_txt}</div>"
            f"<div class='dim-cov'>{cov}</div>"
            f"</div>"
        )
    p.append("</div>")

    # Overall + coverage.
    overall_txt = ("—" if overall is None else f"{overall:.0f} / 100")
    cov_line = (
        f"scored on {coverage['scored']} of {coverage['total']} finding(s)"
        + (f"; {coverage['unavailable']} unavailable"
           if coverage["unavailable"] else "")
    )
    p.append("<div class='overall-line'>")
    p.append(f"<div>Overall (mean of scored dimensions): "
             f"<span class='overall-num'>{overall_txt}</span></div>")
    p.append(f"<div class='muted'>{cov_line} &middot; scan id {scan_id}</div>")
    p.append("</div>")

    # Top drivers.
    if drivers:
        p.append("<h2>Why this score</h2>")
        p.append("<table>")
        p.append("<tr><th>Dimension</th><th>Finding</th><th class='num'>Points</th></tr>")
        for d in drivers:
            dim = next((dname for dname, dd in dim_data.items()
                        if d in dd["findings"]), None)
            dim_label = dim_labels.get(dim, dim or "")
            p.append(
                f"<tr><td>{_esc(dim_label)}</td>"
                f"<td>{_sev_pill(d['severity'])} {_esc(d['observed'])[:180]}</td>"
                f"<td class='num'>&minus;{float(d['deduction'] or 0):.1f}</td></tr>"
            )
        p.append("</table>")
        p.append("<p class='muted'>Every point above traces to a specific "
                 "finding, listed in full below.</p>")

    # Per-dimension sections.
    for dim in dim_order:
        if dim not in dim_data:
            continue
        entry = dim_data[dim]
        if not entry["findings"]:
            continue
        label = _esc(dim_labels.get(dim, dim))
        score = dim_scores.get(dim)
        score_note = ("not measured" if score is None else f"{score:.0f} / 100")
        p.append(
            f"<h2>{label} "
            f"<span class='capscore'>{score_note}</span></h2>"
        )
        fs = sorted(entry["findings"], key=lambda f: (
            SEVERITY_ORDER.index(f["severity"]) if f["severity"] in SEVERITY_ORDER else 99,
            f["check_id"], f["item_key"],
        ))
        for f in fs:
            cls = ""
            if f["severity"] in ("pass", "unavailable", "info"):
                cls = " " + f["severity"]
            p.append(f"<div class='finding{cls}'>")
            p.append("<div class='row'>")
            p.append(f"<div class='head'>{_sev_pill(f['severity'])} "
                     f"{_esc(f['examined'])}</div>")
            if (f["deduction"] or 0) > 0:
                p.append(f"<div class='dedn'>&minus;{float(f['deduction']):.1f}</div>")
            p.append("</div>")
            p.append(f"<div class='body'>{_esc(f['observed'])}</div>")
            if f["remediation"]:
                p.append(f"<div class='remed'>{_esc(f['remediation'])}</div>")
            p.append("</div>")

    # Informational (unmapped) findings — llms.txt lives here.
    if unmapped:
        p.append("<h2>Informational <span class='capscore'>not scored</span></h2>")
        p.append("<p class='muted'>These checks run and appear in the report "
                 "but do not affect the score. llms.txt is not a documented "
                 "ranking signal; the scan reports its state as operational "
                 "hygiene, not authority.</p>")
        for f in unmapped:
            cls = " info"
            p.append(f"<div class='finding{cls}'>")
            p.append("<div class='row'>")
            p.append(f"<div class='head'>{_sev_pill(f['severity'])} "
                     f"{_esc(f['examined'])}</div>")
            p.append("</div>")
            p.append(f"<div class='body'>{_esc(f['observed'])}</div>")
            if f["remediation"]:
                p.append(f"<div class='remed'>{_esc(f['remediation'])}</div>")
            p.append("</div>")

    # AV visibility.
    av = db.av_summary(scan["domain"])
    p.append("<h2>Assistant visibility <span class='capscore'>measurement</span></h2>")
    n = av["n_samples"] or 0
    if n == 0:
        p.append("<p class='muted'>No assistant-visibility samples recorded "
                 "for this domain yet.</p>")
    elif n < 10:
        p.append(f"<p><b>Sample size: {n}</b> in the last {av['window_days']} "
                 "days. This is insufficient for a trend claim — treat as an "
                 "early measurement, not a rank.</p>")
    else:
        rate = av["visibility_rate"]
        p.append(f"<p>Sample size: {n} in the last {av['window_days']} days. "
                 f"Visibility rate: {rate:.0%}. This is a repeated measurement "
                 "with real variance, not a rank.</p>")

    p.append("<div class='disclaimer'>")
    p.append("<b>How to read this report.</b> Each dimension score is "
             "100 &times; (points earned / points available), where "
             "'available' counts only checks that actually ran. A dimension "
             "with unavailable findings is smaller in weight, not counted as "
             "either good or bad. Overall is the straight mean of the "
             "dimensions that had at least one scored finding. This report "
             "does not claim that any listed tactic will cause a change in "
             "Google or assistant rankings; ranking systems are third-party "
             "and not controlled by Sightline. Assistant-visibility numbers "
             "are sampled measurements with real variance and should be read "
             "as trends over sample size, not as ranks.")
    p.append("</div>")

    p.append(f"<div class='footer-note'>Sightline scan #{scan_id} &middot; "
             f"weights <code>{_esc(wv['version'])}</code></div>")
    p.append("</body></html>")
    return "".join(p)
