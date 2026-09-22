#!/usr/bin/env bash
# Update program dari GitHub lalu jalankan ulang. Dipanggil lewat:  sudo ai-ads-update
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE="ai-ads-team"
[[ $EUID -eq 0 ]] || { echo "Jalankan dengan sudo: sudo ai-ads-update"; exit 1; }
cd "$APP_DIR"

echo "==> Backup dulu sebelum update…"
bash deploy/backup.sh

echo "==> Mengambil versi terbaru dari GitHub…"
BEFORE="$(git rev-parse --short HEAD)"
git pull --ff-only
AFTER="$(git rev-parse --short HEAD)"

echo "==> Memasang library Python (jika ada yang baru)…"
.venv/bin/pip install -q -r requirements.txt

# Service systemd ikut diperbarui jika file-nya berubah
APP_USER="$(systemctl show -p User --value "$SERVICE" 2>/dev/null || echo aiads)"
sed -e "s#__APP_DIR__#$APP_DIR#g" -e "s#__APP_USER__#${APP_USER:-aiads}#g" deploy/ai-ads-team.service \
  > "/etc/systemd/system/$SERVICE.service"
systemctl daemon-reload

chown -R root:root "$APP_DIR"
chown -R "${APP_USER:-aiads}:${APP_USER:-aiads}" data .env

echo "==> Menjalankan ulang program…"
systemctl restart "$SERVICE"
sleep 4
if systemctl is-active --quiet "$SERVICE"; then
  echo "✓ Update selesai ($BEFORE → $AFTER). Program berjalan."
else
  journalctl -u "$SERVICE" -n 30 --no-pager
  echo "✗ Program tidak berjalan setelah update. Lihat log di atas." >&2
  exit 1
fi
