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
from .checks import (ai_crawler, structured_data, entity_consistency,
                     answer_first, llms_txt, pagespeed, rank)
from .checks.base import ScanContext
from .fetch import discovery
from .fetch import http as fetch_http
from .scoring import apply as apply_mod
from .scoring import weights as weights_mod

log = logging.getLogger(__name__)

CHECKS = [ai_crawler, structured_data, entity_consistency,
          answer_first, llms_txt, pagespeed, rank]

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


def _build_context(url: str) -> ScanContext:
    """Fetch everything a check might need. Raises RuntimeError with a
    plain-English reason if the target page itself can't be fetched, so an
    unreachable site never produces a score."""
    r = fetch_http.fetch_as_browser(url)
    if not r.ok:
        if r.status:
            raise RuntimeError(
                f"page fetch returned HTTP {r.status}; refusing to score "
                "an unreachable page"
            )
        raise RuntimeError(
            f"page fetch failed: {r.error or 'no response'}; refusing to "
            "score an unreachable page"
        )
    final = r.final_url or url
    ctx = ScanContext(
        url=final,
        origin=fetch_http.origin(final),
        domain=fetch_http.domain(final),
    )
    ctx.page_fetch = r
    ctx.page = discovery.parse_page(final, r.text)
    rt = fetch_http.try_relative(final, "/robots.txt")
    ctx.robots_status = rt.status
    ctx.robots_text = rt.text if rt.ok else None
    ctx.llms_txt_fetch = fetch_http.try_relative(final, "/llms.txt")
    return ctx


def run_scan_for(scan_id: int, url: str,
                 progress: ProgressCallback | None = None) -> dict:
    try:
        ctx = _build_context(url)
        for mod in CHECKS:
            try:
                findings = mod.run(ctx)
            except Exception:
                log.exception("check %s crashed", mod.CHECK_ID)
                if progress:
                    progress(mod.CHECK_ID, None)
                continue
            n = db.write_findings(scan_id, findings)
            if progress:
                progress(mod.CHECK_ID, n)
        wv_id = register_current_weights()
        result = apply_mod.apply_to_scan(scan_id, weights_mod.snapshot(), wv_id)
        db.complete_scan(scan_id, status="complete")
        return result
    except Exception as e:
        db.complete_scan(scan_id, status="failed", error=str(e))
        raise
