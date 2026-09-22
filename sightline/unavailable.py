"""A failed measurement is never a finding — COPY.md rule 10.

Two failures wear the same costume, and neither may reach a client:

  1. The page itself cannot be fetched (DNS failure, refused connection, an
     error status). Sightline already refuses to score it. What this module
     adds is the sentence a view is allowed to print instead of the
     exception: `SiteUnreachable`/`PageNotScorable` carry client-safe text,
     and `failure_message()` is the only thing a template renders — it
     scrubs anything it does not recognise, including the rows written
     before this module existed, which hold raw urllib3 tracebacks.

  2. A third-party measurement fails (PageSpeed 429/500, DataForSEO,
     timeouts). `send_with_retry()` retries what is worth retrying; what
     survives becomes an UNMEASURED finding via `unmeasured()` — no
     remediation, no impact, no effort, no weight in any denominator.

`reason()` is the only text allowed to describe a failed measurement, and
it is built from a service name and a status code. Exceptions are
classified, never quoted: `str()` on a requests failure is a urllib3 repr
with a memory address in it, and Google answers a bad key with a full HTML
error page. Neither has any business in a report. Nothing in this module
interpolates an exception into a string a view can render — that is the
whole point of it.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Callable

import requests

from .checks.base import Finding

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# case 1: the page itself
# --------------------------------------------------------------------------

UNREACHABLE = ("We couldn't reach a website at {host}. Check the spelling, "
               "or confirm the domain is live.")
NOT_SCORABLE = ("We reached {host}, but it answered with an error "
                "(HTTP {status}) instead of a page, so there was nothing to "
                "score. The site may be down.")
DID_NOT_FINISH = ("This scan didn't finish. The reason is recorded in our "
                  "server log — scan #{scan_id}.")


class ScanAborted(RuntimeError):
    """A scan we deliberately stopped. `str()` is always client-safe."""


class SiteUnreachable(ScanAborted):
    def __init__(self, host: str) -> None:
        self.host = host
        super().__init__(UNREACHABLE.format(host=host or "that address"))


class PageNotScorable(ScanAborted):
    def __init__(self, host: str, status: int) -> None:
        self.host, self.status = host, status
        super().__init__(NOT_SCORABLE.format(host=host or "that address",
                                             status=status))


# Every sentence a view may print verbatim. A stored error that does not
# start with one of these is an exception until proven otherwise.
SAFE_PREFIXES = (
    UNREACHABLE.split("{")[0],
    NOT_SCORABLE.split("{")[0],
    DID_NOT_FINISH.split("{")[0],
)


def failure_message(scan: dict) -> str:
    """The only text a failed-scan view may render.

    Whitelist, not blacklist: the raw exception strings we are scrubbing
    have no fixed shape, so recognising ours is the only reliable test.
    Rows that predate this module fall through to the generic line rather
    than showing a client a NameResolutionError.
    """
    err = (scan.get("error") or "").strip()
    if err.startswith(SAFE_PREFIXES):
        return err
    return DID_NOT_FINISH.format(scan_id=scan.get("id"))


# --------------------------------------------------------------------------
# case 2: a third-party measurement
# --------------------------------------------------------------------------

# Worth retrying: rate limiting and the transient 5xx family. A 4xx is a
# statement about our request (bad key, bad URL) and retrying it just
# spends quota to be told the same thing three times.
RETRY_STATUSES = (429, 500, 502, 503, 504)
ATTEMPTS = 3
BASE_DELAY = 1.0
MAX_DELAY = 8.0

_TIMEOUT = "did not respond in time"
_CONNECTION = "could not be reached"
_TRANSPORT = "did not return a usable response"


def _transport_phrase(exc: BaseException) -> str:
    if isinstance(exc, requests.Timeout):
        return _TIMEOUT
    if isinstance(exc, requests.ConnectionError):
        return _CONNECTION
    return _TRANSPORT


def reason(service: str, *, status: int | None = None,
           exc: BaseException | None = None, attempts: int = 1,
           detail: str = "") -> str:
    """One sentence describing why a measurement did not happen.

    Built from the service name and a status code. Callers pass the
    exception so it can be *classified*; its text is never read.
    """
    tries = f" after {attempts} attempts" if attempts > 1 else ""
    if status is not None:
        what = f"returned HTTP {status}{tries}"
    elif exc is not None:
        what = f"{_transport_phrase(exc)}{tries}"
    else:
        what = detail or "did not return a measurement"
    return f"{service} {what}; nothing was measured this scan."


def not_configured(service: str, env_var: str) -> str:
    """Our own gap, stated as ours. Naming the variable is fine here: this
    text lands in the technical block, which only we and a developer read."""
    return (f"{service} is not configured ({env_var} is unset); nothing was "
            "measured this scan.")


def _retry_after(response: Any) -> float | None:
    """Honour Retry-After when the service tells us how long to wait, but
    never sleep longer than MAX_DELAY inside a scan."""
    raw = (getattr(response, "headers", None) or {}).get("Retry-After")
    try:
        return max(0.0, min(float(raw), MAX_DELAY))
    except (TypeError, ValueError):
        return None


def send_with_retry(send: Callable[[], Any], *, service: str,
                    attempts: int = ATTEMPTS, base_delay: float = BASE_DELAY,
                    sleep: Callable[[float], None] | None = None):
    """Call `send()` with backoff on 429/5xx and transport failures.

    Returns `(response, "")` for any response worth reading — including a
    4xx, which retrying cannot fix — or `(None, reason)` when every attempt
    failed. The reason is already client-safe.

    `sleep` is injected so tests exercise the backoff without taking three
    seconds to do it. It defaults to None rather than to `time.sleep` so
    the lookup happens per call: a default argument binds at import, which
    silently defeats patching and makes the suite really sleep.
    """
    sleep = sleep or time.sleep
    last = ""
    for attempt in range(1, attempts + 1):
        wait = min(base_delay * 2 ** (attempt - 1), MAX_DELAY)
        try:
            response = send()
        except requests.RequestException as exc:
            # Whole exception to the log, classification to the report.
            log.warning("%s: attempt %s/%s failed", service, attempt,
                        attempts, exc_info=True)
            last = reason(service, exc=exc, attempts=attempt)
        else:
            if response.status_code not in RETRY_STATUSES:
                return response, ""
            log.warning("%s: attempt %s/%s returned HTTP %s", service,
                        attempt, attempts, response.status_code)
            last = reason(service, status=response.status_code,
                          attempts=attempt)
            wait = _retry_after(response) or wait
        if attempt < attempts:
            sleep(wait)
    return None, last


_HTTP_STATUS = re.compile(r"\bHTTP (\d{3})\b")


def rebuild_detail(examined: str, observed: str) -> str:
    """Re-derive the one safe sentence for a stored unmeasured finding.

    Rows written before this module hold whatever the service said —
    `HTTP 500: {"error": {"message": "Lighthouse returned error: ...`, a
    urllib3 repr, a Google error page. That text is the finding's
    technical detail, so the renderer prints it. Copy is derived, not
    stored (see findings.py), so the fix is to rebuild this sentence on
    read rather than migrate 86 rows: keep the status code, which is the
    only part that was ever information, and drop the rest.
    """
    match = _HTTP_STATUS.search(observed or "")
    if match:
        return reason(examined, status=int(match.group(1)))
    return reason(examined, detail="did not return a measurement")


def unmeasured(check_id: str, item_key: str, examined: str,
               why: str, evidence: dict | None = None) -> Finding:
    """The only way to record a measurement that did not happen.

    No remediation, by construction: there is nothing for anyone to fix.
    `checks.base.Finding` refuses an unavailable finding that carries one,
    so this is the path of least resistance as well as the correct one.
    """
    return Finding(
        check_id=check_id, item_key=item_key, severity="unavailable",
        examined=examined, observed=why, evidence=evidence or {},
    )
