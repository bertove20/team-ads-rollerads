#!/usr/bin/env bash
# Uji backup: memastikan file backup benar-benar bisa dipakai memulihkan data.
# Tidak mengubah apa pun yang sedang berjalan — semua dites di folder sementara.
#
#   sudo ai-ads-restore-test              # uji backup terbaru
#   sudo ai-ads-restore-test /path/file.tar.gz
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="${BACKUP_DIR:-/var/backups/ai-ads-team}"
FILE="${1:-$(ls -t "$DEST"/ai-ads-*.tar.gz 2>/dev/null | head -1 || true)}"

[[ $EUID -eq 0 ]] || { echo "Jalankan dengan sudo: sudo ai-ads-restore-test"; exit 1; }
[[ -n "$FILE" && -f "$FILE" ]] || { echo "✗ Tidak ada file backup di $DEST. Jalankan dulu: sudo ai-ads-backup"; exit 1; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
echo "Menguji: $FILE"
tar -xzf "$FILE" -C "$TMP"

fail=0
DB="$TMP/team.db"
if [[ -f "$DB" ]]; then
  integrity="$(sqlite3 "$DB" 'PRAGMA integrity_check;')"
  [[ "$integrity" == "ok" ]] && echo "✓ Database utuh (integrity_check ok)" || { echo "✗ Database rusak: $integrity"; fail=1; }
  for t in kv proposals tasks chat_log usage snapshots; do
    n="$(sqlite3 "$DB" "SELECT COUNT(*) FROM $t;" 2>/dev/null || echo ERR)"
    [[ "$n" == "ERR" ]] && { echo "✗ Tabel $t tidak terbaca"; fail=1; } || echo "  - $t: $n baris"
  done
  sites="$(sqlite3 "$DB" "SELECT COUNT(*) FROM kv WHERE key='tracking_sites';")"
  echo "  - data script tracking: $([[ "$sites" == "1" ]] && echo ada || echo "belum ada")"
else
  echo "✗ team.db tidak ada di dalam backup"; fail=1
fi

if [[ -f "$TMP/.env" ]]; then
  keys="$(grep -cE '^[A-Z_]+=' "$TMP/.env" || true)"
  echo "✓ .env ikut ter-backup ($keys pengaturan)"
  grep -qE '^DASHBOARD_PASSWORD=.+' "$TMP/.env" && echo "  - password dashboard ada" || echo "  ! password dashboard kosong"
else
  echo "✗ .env tidak ada di dalam backup"; fail=1
fi

age_days=$(( ( $(date +%s) - $(stat -c %Y "$FILE") ) / 86400 ))
echo "Umur backup: $age_days hari"
[[ $age_days -gt 2 ]] && { echo "! Backup terakhir sudah lama. Cek cron: cat /etc/cron.d/ai-ads-backup"; }

if [[ $fail -eq 0 ]]; then
  echo "✅ Backup ini BISA dipakai memulihkan data. Cara restore ada di README-VPS.md."
else
  echo "❌ Backup bermasalah. Jangan diandalkan; jalankan 'sudo ai-ads-backup' lalu uji lagi." >&2
  exit 1
fi
