#!/usr/bin/env bash
# One-shot bootstrap to get mac-mini ready for the weekly refresh cron.
#
# Run this ONCE from the developer mac (this script ssh+rsync's the gitignored
# state — db, parsed corpus, side-cars, R2 upload manifest — over to mac-mini).
# After this, ~/Library/LaunchAgents/com.hospitalledger.refresh.plist's weekly
# fire will have everything it needs.
#
# Usage (from the developer mac):
#   bash scripts/bootstrap-mac-mini.sh           # rsync over SSH
#   bash scripts/bootstrap-mac-mini.sh --dry-run # show what would copy
#
# Total transfer: ~6 GB (5.8 GB parsed + 12 MB db + ~11 MB side-cars).
# Over a LAN: 1–3 min. Over WAN: depends.

set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

REMOTE="${REMOTE:-mac-mini}"
REMOTE_DIR="${REMOTE_DIR:-/Users/barkleesanders/projects/hospital-ledger}"
DRY="${1:-}"
RSYNC_FLAGS=(-az --partial --stats)
[ "$DRY" = "--dry-run" ] && RSYNC_FLAGS+=(--dry-run)

echo "Bootstrapping $REMOTE:$REMOTE_DIR — gitignored state only"
echo "(skips: .venv/, .wrangler/, dist/, node_modules/ — those rebuild on the mac mini)"
echo ""

# Critical files refresh.sh needs that are gitignored:
PATHS=(
  "db/hospital_ledger.db"
  "data/parsed/"
  "data/_payer_raw.jsonl"
  "data/_compliance_per_hospital.jsonl"
  "data/r2_upload_manifest.json"
)

ssh "$REMOTE" "mkdir -p $REMOTE_DIR/db $REMOTE_DIR/data/parsed $REMOTE_DIR/data/parse_errors $REMOTE_DIR/data/refresh_logs"

for p in "${PATHS[@]}"; do
  if [ -e "$p" ]; then
    echo "→ $p"
    rsync "${RSYNC_FLAGS[@]}" "$p" "$REMOTE:$REMOTE_DIR/${p%/*}/" || {
      echo "  (rsync of $p failed; continuing)"
    }
  else
    echo "× $p (missing locally — skipping; refresh.sh will regenerate)"
  fi
done

echo ""
echo "Verifying remote state..."
ssh "$REMOTE" bash -lc "'cd $REMOTE_DIR && du -sh db data 2>/dev/null && echo --- && ls data/parsed/*.json.gz 2>/dev/null | wc -l | tr -d \" \" | xargs -I{} echo \"data/parsed/: {} .json.gz files\"'"

echo ""
echo "Bootstrap complete. Trigger the first real refresh with:"
echo "  ssh $REMOTE 'launchctl kickstart -k gui/\$(id -u)/com.hospitalledger.refresh'"
echo "  ssh $REMOTE 'tail -F $REMOTE_DIR/data/refresh_logs/launchd.out.log'"
