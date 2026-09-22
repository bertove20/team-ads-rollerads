#!/usr/bin/env bash
# Pengawas dari luar program: kalau program mati atau membeku, Owner diberi tahu lewat Telegram.
# Dijalankan cron tiap 5 menit (dipasang oleh deploy/install.sh).
#
# Program menulis "detak" tiap pemeriksaan rutin. Kalau detak terakhir sudah lebih lama dari batas,
# atau service-nya tidak aktif, script ini mengirim pesan — sekali saja sampai keadaan pulih.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE="ai-ads-team"
MAX_AGE_MIN="${MAX_AGE_MIN:-20}"           # detak lebih tua dari ini = dianggap membeku
FLAG=/var/lib/ai-ads-team-alert           # penanda supaya tidak mengirim berulang-ulang
DB="$APP_DIR/data/team.db"

TOKEN="$(grep -E '^BOT_TOKEN_HEAD_MARKETING=' "$APP_DIR/.env" | cut -d= -f2- | tr -d '"'"'"' ')"
CHAT="$(grep -E '^TELEGRAM_GROUP_ID=' "$APP_DIR/.env" | cut -d= -f2- | tr -d '"'"'"' ')"

kirim() {  # kirim pesan Telegram (kalau bot & grup sudah diisi)
  [[ -n "$TOKEN" && -n "$CHAT" ]] || { echo "$1"; return; }
  curl -s -m 20 -X POST "https://api.telegram.org/bot$TOKEN/sendMessage" \
    --data-urlencode "chat_id=$CHAT" --data-urlencode "text=$1" >/dev/null || true
}

masalah=""
if ! systemctl is-active --quiet "$SERVICE"; then
  masalah="Service $SERVICE TIDAK berjalan di VPS."
elif [[ -f "$DB" ]]; then
  last="$(sqlite3 "$DB" "SELECT CAST(value AS INTEGER) FROM kv WHERE key='heartbeat_ts'" 2>/dev/null || echo 0)"
  now="$(date +%s)"
  age=$(( (now - ${last:-0}) / 60 ))
  if [[ ${last:-0} -gt 0 && $age -gt $MAX_AGE_MIN ]]; then
    masalah="Program berjalan tetapi membeku: tidak ada tanda hidup selama $age menit."
  fi
fi

if [[ -n "$masalah" ]]; then
  if [[ ! -f $FLAG ]]; then
    touch $FLAG
    kirim "🆘 Tim AI Ads bermasalah di VPS: $masalah
Iklan tetap jalan di RollerAds, tetapi auto-pause, alert, dan tim AI BERHENTI.
Cek: sudo systemctl status $SERVICE  |  sudo journalctl -u $SERVICE -n 50"
  fi
  systemctl restart "$SERVICE" 2>/dev/null || true
elif [[ -f $FLAG ]]; then
  rm -f $FLAG
  kirim "✅ Tim AI Ads sudah berjalan normal lagi di VPS."
fi
