#!/usr/bin/env bash
# Backup database tim (usulan, ingatan, riwayat, sesi) + .env. Disimpan di /var/backups/ai-ads-team selama 14 hari.
# Otomatis tiap hari jam 03:00 (cron), atau manual:  sudo ai-ads-backup
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="${BACKUP_DIR:-/var/backups/ai-ads-team}"
KEEP_DAYS="${KEEP_DAYS:-14}"
STAMP="$(date +%Y%m%d-%H%M%S)"

[[ $EUID -eq 0 ]] || { echo "Jalankan dengan sudo: sudo ai-ads-backup"; exit 1; }
mkdir -p "$DEST"
chmod 700 "$DEST"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# Salinan database yang konsisten walau program sedang berjalan
if [[ -f "$APP_DIR/data/team.db" ]]; then
  sqlite3 "$APP_DIR/data/team.db" ".backup '$TMP/team.db'"
fi
[[ -f "$APP_DIR/.env" ]] && cp "$APP_DIR/.env" "$TMP/.env"

tar -czf "$DEST/ai-ads-$STAMP.tar.gz" -C "$TMP" .
chmod 600 "$DEST/ai-ads-$STAMP.tar.gz"
find "$DEST" -name 'ai-ads-*.tar.gz' -mtime +"$KEEP_DAYS" -delete
echo "✓ Backup: $DEST/ai-ads-$STAMP.tar.gz"
