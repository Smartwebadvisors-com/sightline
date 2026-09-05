"""Entrypoint: `python -m sightline.web`. Loads /root/sightline/.env into
os.environ before importing sightline.config (which reads env at import),
then serves the Flask app via waitress on 127.0.0.1:5055.

Bind 127.0.0.1 only. Cloudflare Access provides auth in front."""
from __future__ import annotations

import os
import pathlib


def _load_dotenv() -> None:
    env_path = pathlib.Path(__file__).resolve().parents[2] / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
            v = v[1:-1]
        os.environ.setdefault(k.strip(), v)


_load_dotenv()

from waitress import serve  # noqa: E402  (must import after _load_dotenv)

from .app import create_app  # noqa: E402


def main() -> None:
    port = int(os.environ.get("SIGHTLINE_WEB_PORT", "5055"))
    app = create_app()
    print(f"sightline-web listening on 127.0.0.1:{port}", flush=True)
    serve(app, host="127.0.0.1", port=port, threads=8, ident="sightline-web")


if __name__ == "__main__":
    main()
