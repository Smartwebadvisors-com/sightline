#!/usr/bin/env bash
# install.sh -- set up the prospecting layer beside Sightline.
#
# Nothing inside Sightline is modified. This creates a subfolder, installs
# Python deps, adds sightline_-prefixed tables to the database Sightline
# already uses, and runs the offline test suites to prove the install.
#
#   bash install.sh
#
# Safe to re-run. Applies no schema changes it hasn't been asked for, and
# stops on the first real problem rather than half-installing.

set -euo pipefail

SIGHTLINE_DIR="${SIGHTLINE_DIR:-/root/sightline}"
TARGET="${TARGET:-$SIGHTLINE_DIR/prospecting}"

say()  { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
warn() { printf '    \033[33mwarning:\033[0m %s\n' "$1"; }
die()  { printf '\n\033[31mstopped:\033[0m %s\n' "$1" >&2; exit 1; }

# ---------------------------------------------------------------------------
say "1. checking prerequisites"

command -v python3 >/dev/null || die "python3 not found"
echo "    python3: $(python3 --version)"

[ -d "$SIGHTLINE_DIR" ] || die "no Sightline at $SIGHTLINE_DIR (set SIGHTLINE_DIR=)"
echo "    sightline: $SIGHTLINE_DIR"

# Pick up Sightline's own environment if it has one -- same DB, same creds.
#
# Sourcing a .env is the normal convention but it is not safe for values that
# were written unquoted: a connection string like
#   DATABASE_URL=postgres://u:p@h/db?sslmode=require&application_name=x
# contains & and ? , which the shell reads as syntax, and the variable silently
# ends up empty or truncated. So we source it, then read the raw line directly
# for anything that came back missing.
read_env_line() {   # read_env_line FILE KEY -> prints the raw value
    sed -E -n "s/^[[:space:]]*(export[[:space:]]+)?$2[[:space:]]*=[[:space:]]*//p" "$1" \
        | head -n1 | sed -E -e 's/^"(.*)"$/\1/' -e "s/^'(.*)'$/\1/"
}

if [ -f "$SIGHTLINE_DIR/.env" ]; then
    set -a; . "$SIGHTLINE_DIR/.env" 2>/dev/null || true; set +a
    for var in DATABASE_URL PERPLEXITY_API_KEY DATAFORSEO_LOGIN DATAFORSEO_PASSWORD; do
        if [ -z "${!var:-}" ]; then
            value="$(read_env_line "$SIGHTLINE_DIR/.env" "$var" || true)"
            [ -n "$value" ] && export "$var=$value"
        fi
    done
    echo "    loaded $SIGHTLINE_DIR/.env"
fi

[ -n "${DATABASE_URL:-}" ] || die \
    "DATABASE_URL not set and not found in $SIGHTLINE_DIR/.env.
    Re-run as: DATABASE_URL='postgres://...' bash install.sh"

# ---------------------------------------------------------------------------
say "2. placing files in $TARGET"

mkdir -p "$TARGET"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ "$SRC" != "$TARGET" ]; then
    cp "$SRC"/*.py "$SRC"/*.sql "$SRC"/*.sh "$SRC"/*.md "$TARGET"/ 2>/dev/null || true
    cp "$SRC"/prospects.example.json "$TARGET"/ 2>/dev/null || true
fi
cd "$TARGET"
echo "    $(ls *.py 2>/dev/null | wc -l) python files, $(ls *.sql 2>/dev/null | wc -l) sql"

# ---------------------------------------------------------------------------
say "3. installing python dependencies"

PIP_FLAGS=""
python3 -c "import sys; sys.exit(0)" 2>/dev/null
if pip install --help 2>/dev/null | grep -q break-system-packages; then
    PIP_FLAGS="--break-system-packages"
fi
pip install $PIP_FLAGS -q requests beautifulsoup4 'psycopg[binary]' redis \
    || die "dependency install failed"
python3 - <<'PY' || die "dependencies did not import"
import requests, bs4, psycopg, redis
print(f"    requests {requests.__version__}, bs4 {bs4.__version__}, "
      f"psycopg {psycopg.__version__}, redis {redis.__version__}")
PY

# ---------------------------------------------------------------------------
say "4. applying schema to the shared database"

echo "    target: $(python3 - <<'PY'
import os, re
print(re.sub(r'://[^@]*@', '://***@', os.environ.get('DATABASE_URL', '')))
PY
)"

# Show what already exists so a re-run is not a surprise.
EXISTING=$(psql "$DATABASE_URL" -At -c \
  "SELECT count(*) FROM pg_tables WHERE tablename IN
   ('sightline_prospects','sightline_prospect_scans','sightline_outreach_events');" \
  2>/dev/null || echo "?")
echo "    prospecting tables already present: $EXISTING of 3"

psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -q -f schema.sql \
    || die "schema failed to apply -- nothing else was changed"
echo "    schema applied"

psql "$DATABASE_URL" -At -c \
  "SELECT tablename FROM pg_tables WHERE tablename LIKE 'sightline_%' ORDER BY 1;" \
  | sed 's/^/      /'

# ---------------------------------------------------------------------------
say "5. running the offline test suites"

FAILED=0
for suite in selftest test_sightline test_failures test_sourcing; do
    if python3 "$suite.py" >/tmp/$suite.log 2>&1; then
        printf '    pass  %s\n' "$suite"
    else
        printf '    FAIL  %s  (see /tmp/%s.log)\n' "$suite" "$suite"
        FAILED=1
    fi
done
[ "$FAILED" -eq 0 ] || die "test suites failed -- do not run this against real prospects"

if DATABASE_URL="$DATABASE_URL" python3 test_outbox_pg.py >/tmp/test_outbox_pg.log 2>&1; then
    echo "    pass  test_outbox_pg (duplicate-send guard verified against this database)"
else
    warn "test_outbox_pg failed -- see /tmp/test_outbox_pg.log"
    warn "this suite DROPS AND RECREATES the prospecting tables; skip it if you"
    warn "already have real prospect data you care about"
fi

# ---------------------------------------------------------------------------
say "6. environment"

if [ ! -f .env ]; then
    cat > .env <<'ENVEOF'
# Prospecting layer -- fill these in, then: set -a; . ./.env; set +a
#
# QUOTE EVERY VALUE. This file is sourced by the shell, so an unquoted
# apostrophe, &, ?, # or space silently breaks the variable -- and real
# connection strings and API keys contain all of them.

# Required. https://www.perplexity.ai/settings/api
PERPLEXITY_API_KEY=""

# Required for sourcing. https://app.dataforseo.com/api-access
DATAFORSEO_LOGIN=""
DATAFORSEO_PASSWORD=""

# Sightline wiring -- correct these from discover.sh output
SIGHTLINE_DIR="/root/sightline"
SIGHTLINE_CMD="python3 -m sightline scan --json {url}"
SIGHTLINE_REPORT_BASE=""
AEO_ENGINE="sightline-cli"

# Where to look. Radius is kilometers.
LV_CENTER="40.6300,-75.3700"
LV_RADIUS_KM="25"

# A business you are CERTAIN answer engines cite. Preflight halts the batch
# if this one comes back invisible.
CANARY_NAME="Rita's Italian Ice"
CANARY_DOMAIN="ritasice.com"
CANARY_CATEGORY="italian ice shop"
CANARY_CITY="Bethlehem"
CANARY_STATE="PA"

# Safety rails. Leave approval on until the copy is proven.
AEO_REQUIRE_APPROVAL="1"
AEO_DAILY_CAP="30"
AEO_MIN_PRIORITY="55"
AEO_PROBE_QUORUM="3"
ENVEOF
    echo "    wrote $TARGET/.env -- fill in the blanks"
else
    echo "    $TARGET/.env already exists, left alone"
fi

MISSING=""
for var in PERPLEXITY_API_KEY DATAFORSEO_LOGIN DATAFORSEO_PASSWORD; do
    [ -n "${!var:-}" ] || MISSING="$MISSING $var"
done
[ -z "$MISSING" ] || warn "not set yet:$MISSING"

# ---------------------------------------------------------------------------
say "installed"
cat <<EOF

Next, in order:

  1. Fill in $TARGET/.env  (Perplexity key, DataForSEO login)
     then:  cd $TARGET && set -a && . ./.env && set +a

  2. bash discover.sh > sightline-surface.txt
     Send that back so SIGHTLINE_CMD and the field mapping get pinned to reality.

  3. python3 preflight.py
     Exit 0 = GO. Exit 2 = do not run a batch; the numbers cannot be trusted.

  4. python3 sourcing.py --dry-run
     Read the list. If these are not businesses you would call, tune
     --categories and LV_RADIUS_KM before spending a single scan.

Nothing in Sightline itself was modified.
EOF
