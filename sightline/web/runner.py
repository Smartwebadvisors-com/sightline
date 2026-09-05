"""Background scan runner. A bounded thread pool so two concurrent 'Run
scan' clicks don't hammer PSI quota in parallel while still keeping the
request handler non-blocking."""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

from ..pipeline import run_scan_for

log = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="sightline-scan")


def submit(scan_id: int, url: str) -> None:
    def _run() -> None:
        try:
            run_scan_for(scan_id, url)
        except Exception:
            # run_scan_for already marked the row 'failed' with the error;
            # log for the PM2 log stream so we can diagnose.
            log.exception("background scan %s failed", scan_id)
    _executor.submit(_run)
