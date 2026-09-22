"""Test package init. Loads .env before any test module imports
sightline.config.

sightline.config reads the environment once, at import, and nothing in the
library loads .env for it — the web entrypoint does it by hand. Under
`unittest discover` the first test module to import sightline.config wins,
and if that happens before .env is read, settings.db_url silently falls
back to the password-less default. The database then looks unreachable and
the read-only render test over real scans skips itself — which is the one
failure mode that test exists to prevent.
"""
from __future__ import annotations

import os
import pathlib


# The DSN the destructive prospecting tests read. They run DROP TABLE at
# MODULE scope (prospecting/test_outbox_pg.py:85, test_queue_pg.py:80), so
# merely importing one is destructive, and their _refuse_if_real_data()
# guard only counts the four prospect tables. They are outside this
# package and cannot be reached by `unittest discover -s tests`, and .env
# does not currently define this key — but this file exists to put a live
# DSN into the environment, so if that key is ever added to .env, loading
# it here would arm them. Refuse instead.
DESTRUCTIVE_TEST_DSN_VAR = "DATABASE_URL"


def load_dotenv() -> None:
    env_path = pathlib.Path(__file__).resolve().parents[1] / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
            v = v[1:-1]
        if k == DESTRUCTIVE_TEST_DSN_VAR:
            raise RuntimeError(
                f"{env_path} defines {DESTRUCTIVE_TEST_DSN_VAR}. The test "
                "suite will not load it: prospecting/test_outbox_pg.py and "
                "test_queue_pg.py read that variable and drop tables at "
                "import time. Point them at a scratch database explicitly "
                "instead."
            )
        os.environ.setdefault(k, v)


load_dotenv()
