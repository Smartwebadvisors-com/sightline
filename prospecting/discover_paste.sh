cat > /tmp/discover.sh <<'DISCOVER_EOF'
#!/usr/bin/env bash
# Read-only. Writes nothing, scans nothing, sends nothing.
set -uo pipefail
D="${SIGHTLINE_DIR:-/root/sightline}"
[ -f "$D/.env" ] && set -a && . "$D/.env" 2>/dev/null; set +a
hr() { printf '\n===== %s =====\n' "$1"; }

hr "0. context"
echo "dir=$D   DATABASE_URL set: $([ -n "${DATABASE_URL:-}" ] && echo yes || echo NO)"

hr "1. layout"
ls -la "$D" 2>&1 | head -30
find "$D" -maxdepth 2 -name '*.py' 2>/dev/null | head -25

hr "2. entry point"
for c in "python3 -m sightline --help" "python3 $D/main.py --help" \
         "python3 $D/cli.py --help" "python3 $D/sightline.py --help"; do
  echo "--- $c"
  (cd "$D" && timeout 20 $c 2>&1 | head -25); echo
done

hr "3. json flag?"
grep -rn --include='*.py' -E "add_argument\(.*(json|format|output|out)" "$D" 2>/dev/null | head -20

hr "4. tables"
if [ -n "${DATABASE_URL:-}" ]; then
  psql "$DATABASE_URL" -c "\dt sightline_*" 2>&1 | head -25
  for t in $(psql "$DATABASE_URL" -At -c "SELECT tablename FROM pg_tables WHERE tablename LIKE 'sightline_%' ORDER BY 1" 2>/dev/null | head -6); do
    echo "--- $t"; psql "$DATABASE_URL" -c "\d $t" 2>&1 | head -35; echo
  done
else
  echo "DATABASE_URL not set -- rerun as: DATABASE_URL=postgres://... bash /tmp/discover.sh"
fi

hr "5. one real scan row (truncated)"
[ -n "${DATABASE_URL:-}" ] && psql "$DATABASE_URL" -At -c \
  "SELECT left(row_to_json(t)::text,4000) FROM (SELECT * FROM sightline_scans ORDER BY 1 DESC LIMIT 1) t;" 2>&1 | head -20

hr "6. how findings are stored"
grep -rn --include='*.py' -E "(finding|issue|recommendation|check)s?\s*[:=]" "$D" 2>/dev/null | head -25

hr "7. does Sightline check AI crawlers?"
grep -rniE "gptbot|perplexitybot|claudebot|google-extended|ai.crawler" "$D" 2>/dev/null | head -10
echo "(nothing above = not checked yet)"

hr "8. report urls"
grep -rn --include='*.py' -E "(report_url|permalink|public_url|/r/|share)" "$D" 2>/dev/null | head -15

hr "done"
DISCOVER_EOF
bash /tmp/discover.sh > /tmp/sightline-surface.txt 2>&1
wc -l /tmp/sightline-surface.txt
