"""HTML report renderer. Standalone file — inline CSS, no external assets,
prints cleanly.

INTERNAL AND TECHNICAL. This view renders each finding's `technical` block,
which names formats and specs in our own vocabulary — it is for us and for a
client's developer, not for the client. The client-facing surface is Cited,
fed by render/json_export.py, which renders `plain`. Handing this file to a
prospect puts "JSON-LD" and "CWV" in front of someone who has never heard
either, which COPY.md rule 1 exists to prevent.

Severity is not rendered here. It is a scoring input; what a reader sees per
finding is impact, effort and owner as separate labelled values (rule 4).

Under v2 the headline is six per-dimension scores; the overall score is a
smaller line beneath. Coverage is explicit so a scan with an unmeasured
dimension doesn't read as if it just failed.

The SEO score gets its own section rather than a seventh tile in the
dimension grid. It is measured from DataForSEO rather than deducted from
findings, and it is NOT part of the Overall mean -- putting it in the grid
would imply both."""
from __future__ import annotations

import html as html_lib

from .. import db
from .. import findings as findings_mod
from ..scoring.report import compute_report


SEVERITY_ORDER = ["critical", "high", "medium", "low", "info", "pass", "unavailable"]
# Severity still orders the list (worst first) but is never printed.
IMPACT_COLOR = {"high": "#8a1616", "medium": "#b3841c", "low": "#4a7a3a"}
OWNER_LABEL = {"client": "you do this", "swa": "we do this"}
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


def _impact_pill(impact: str) -> str:
    c = IMPACT_COLOR.get(impact, "#666")
    return f'<span class="impact" style="background:{c}">{_esc(impact)}</span>'


def _task_meta(f) -> str:
    """Impact, effort and owner as three labelled values. Never fused into a
    phrase — COPY.md rule 4.

    Also carries severity in a `class="sev"` span. That is NOT redundant with
    impact: severity is the scoring input, impact is the business consequence,
    and this view is internal so both are legitimate here. It is also load-
    bearing for Cited, which parses this report with
    /class=['"]sev['"][^>]*>(.*?)<\/span>/ and DEFAULTS TO "info" when the
    match fails — see /opt/cited/src/lib/sightline/dashboard.ts. A silent
    default of "info" makes every check read 'pass' and empties the
    recommendation list, so removing this span tells every Cited client their
    site is clean. Do not remove it until Cited reads findings.json instead;
    tests/test_findings_copy.py pins it.
    """
    return (
        "<div class='task'>"
        f"<span><b>Impact</b> {_esc(f.impact)}</span>"
        f"<span><b>Effort</b> {f.effort_minutes} min</span>"
        f"<span><b>Owner</b> {_esc(OWNER_LABEL.get(f.owner, f.owner))}</span>"
        f"<span><b>Severity</b> "
        f'<span class="sev">{_esc(f.severity)}</span></span>'
        "</div>"
    )


def _seo_section(scan: dict, peers: dict | None = None) -> list[str]:
    """The SEO score block: one headline number plus the four component
    metrics behind it, read straight off sightline_scans.

    Presented like the dimension grid because it is the same kind of
    object -- a 0-100 score with per-component coverage -- but kept in its
    own section, with its own CSS classes, because it is measured rather
    than deducted and does not feed Overall.
    """
    from ..scoring.seo import COMPONENT_ORDER

    metrics = scan.get("seo_metrics") or {}
    score = scan.get("seo_score")
    components = metrics.get("components") or {}

    out: list[str] = []
    version = metrics.get("version") or ""
    score_txt = findings_mod.score_phrase(score)
    cap = score_txt + (f" &middot; {_esc(version)}" if version else "")
    out.append(f"<h2>Organic search presence "
               f"<span class='capscore'>{cap}</span></h2>")

    if score is None and not components:
        out.append("<p class='muted'>No SEO measurement recorded for this "
                   "scan. Either it predates the SEO score or no DataForSEO "
                   "endpoint could be reached &mdash; this is not a score of "
                   "zero.</p>")
        return out

    # The "free to disagree" hedge that used to live here is now part of the
    # single disclaimer under the score block (rule 8).
    out.append("<p class='muted'>Measured from DataForSEO &mdash; keyword "
               "footprint and link graph, scored on absolute values rather "
               "than deducted from the findings above.</p>")

    # jsonb does not preserve key order, so impose the canonical one and
    # append anything a different score version left behind.
    names = [n for n in COMPONENT_ORDER if n in components]
    names += [n for n in components if n not in names]

    out.append("<div class='seo-grid'>")
    for name in names:
        c = components.get(name) or {}
        cscore = c.get("score")
        label = _esc(c.get("label") or name.replace("_", " ").title())
        if cscore is None:
            out.append(f"<div class='seo-tile'>"
                       f"<div class='seo-label'>{label}</div>"
                       f"<div class='seo-score' style='color:#888'>&mdash;</div>"
                       f"<div class='seo-cov'>not measured</div></div>")
            continue
        value, weight = c.get("value"), c.get("weight")
        sub = f"{value:,}" if isinstance(value, (int, float)) else _esc(value)
        if weight is not None:
            sub += f" &middot; weight {weight:g}"
        out.append(f"<div class='seo-tile'>"
                   f"<div class='seo-label'>{label}</div>"
                   f"<div class='seo-score' style='color:{_score_color(cscore)}'>"
                   f"{cscore:.0f}</div>"
                   f"<div class='seo-cov'>{sub}</div></div>")
    out.append("</div>")

    covered = metrics.get("covered_weight")
    notes: list[str] = []
    if isinstance(covered, (int, float)) and covered < 1.0:
        pretty = ", ".join(
            _esc((components.get(n) or {}).get("label") or n)
            for n in (metrics.get("unmeasured") or [])
        )
        notes.append(f"{covered:.0%} of weight measured"
                     + (f"; no data for {pretty}" if pretty else ""))
    else:
        notes.append("all components measured")
    if metrics.get("backfilled"):
        when = str(metrics.get("measured_at") or "")[:10]
        notes.append(f"measured {when or 'later'}, after this scan ran")

    out.append("<div class='overall-line'>")
    out.append("<div>SEO score (weighted mean of measured components): "
               f"<span class='seo-num'>{score_txt}</span></div>")
    out.append(f"<div class='muted'>{' &middot; '.join(notes)}</div>")
    out.append("</div>")
    peer = findings_mod.peer_sentence(score, peers)
    if peer:
        out.append(f"<p class='peer'>{_esc(peer)}</p>")
    return out


def render_scan(scan_id: int, weight_version_id: int) -> str:
    scan = db.scan(scan_id)
    if not scan:
        raise ValueError(f"scan {scan_id} not found")
    wv = db.get_weight_version_by_id(weight_version_id)
    if not wv:
        raise ValueError(f"weight version id {weight_version_id} not found")

    report = compute_report(scan_id, weight_version_id)
    profile = findings_mod.profile_from_meta(scan.get("meta"))
    dim_scores = report["dimensions"]
    dim_data = report["dim_data"]
    overall = report["overall"]
    coverage = report["coverage"]
    unmapped = report["unmapped_findings"]

    dim_labels = wv["weights"].get("dimension_labels", {})
    dim_order = (wv["weights"].get("dimension_order")
                 or list(dim_scores.keys()))

    # Rebuild every stored row through the findings module, so this view and
    # Cited read the same two blocks from the same templates. Nothing here
    # writes a client-facing sentence.
    for entry in dim_data.values():
        entry["built"] = [findings_mod.from_row(r, profile)
                          for r in entry["findings"]]
    unmapped_built = [findings_mod.from_row(r, profile) for r in unmapped]

    # swa-owned findings are work we perform, not instructions to the client,
    # so they get their own section and leave the graded lists.
    ours = [f for e in dim_data.values() for f in e["built"] if f.owner == "swa"]
    ours += [f for f in unmapped_built if f.owner == "swa"]
    for entry in dim_data.values():
        entry["built"] = [f for f in entry["built"] if f.owner != "swa"]
    unmapped_built = [f for f in unmapped_built if f.owner != "swa"]

    # Top drivers: findings with the largest deduction across all dimensions.
    drivers = [f for e in dim_data.values() for f in e["built"]
               if (f.deduction or 0) > 0 and f.severity != "unavailable"]
    drivers.sort(key=lambda f: -float(f.deduction or 0))
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
    .seo-grid { display: grid; grid-template-columns: repeat(4, 1fr);
                gap: 8px; margin: 16px 0 8px; }
    .seo-tile { border: 1px solid #ddd; border-radius: 6px; padding: 14px 10px;
                text-align: center; background: #fafafa; }
    .seo-tile .seo-label { font-size: 11px; color: #666;
                           text-transform: uppercase; letter-spacing: 0.03em;
                           font-weight: 600; }
    .seo-tile .seo-score { font-size: 32px; font-weight: 700; line-height: 1.1;
                           margin-top: 4px; font-variant-numeric: tabular-nums; }
    .seo-tile .seo-cov { font-size: 11px; color: #888; margin-top: 4px; }
    .overall-line .seo-num { font-size: 20px; font-weight: 600; color: #222;
                             font-variant-numeric: tabular-nums; }
    .overall-line { display: flex; justify-content: space-between; align-items: baseline;
                    padding: 12px 4px; color: #444; font-size: 14px; }
    .overall-line .overall-num { font-size: 20px; font-weight: 600; color: #222;
                                 font-variant-numeric: tabular-nums; }
    .peer { color: #555; font-size: 13px; margin: 0 4px 8px; }
    .scale-note { color: #888; font-size: 12px; margin: 0 4px 4px; }
    table { border-collapse: collapse; width: 100%; margin: 8px 0 16px; }
    th, td { border-bottom: 1px solid #eee; padding: 6px 8px; text-align: left; vertical-align: top; }
    th { background: #f6f6f6; font-weight: 600; font-size: 13px; }
    td.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
    .impact { display: inline-block; padding: 1px 8px; border-radius: 10px;
              color: white; font-size: 11px; font-weight: 600;
              text-transform: uppercase; letter-spacing: 0.02em; }
    .task { display: flex; gap: 16px; margin-top: 8px; color: #444;
            font-size: 13px; }
    .task b { color: #666; font-weight: 600; }
    .ours { background: #f6f8fb; border-color: #dbe3ee; }
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
    .disclaimer { color: #555; font-size: 13px; border-left: 3px solid #ddd;
                  padding: 8px 12px; margin: 4px 0 20px; }
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
    p.append("<p class='scale-note'>Each dimension is scored out of 100.</p>")

    # Overall + coverage, then the one disclaimer, directly underneath.
    cov_line = (
        f"scored on {coverage['scored']} of {coverage['total']} finding(s)"
        + (f"; {coverage['unavailable']} unavailable"
           if coverage["unavailable"] else "")
    )
    p.append("<div class='overall-line'>")
    p.append(f"<div>Overall (mean of scored dimensions): "
             f"<span class='overall-num'>"
             f"{findings_mod.score_phrase(overall)}</span></div>")
    p.append(f"<div class='muted'>{cov_line} &middot; scan id {scan_id}</div>")
    p.append("</div>")
    peer = findings_mod.peer_sentence(
        overall, db.peer_overall(weight_version_id, scan["domain"]))
    if peer:
        p.append(f"<p class='peer'>{_esc(peer)}</p>")

    # Rule 8: one disclaimer, once, here. The three that used to be scattered
    # through this report (ranking systems, sampling variance, SEO-vs-
    # dimension divergence) are folded into findings.disclaimer().
    p.append(f"<div class='disclaimer'>{_esc(findings_mod.disclaimer(profile))}"
             "</div>")

    # SEO score. Its own section, directly under the AEO headline: both
    # are scores of the same site, but only the dimensions feed Overall.
    p.extend(_seo_section(scan, db.peer_seo_score(scan["domain"])))

    # Top drivers.
    if drivers:
        p.append("<h2>Why this score</h2>")
        p.append("<table>")
        p.append("<tr><th>Dimension</th><th>Finding</th><th class='num'>Points</th></tr>")
        for d in drivers:
            dim = next((dname for dname, dd in dim_data.items()
                        if d in dd["built"]), None)
            dim_label = dim_labels.get(dim, dim or "")
            p.append(
                f"<tr><td>{_esc(dim_label)}</td>"
                f"<td>{_impact_pill(d.impact)} "
                f"{_esc(d.technical.detail)[:180]}</td>"
                f"<td class='num'>&minus;{float(d.deduction or 0):.1f}</td></tr>"
            )
        p.append("</table>")
        p.append("<p class='muted'>Every point above traces to a specific "
                 "finding, listed in full below.</p>")

    # Per-dimension sections.
    for dim in dim_order:
        if dim not in dim_data:
            continue
        entry = dim_data[dim]
        if not entry["built"]:
            continue
        label = _esc(dim_labels.get(dim, dim))
        score = dim_scores.get(dim)
        score_note = findings_mod.score_phrase(score)
        p.append(
            f"<h2>{label} "
            f"<span class='capscore'>{score_note}</span></h2>"
        )
        fs = sorted(entry["built"], key=lambda f: (
            SEVERITY_ORDER.index(f.severity) if f.severity in SEVERITY_ORDER else 99,
            f.check_id, f.item_key,
        ))
        for f in fs:
            cls = ""
            if f.severity in ("pass", "unavailable", "info"):
                cls = " " + f.severity
            p.append(f"<div class='finding{cls}'>")
            p.append("<div class='row'>")
            p.append(f"<div class='head'>{_impact_pill(f.impact)} "
                     f"{_esc(f.technical.title)}</div>")
            if (f.deduction or 0) > 0:
                p.append(f"<div class='dedn'>&minus;{float(f.deduction):.1f}</div>")
            p.append("</div>")
            p.append(f"<div class='body'>{_esc(f.technical.detail)}</div>")
            if f.remediation:
                p.append(f"<div class='remed'>{_esc(f.remediation)}</div>")
            p.append(_task_meta(f))
            p.append("</div>")

    # Informational (unmapped) findings — llms.txt lives here.
    if unmapped_built:
        p.append("<h2>Informational <span class='capscore'>not scored</span></h2>")
        p.append("<p class='muted'>These checks run and appear in the report "
                 "but do not affect the score. llms.txt is not a documented "
                 "ranking signal; the scan reports its state as operational "
                 "hygiene, not authority.</p>")
        for f in unmapped_built:
            p.append("<div class='finding info'>")
            p.append("<div class='row'>")
            p.append(f"<div class='head'>{_impact_pill(f.impact)} "
                     f"{_esc(f.technical.title)}</div>")
            p.append("</div>")
            p.append(f"<div class='body'>{_esc(f.technical.detail)}</div>")
            if f.remediation:
                p.append(f"<div class='remed'>{_esc(f.remediation)}</div>")
            p.append(_task_meta(f))
            p.append("</div>")

    # Work we perform. Separate section so nothing owned by us reads as a
    # to-do list handed back to the client (COPY.md rule 3).
    if ours:
        p.append("<h2>Work we perform <span class='capscore'>not scored</span></h2>")
        p.append("<p class='muted'>Sightline runs these; they are not "
                 "instructions for the client and they do not affect any "
                 "score.</p>")
        for f in ours:
            p.append("<div class='finding ours'>")
            p.append("<div class='row'>")
            p.append(f"<div class='head'>{_impact_pill(f.impact)} "
                     f"{_esc(f.technical.title)}</div>")
            p.append("</div>")
            p.append(f"<div class='body'>{_esc(f.technical.detail)}</div>")
            p.append(_task_meta(f))
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
                 "days. Below the 10-sample floor for a trend claim.</p>")
    else:
        rate = av["visibility_rate"]
        p.append(f"<p>Sample size: {n} in the last {av['window_days']} days. "
                 f"Visibility rate: {rate:.0%}.</p>")

    # Scoring METHOD, not a disclaimer: it explains the arithmetic for the
    # internal reader and makes no hedge. The three hedges this block used to
    # also carry (ranking systems are third-party, sampling variance,
    # SEO-vs-dimension divergence) are now the single disclaimer rendered
    # under the score block — COPY.md rule 8, one disclaimer in one place.
    p.append("<p class='muted'><b>How this is scored.</b> Each dimension "
             "score is 100 &times; (points earned / points available), where "
             "'available' counts only checks that actually ran. A dimension "
             "with unavailable findings is smaller in weight, not counted as "
             "either good or bad. Overall is the straight mean of the "
             "dimensions that had at least one scored finding &mdash; the SEO "
             "score is not one of them, and is measured from DataForSEO's "
             "keyword and backlink data on absolute values.</p>")

    p.append(f"<div class='footer-note'>Sightline scan #{scan_id} &middot; "
             f"weights <code>{_esc(wv['version'])}</code></div>")
    p.append("</body></html>")
    return "".join(p)
