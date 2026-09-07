"""Sightline CLI. Cron-friendly, no daemon. Follows trendsignal's shape.

Commands:
  init-db                             create schema + register current weights
  scan <url>                          run all checks against a URL, score it
  rescore <scan_id|all> --version V   re-score history against a weight version
  backfill-seo [--dry-run] [--limit N]  measure seo_score for older scans
  render <scan_id> [--out FILE]       write standalone HTML report
  history <domain>                    list recent scans for a domain
  av-sample <url> --model M --query Q --mentioned yes|no [--excerpt TEXT]
"""
from __future__ import annotations

import argparse
import pathlib
import sys
from datetime import datetime, timezone

from . import db
from . import pipeline
from .fetch import http as fetch_http
from .scoring import weights as weights_mod
from .scoring import apply as apply_mod


def cmd_init_db(_args) -> None:
    sql_dir = pathlib.Path(__file__).resolve().parent.parent / "sql"
    db.init_schema(str(sql_dir))
    wv_id = pipeline.register_current_weights()
    print(f"schema initialized; weights '{weights_mod.WEIGHTS_VERSION}' "
          f"registered as version_id={wv_id}")


def cmd_scan(args) -> None:
    try:
        scan_id = pipeline.create_pending_scan(args.url)
    except ValueError as e:
        print(f"invalid URL: {e}", file=sys.stderr)
        sys.exit(1)
    scan = db.scan(scan_id)
    print(f"scan {scan_id} started for {scan['url']}")

    def _progress(check_id: str, n: int | None) -> None:
        if n is None:
            print(f"  ! {check_id} crashed")
        else:
            print(f"  {check_id.ljust(22)} {n} finding(s)")

    try:
        result = pipeline.run_scan_for(scan_id, scan["url"], progress=_progress)
    except RuntimeError as e:
        print(f"scan {scan_id} aborted: {e}", file=sys.stderr)
        sys.exit(1)
    print("")
    overall = result.get("overall")
    cov = result.get("coverage") or {}
    overall_txt = f"{overall:.1f}" if overall is not None else "n/a"
    print(f"overall: {overall_txt} / {int(weights_mod.MAX_SCORE)}  "
          f"(scored on {cov.get('scored', 0)} of {cov.get('total', 0)} "
          f"finding(s), weights={weights_mod.WEIGHTS_VERSION})")
    for dim_id, score in (result.get("dimensions") or {}).items():
        label = (weights_mod.DIMENSION_LABELS.get(dim_id) or dim_id).ljust(22)
        val = f"{score:.1f}" if score is not None else "n/a"
        print(f"  {label} {val:>6}")
    _print_seo(result.get("seo"))
    print(f"scan_id={scan_id}")


def _print_seo(seo: dict | None) -> None:
    """The SEO score is a separate number from the AEO composite above,
    so it prints as its own block rather than as another dimension."""
    print("")
    if not seo:
        print("seo: n/a (not scored)")
        return
    score = seo.get("score")
    score_txt = f"{score:.1f}" if score is not None else "n/a"
    covered = seo.get("covered_weight") or 0.0
    suffix = "" if covered >= 1.0 else f", {covered:.0%} of weight measured"
    print(f"seo: {score_txt} / 100  ({seo.get('version')}{suffix})")
    for name, c in (seo.get("components") or {}).items():
        label = (c.get("label") or name).ljust(22)
        if c.get("score") is None:
            print(f"  {label} {'n/a':>6}   not measured")
            continue
        print(f"  {label} {c['score']:>6}   "
              f"{c.get('value')} (weight {c.get('weight')})")


def cmd_rescore(args) -> None:
    snap = weights_mod.snapshot()
    wv_id = db.upsert_weight_version(
        args.version, weights_mod.WEIGHTS_DESCRIPTION, snap
    )
    print(f"weights '{args.version}' registered as version_id={wv_id}")
    if args.scan == "all":
        with db.conn() as c:
            scan_ids = [r["id"] for r in c.execute(
                "SELECT id FROM sightline_scans WHERE status = 'complete' "
                "ORDER BY id").fetchall()]
    else:
        scan_ids = [int(args.scan)]
    for sid in scan_ids:
        res = apply_mod.apply_to_scan(sid, snap, wv_id)
        overall = res.get("overall")
        cov = res.get("coverage") or {}
        overall_txt = f"{overall:>5.1f}" if overall is not None else "  n/a"
        print(f"  scan {sid:>4}: overall {overall_txt}  "
              f"(scored {cov.get('scored', 0)}/{cov.get('total', 0)})")


def cmd_backfill_seo(args) -> None:
    """Fill seo_score for completed scans that predate it.

    One pair of DataForSEO calls per DISTINCT domain, applied to every
    completed scan of that domain. Re-runnable: scans that already have a
    score are skipped, so an interrupted run resumes where it stopped.
    """
    from . import dataforseo as dfs
    from .scoring import seo as seo_mod

    rows = db.domains_missing_seo_score()
    if args.limit:
        rows = rows[:args.limit]
    if not rows:
        print("no completed scans are missing an SEO score")
        return

    n_scans = sum(len(r["scan_ids"]) for r in rows)
    print(f"{len(rows)} domain(s), {n_scans} scan(s) to backfill "
          f"(~${len(rows) * 0.036:.2f} of DataForSEO calls)")
    if args.dry_run:
        for r in rows:
            print(f"  {r['domain']:<40} scans {r['scan_ids']}")
        print("dry run; nothing written")
        return
    if not dfs.configured():
        print("DATAFORSEO_LOGIN/DATAFORSEO_PASSWORD not set; nothing to "
              "measure with", file=sys.stderr)
        sys.exit(1)

    for r in rows:
        metrics = dfs.collect_seo_metrics(r["domain"])
        result = seo_mod.score(metrics)
        # A backfilled score is measured TODAY and attached to a scan from
        # whenever. Stamp it so nobody later reads it as contemporaneous.
        result["backfilled"] = True
        result["measured_at"] = datetime.now(timezone.utc).isoformat()
        n = db.set_seo_score_for_scans(r["scan_ids"], result["score"], result)
        score = result["score"]
        score_txt = f"{score:>5.1f}" if score is not None else "  n/a"
        detail = ", ".join(
            f"{k}={metrics.get(k)}" for k in seo_mod.COMPONENT_ORDER
        )
        print(f"  {r['domain'][:38]:<38} {score_txt}  "
              f"({n} scan(s))  {detail}")


def cmd_render(args) -> None:
    from .render.html import render_scan
    wv = db.get_weight_version(args.version or weights_mod.WEIGHTS_VERSION)
    if not wv:
        print(f"weight version '{args.version}' not found. "
              "Run init-db or rescore first.", file=sys.stderr)
        sys.exit(1)
    html = render_scan(args.scan_id, wv["id"])
    if args.out:
        pathlib.Path(args.out).write_text(html, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        sys.stdout.write(html)


def cmd_compare(args) -> None:
    from .scoring.report import compute_report, composite_score
    before = db.get_weight_version(args.before)
    after = db.get_weight_version(args.after)
    if not before or not after:
        missing = [v for v, x in [(args.before, before), (args.after, after)]
                   if not x]
        print(f"weight version(s) not found: {', '.join(missing)}",
              file=sys.stderr)
        sys.exit(1)

    with db.conn() as c:
        scans = c.execute(
            """SELECT id, LOWER(domain) AS domain, url
                 FROM sightline_scans
                WHERE status = 'complete'
                ORDER BY LOWER(domain), id"""
        ).fetchall()
    if not scans:
        print("no completed scans to compare")
        return

    print("")
    print(f"  Overall: {args.before} (composite: 100 - Σ deductions) "
          f"vs {args.after} (mean of scored dimensions)")
    header = f"  {'DOMAIN':<38} {'SCAN':>5}  {args.before:>7}  {args.after:>7}  {'Δ':>6}  {'COVERAGE':>10}"
    print(header)
    print("  " + "-" * (len(header) - 2))
    for s in scans:
        b = composite_score(s["id"], before["id"])
        a_r = compute_report(s["id"], after["id"])
        a = a_r["overall"]
        cov = a_r["coverage"]
        b_txt = f"{b:>6.0f}"
        a_txt = f"{a:>6.0f}" if a is not None else "   n/a"
        delta = f"{(a - b):>+5.0f}" if a is not None else "     "
        cov_txt = f"{cov['scored']}/{cov['total']}"
        print(f"  {s['domain'][:38]:<38} {s['id']:>5}  {b_txt}  {a_txt}  {delta}  {cov_txt:>10}")

    print("")
    dims_order = after["weights"].get("dimension_order") or []
    dim_labels = after["weights"].get("dimension_labels") or {}
    short = {d: (dim_labels.get(d, d).split()[0][:5]) for d in dims_order}
    print(f"  Per-dimension breakdown under {args.after}:")
    print("  " + "-" * 78)
    header2 = "  " + f"{'DOMAIN':<38}" + " ".join(f"{short[d]:>6}"
                                                  for d in dims_order)
    print(header2)
    for s in scans:
        r = compute_report(s["id"], after["id"])
        cells = []
        for d in dims_order:
            v = r["dimensions"].get(d)
            cells.append("   n/a" if v is None else f"{v:>6.0f}")
        print("  " + f"{s['domain'][:38]:<38}" + " ".join(cells))
    print("")


def cmd_history(args) -> None:
    rows = db.scan_history(args.domain)
    if not rows:
        print(f"no scans for {args.domain}")
        return
    print("ID".rjust(6) + "  " + "REQUESTED".ljust(20)
          + "STATUS".ljust(12) + "URL")
    for r in rows:
        print(str(r["id"]).rjust(6) + "  "
              + r["requested_at"].strftime("%Y-%m-%d %H:%M").ljust(20)
              + r["status"].ljust(12) + r["url"])


def cmd_av_sample(args) -> None:
    url = fetch_http.normalize_url(args.url)
    dom = fetch_http.domain(url)
    excerpt = args.excerpt or ""
    if args.excerpt_file:
        excerpt = pathlib.Path(args.excerpt_file).read_text(encoding="utf-8")[:2000]
    obs_id = db.write_av_observation(
        domain=dom, scan_id=args.scan_id, model=args.model,
        query=args.query, brand_mentioned=(args.mentioned == "yes"),
        response_excerpt=excerpt, meta={},
    )
    summary = db.av_summary(dom)
    print(f"recorded av observation id={obs_id} for {dom}")
    print(f"  {summary['n_samples']} sample(s) in last {summary['window_days']}d, "
          f"visibility_rate={summary['visibility_rate']}")
    if (summary["n_samples"] or 0) < 10:
        print("  note: N<10 — insufficient for a trend claim.")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sightline")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init-db").set_defaults(func=cmd_init_db)

    s = sub.add_parser("scan")
    s.add_argument("url")
    s.set_defaults(func=cmd_scan)

    r = sub.add_parser("rescore")
    r.add_argument("scan", help="scan id or 'all'")
    r.add_argument("--version", required=True,
                   help="weight version name to register/use")
    r.set_defaults(func=cmd_rescore)

    bf = sub.add_parser("backfill-seo",
        help="measure + store seo_score for completed scans missing one")
    bf.add_argument("--dry-run", action="store_true",
                    help="list what would be measured, spend nothing")
    bf.add_argument("--limit", type=int, default=0,
                    help="cap the number of domains (0 = no cap)")
    bf.set_defaults(func=cmd_backfill_seo)

    ren = sub.add_parser("render")
    ren.add_argument("scan_id", type=int)
    ren.add_argument("--version", default=None,
                     help="weight version to render (default: current)")
    ren.add_argument("--out", default=None)
    ren.set_defaults(func=cmd_render)

    h = sub.add_parser("history")
    h.add_argument("domain")
    h.set_defaults(func=cmd_history)

    cmp_ = sub.add_parser("compare",
        help="print v_before vs v_after overall + per-dim table")
    cmp_.add_argument("before", help="earlier weight version (e.g. v1)")
    cmp_.add_argument("after",  help="later weight version (e.g. v2)")
    cmp_.set_defaults(func=cmd_compare)

    av = sub.add_parser("av-sample")
    av.add_argument("url")
    av.add_argument("--model", required=True)
    av.add_argument("--query", required=True)
    av.add_argument("--mentioned", choices=["yes", "no"], required=True)
    av.add_argument("--excerpt", default="")
    av.add_argument("--excerpt-file", default=None)
    av.add_argument("--scan-id", type=int, default=None)
    av.set_defaults(func=cmd_av_sample)

    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
