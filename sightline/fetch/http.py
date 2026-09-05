"""HTTP fetching. One retry, honest User-Agent, size cap.
Also supports UA-spoofing for the ai_crawler edge-blocking check."""
from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse, urlunparse

import requests

from ..config import settings


MAX_BYTES = 3_000_000  # 3 MB is enough HTML for anything worth diagnosing
BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)


@dataclass
class FetchResult:
    url: str
    final_url: str
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    text: str = ""
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and 200 <= self.status < 300


def normalize_url(url: str) -> str:
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    # DNS is case-insensitive; fold the host so 'Example.com' and 'example.com'
    # produce one canonical URL and one grouping key downstream.
    p = urlparse(url)
    if p.netloc and p.netloc != p.netloc.lower():
        url = urlunparse(p._replace(netloc=p.netloc.lower()))
    return url


def origin(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc.lower()}"


def domain(url: str) -> str:
    return urlparse(url).netloc.lower().replace("www.", "")


def _get(url: str, user_agent: str) -> FetchResult:
    headers = {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        r = requests.get(url, headers=headers, timeout=settings.http_timeout,
                         allow_redirects=True, stream=True)
        # Read up to MAX_BYTES to avoid pulling in giant pages.
        chunks: list[bytes] = []
        total = 0
        for chunk in r.iter_content(chunk_size=65536):
            chunks.append(chunk)
            total += len(chunk)
            if total >= MAX_BYTES:
                break
        raw = b"".join(chunks)
        text = raw.decode(r.encoding or "utf-8", errors="replace")
        return FetchResult(
            url=url, final_url=r.url, status=r.status_code,
            headers={k.lower(): v for k, v in r.headers.items()},
            text=text,
        )
    except requests.RequestException as e:
        return FetchResult(url=url, final_url=url, status=0, error=str(e))


def fetch(url: str) -> FetchResult:
    """Fetch as the Sightline diagnostic UA."""
    return _get(url, settings.user_agent)


def fetch_as_browser(url: str) -> FetchResult:
    """Fetch spoofing a real browser. Used to compare against bot fetches."""
    return _get(url, BROWSER_UA)


def fetch_as_bot(url: str, bot_ua: str) -> FetchResult:
    """Fetch spoofing a named assistant crawler UA. Used to detect edge
    blocking that the robots.txt check alone would miss."""
    return _get(url, bot_ua)


def try_relative(base_url: str, path: str) -> FetchResult:
    return fetch(urljoin(origin(base_url) + "/", path.lstrip("/")))
