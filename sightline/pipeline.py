"""Full scan pipeline. One entrypoint used by both the CLI and the web
dashboard so the scan code path lives in exactly one place.

Contract:
  create_pending_scan(url) -> scan_id
      Validates the URL, inserts a sightline_scans row with status='running',
      returns the id. Raises ValueError if the URL doesn't parse.

  run_scan_for(scan_id, url, progress=None) -> result dict
      Fetches, runs every check, persists findings, applies current weights,
      marks the row complete. Raises RuntimeError with a clear message if
      the target page cannot be fetched — the caller decides what to do
      with it (CLI prints and exits; web marks the row failed).

The scan row is created BEFORE fetching so the dashboard can show a pending
entry even while the fetch is in flight."""
from __future__ import annotations

import logging
from typing import Callable
from urllib.parse import urlparse

from . import db
from . import findings as findings_mod
from . import unavailable
from .checks import (ai_crawler, structured_data, entity_consistency,
                     answer_first, llms_txt, pagespeed, rank, prompt_testing)
from . import aggregate as aggregate_mod
from .checks.base import ScanContext
from .fetch import discovery
from .fetch import http as fetch_http
from .fetch import sample as sample_mod
from .scan_slot import slot_decision
from .scoring import apply as apply_mod
from .scoring import seo as seo_mod
from .scoring import weights as weights_mod

log = logging.getLogger(__name__)

CHECKS = [ai_crawler, structured_data, entity_consistency,
          answer_first, llms_txt, pagespeed, rank, prompt_testing]

# Read on every sampled page. The others are domain readings and run once.
_PAGE_CHECKS = {structured_data.CHECK_ID, entity_consistency.CHECK_ID, answer_first.CHECK_ID}
# Billed per URL. Homepage and the main service page only.
_SPEED_CHECK = pagespeed.CHECK_ID

ProgressCallback = Callable[[str, int | None], None]


def _validate(url: str) -> str:
    """Normalize + validate. Raises ValueError with a message the user
    can read."""
    if not (url or "").strip():
        raise ValueError("URL is required")
    canonical = fetch_http.normalize_url(url)
    p = urlparse(canonical)
    if p.scheme not in ("http", "https"):
        raise ValueError(f"'{url}' is not an http(s) URL")
    host = p.hostname or ""
    if (not host or "." not in host or " " in host or ".." in host
            or host.startswith("-") or host.endswith("-")):
        raise ValueError(f"'{url}' has no valid host")
    return canonical


def register_current_weights() -> int:
    return db.upsert_weight_version(
        weights_mod.WEIGHTS_VERSION,
        weights_mod.WEIGHTS_DESCRIPTION,
        weights_mod.snapshot(),
    )


def create_pending_scan(url: str) -> int:
    canonical = _validate(url)
    dom = fetch_http.domain(canonical)
    return db.create_scan(
        canonical, dom,
        meta={"weights_version": weights_mod.WEIGHTS_VERSION},
    )


def begin_scan(url: str) -> tuple[int, bool]:
    """(scan_id, started). A second click joins the scan already in flight,
    or the one that finished in the last few minutes."""
    canonical = _validate(url)
    row = db.latest_scan_for_url(canonical)
    decision = slot_decision(row)
    if decision == "reuse" and row:
        return int(row["id"]), False
    if decision == "abandon" and row:
        db.complete_scan(
            int(row["id"]), status="failed",
            error=unavailable.DID_NOT_FINISH.format(scan_id=row["id"]),
        )
    return create_pending_scan(canonical), True


def _build_context(url: str) -> ScanContext:
    """Fetch everything a check might need. Raises ScanAborted with a
    client-safe sentence if the target page itself can't be fetched, so an
    unreachable site never produces a score.

    The transport error is ours to diagnose and nobody else's to read: it
    goes to the log whole, and what travels out of here is one sentence
    naming the host. `r.error` is a urllib3 repr with a memory address in
    it, and it used to be stored on the scan row and rendered on the
    failure page — and scraped from there into Cited's own error banner.
    """
    r = fetch_http.fetch_as_browser(url)
    if not r.ok:
        host = urlparse(r.final_url or url).hostname or ""
        log.warning("page fetch failed for %s (status=%s): %s",
                    url, r.status, r.error)
        if r.status:
            raise unavailable.PageNotScorable(host, r.status)
        raise unavailable.SiteUnreachable(host)
    final = r.final_url or url
    ctx = ScanContext(
        url=final,
        origin=fetch_http.origin(final),
        domain=fetch_http.domain(final),
    )
    ctx.page_fetch = r
    ctx.page = discovery.parse_page(final, r.text)
    # Resolved once, from the page we just parsed, and reused for every
    # finding's plain copy so one report cannot call the client two names.
    ctx.profile = findings_mod.profile_from_page(ctx.page, ctx.domain)
    rt = fetch_http.try_relative(final, "/robots.txt")
    ctx.robots_status = rt.status
    ctx.robots_text = rt.text if rt.ok else None
    ctx.llms_txt_fetch = fetch_http.try_relative(final, "/llms.txt")
    return ctx


def _score_seo(scan_id: int, ctx: ScanContext) -> dict | None:
    """Score and persist the DataForSEO-sourced SEO number.

    Independent of the AEO composite: it reads the raw metrics rank.py
    stashed on the context, never a finding or a deduction. A failure
    here does not fail the scan — the AEO result is complete and valid
    on its own, the same way one crashing check doesn't void the rest.
    """
    try:
        result = seo_mod.score(ctx.seo_metrics or {})
        db.set_seo_score(scan_id, result["score"], result)
        return result
    except Exception:
        log.exception("seo scoring failed for scan %s", scan_id)
        return None


def _persist(scan_id: int, ctx: ScanContext, observations, progress, check_id: str) -> None:
    try:
        # build_all is the only path from observation to stored finding,
        # so both copy blocks exist before anything is persisted.
        n = db.write_findings(
            scan_id, findings_mod.build_all(observations, ctx.profile))
    except Exception:
        log.exception("check %s crashed", check_id)
        if progress:
            progress(check_id, None)
        return
    if progress:
        progress(check_id, n)


def _across_pages(scan_id: int, ctx: ScanContext, mod, pages: list,
                  progress: ProgressCallback | None) -> None:
    grouped: dict[tuple[str, str], list] = {}
    readable = [page for page in pages if page.page is not None]
    for page in readable:
        page_ctx = sample_mod.context_for(ctx, page)
        try:
            observations = mod.run(page_ctx)
        except Exception:
            log.exception("check %s crashed on %s", mod.CHECK_ID, page.url)
            continue
        for obs in observations:
            grouped.setdefault((obs.check_id, obs.item_key), []).append((page.url, obs))
    if not grouped:
        if progress:
            progress(mod.CHECK_ID, None)
        return
    _persist(
        scan_id, ctx,
        aggregate_mod.aggregate(grouped, pages_checked=len(readable)),
        progress, mod.CHECK_ID,
    )


def run_scan_for(scan_id: int, url: str,
                 progress: ProgressCallback | None = None) -> dict:
    try:
        ctx = _build_context(url)
        db.set_scan_profile(scan_id, ctx.profile)
        pages = sample_mod.collect(ctx)
        speed_pages = [page for page in pages if page.role in ("home", "service")]
        for mod in CHECKS:
            if mod.CHECK_ID in _PAGE_CHECKS:
                _across_pages(scan_id, ctx, mod, pages, progress)
            elif mod.CHECK_ID == _SPEED_CHECK:
                _across_pages(scan_id, ctx, mod, speed_pages, progress)
            else:
                try:
                    observations = mod.run(ctx)
                except Exception:
                    log.exception("check %s crashed", mod.CHECK_ID)
                    if progress:
                        progress(mod.CHECK_ID, None)
                    continue
                _persist(scan_id, ctx, observations, progress, mod.CHECK_ID)
        wv_id = register_current_weights()
        result = apply_mod.apply_to_scan(scan_id, weights_mod.snapshot(), wv_id)
        result["seo"] = _score_seo(scan_id, ctx)
        db.complete_scan(scan_id, status="complete")
        return result
    except unavailable.ScanAborted as e:
        # We chose to stop, and the message was written to be read.
        db.complete_scan(scan_id, status="failed", error=str(e))
        raise
    except Exception:
        # Anything else is a bug or an outage, and `str(e)` on one of those
        # is a stack-shaped string that ends up rendered on the failure
        # page. Log it whole; store a line a client can read.
        log.exception("scan %s failed", scan_id)
        db.complete_scan(
            scan_id, status="failed",
            error=unavailable.DID_NOT_FINISH.format(scan_id=scan_id))
        raise
