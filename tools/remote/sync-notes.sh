#!/bin/sh
# Keeps a read-only copy of your search folders on the server, for the fact search (Read/Grep/Glob).
# A copy, not an rclone mount: Grep reads every file, and over a mount each read is a cloud API call.
# Run from cron, for example every 10 minutes:
#   */10 * * * * /opt/copilot-ai/sync-notes.sh >> /var/log/copilot-ai-sync.log 2>&1
# Settings (environment or the file sync-notes.env next to this script):
#   REMOTE    rclone remote and base folder, for example dropbox:
#   FOLDERS   folders under REMOTE, separated by spaces (no spaces inside a name)
#   DEST      where the copy goes (mounted read-only in the container as /notes)
#   MAX_SIZE  bigger files are left out (default 25M); EXCLUDE: rclone patterns left out
#   MIN_FREE_GB  no copy when the disk has less free space than this (default 10)
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
[ -f "$HERE/sync-notes.env" ] && . "$HERE/sync-notes.env"
: "${REMOTE:?set REMOTE}" "${FOLDERS:?set FOLDERS}"
DEST=${DEST:-$HERE/notes}
MAX_SIZE=${MAX_SIZE:-25M}
EXCLUDE=${EXCLUDE:-"*.gz *.zip *.m4a *.mp3 *.mp4 *.mov"}
MIN_FREE_GB=${MIN_FREE_GB:-10}

exec 9>"$HERE/.sync.lock"
flock -n 9 || { echo "$(date '+%F %T') another copy is running"; exit 0; }

free_gb=$(df -P -k "$HERE" | awk 'NR==2 {print int($4 / 1048576)}')
if [ "$free_gb" -lt "$MIN_FREE_GB" ]; then
  echo "$(date '+%F %T') only ${free_gb} GB free (< ${MIN_FREE_GB}): no copy"
  exit 1
fi

set -f   # the patterns stay patterns
args=""
for p in $EXCLUDE; do args="$args --exclude $p"; done
status=0
for f in $FOLDERS; do
  # sync: files deleted or renamed in the cloud go here too; --delete-excluded drops what a filter left out
  # shellcheck disable=SC2086
  rclone sync "$REMOTE$f" "$DEST/$f" $args --max-size "$MAX_SIZE" --delete-excluded \
    --fast-list --transfers 4 --checkers 8 --log-level NOTICE || status=1
done
echo "$(date '+%F %T') copy done (status $status, $(du -sh "$DEST" | cut -f1))"
exit $status
