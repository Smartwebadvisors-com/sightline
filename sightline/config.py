"""Configuration. Everything reads from env — no secrets in code.
Missing credentials disable the corresponding check rather than crash."""
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    db_url: str = os.getenv(
        "SIGHTLINE_DB_URL",
        "postgresql://trendsignal@localhost/trendsignal",
    )
    pagespeed_api_key: str | None = os.getenv("PAGESPEED_API_KEY") or None
    dataforseo_login: str | None = os.getenv("DATAFORSEO_LOGIN") or None
    dataforseo_password: str | None = os.getenv("DATAFORSEO_PASSWORD") or None
    user_agent: str = os.getenv(
        "SIGHTLINE_USER_AGENT",
        "Sightline-Diagnostic/0.1",
    )
    http_timeout: int = int(os.getenv("SIGHTLINE_HTTP_TIMEOUT", "20"))


settings = Settings()
