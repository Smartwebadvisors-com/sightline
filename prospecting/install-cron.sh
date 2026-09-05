#!/usr/bin/env bash
# install-cron.sh -- schedule the unattended runs.
#
# Three entries. Nothing here sends anything: the act stage still lands in a
# review queue for a human.
#
#   bash install-cron.sh          # show what would be installed
#   bash install-cron.sh --apply  # install it

set -euo pipefail
DIR="${DIR:-/root/sightline/prospecting}"
MARK="# aeo-prospecting"

read -r -d '' ENTRIES <<CRON || true
$MARK begin
# Refill the prospect queue weekly. New businesses do not appear daily.
0 6 * * 1 cd $DIR && set -a && . ./.env && set +a && python3 sourcing.py --enqueue >> /var/log/aeo-sourcing.log 2>&1
# Scan the queue on weekday mornings. The run lock makes overlap harmless.
0 7 * * 1-5 cd $DIR && set -a && . ./.env && set +a && python3 autoscan.py --limit 25 >> /var/log/aeo-autoscan.log 2>&1
# Release qualified rows into the review queue. Still not a send.
30 8 * * 1-5 cd $DIR && set -a && . ./.env && set +a && python3 loop_hook.py --act --limit 15 >> /var/log/aeo-act.log 2>&1
$MARK end
CRON

if [ "${1:-}" != "--apply" ]; then
    echo "Would install:"; echo; echo "$ENTRIES"; echo
    echo "Re-run with --apply to install."
    exit 0
fi

TMP="$(mktemp)"
crontab -l 2>/dev/null | sed "/$MARK begin/,/$MARK end/d" > "$TMP" || true
echo "$ENTRIES" >> "$TMP"
crontab "$TMP"
rm -f "$TMP"
echo "installed. current crontab:"
crontab -l | sed -n "/$MARK begin/,/$MARK end/p"
