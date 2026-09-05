#!/usr/bin/env bash
# discover.sh -- run this on the VPS and send me the output.
#
# The adapter is written to tolerate several plausible Sightline output shapes,
# but tolerant is not the same as correct. This dumps the three things that
# pin the mapping to reality:
#
#   1. how Sightline is invoked and whether it can emit JSON
#   2. what its tables actually look like
#   3. one real scan payload
#
# Reads only. Writes nothing, scans nothing, sends nothing.
#
#   bash discover.sh > sightline-surface.txt 2>&1

set -uo pipefail
SIGHTLINE_DIR="${SIGHTLINE_DIR:-/root/sightline}"

hr() { printf '\n===== %s =====\n' "$1"; }

hr "1. layout"
ls -la "$SIGHTLINE_DIR" 2>&1 | head -40
echo
find "$SIGHTLINE_DIR" -maxdepth 2 -name '*.py' 2>/dev/null | head -30

hr "2. entry point"
# Whichever of these exists tells me how to call it.
for candidate in \
    "python3 -m sightline --help" \
    "python3 $SIGHTLINE_DIR/main.py --help" \
    "python3 $SIGHTLINE_DIR/cli.py --help" \
    "python3 $SIGHTLINE_DIR/sightline.py --help"
do
    echo "--- $candidate"
    (cd "$SIGHTLINE_DIR" && timeout 20 $candidate 2>&1 | head -30)
    echo
done

hr "3. does it have a JSON flag?"
grep -rn --include='*.py' -E "add_argument\(.*(json|format|output|out)" \
    "$SIGHTLINE_DIR" 2>/dev/null | head -20

hr "4. tables"
if [ -n "${DATABASE_URL:-}" ]; then
    psql "$DATABASE_URL" -c "\dt sightline_*" 2>&1 | head -30
    echo
    for t in $(psql "$DATABASE_URL" -At -c \
        "SELECT tablename FROM pg_tables WHERE tablename LIKE 'sightline_%' ORDER BY 1" \
        2>/dev/null | head -6); do
        echo "--- $t"
        psql "$DATABASE_URL" -c "\d $t" 2>&1 | head -40
        echo
    done
else
    echo "DATABASE_URL not set in this shell -- run:"
    echo "  DATABASE_URL=postgres://... bash discover.sh"
fi

hr "5. one real scan row (structure only, values truncated)"
if [ -n "${DATABASE_URL:-}" ]; then
    psql "$DATABASE_URL" -At -c "
        SELECT left(row_to_json(t)::text, 4000)
        FROM (SELECT * FROM sightline_scans ORDER BY 1 DESC LIMIT 1) t;
    " 2>&1 | head -20
fi

hr "6. how findings are stored"
grep -rn --include='*.py' -E "(finding|issue|recommendation|check)s?\s*[:=]" \
    "$SIGHTLINE_DIR" 2>/dev/null | head -25

hr "7. does Sightline already check AI crawlers?"
grep -rniE "gptbot|perplexitybot|claudebot|google-extended|ai.crawler" \
    "$SIGHTLINE_DIR" 2>/dev/null | head -10
echo "(empty above = Sightline does not check this yet; the adapter's"
echo " supplement=True will add it, or port check_ai_crawlers() into Sightline)"

hr "8. report URLs"
grep -rn --include='*.py' -E "(report_url|permalink|public_url|/r/|share)" \
    "$SIGHTLINE_DIR" 2>/dev/null | head -15

hr "done"
echo "Send this file back and I'll pin FIELD_ALIASES/FINDING_ALIASES to it."
