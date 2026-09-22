#!/usr/bin/env bash
# Instalasi Tim AI Ads di VPS Ubuntu (22.04 / 24.04).
#
#   sudo bash deploy/install.sh                      # tanya domain secara interaktif
#   sudo bash deploy/install.sh ads.domain-anda.com  # langsung dengan domain (HTTPS otomatis via Caddy)
#   sudo bash deploy/install.sh --no-domain          # tanpa domain: dashboard dibuka lewat SSH tunnel
#
# Aman dijalankan ulang (mis. setelah update): yang sudah ada tidak ditimpa.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_USER="${APP_USER:-aiads}"
SERVICE="ai-ads-team"
PORT_DEFAULT="8090"

info() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m!! %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31mGAGAL: %s\033[0m\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Jalankan dengan sudo:  sudo bash deploy/install.sh"
[[ -f "$APP_DIR/main.py" ]] || die "main.py tidak ditemukan di $APP_DIR"
case "$APP_DIR" in
  /home/*|/root/*) die "Folder program ada di $APP_DIR. Pindahkan ke /opt/ai-ads-team (lihat README-VPS.md) lalu ulangi." ;;
esac
if [[ -r /etc/os-release ]]; then . /etc/os-release; [[ "${ID:-}" == "ubuntu" ]] || warn "OS bukan Ubuntu (${ID:-?}). Dilanjutkan, tapi belum dites."; fi

# ------------------------------------------------------------------ domain
DOMAIN="${1:-}"
if [[ "$DOMAIN" == "--no-domain" ]]; then
  DOMAIN=""
elif [[ -z "$DOMAIN" && -t 0 ]]; then
  echo
  echo "Domain untuk dashboard, mis. ads.domain-anda.com (DNS A record harus sudah mengarah ke IP VPS ini)."
  read -rp "Domain (kosongkan jika belum punya): " DOMAIN
fi
DOMAIN="$(echo "$DOMAIN" | tr 'A-Z' 'a-z' | sed -E 's#^https?://##; s#/.*$##')"
if [[ -n "$DOMAIN" && ! "$DOMAIN" =~ ^[a-z0-9-]+(\.[a-z0-9-]+)+$ ]]; then die "Domain tidak valid: $DOMAIN"; fi

# ------------------------------------------------------------------ paket sistem
info "Memasang paket sistem (python, git, firewall)…"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q python3 python3-venv python3-pip git curl ca-certificates ufw sqlite3 nodejs >/dev/null
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
  || die "Butuh Python 3.10 atau lebih baru (Ubuntu 22.04+). Versi sekarang: $(python3 --version)"

# ------------------------------------------------------------------ user & folder
info "Menyiapkan user '$APP_USER' dan folder $APP_DIR…"
id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --home-dir "$APP_DIR" --no-create-home \
  --shell /usr/sbin/nologin "$APP_USER"
cd "$APP_DIR"
mkdir -p data
[[ -f .env ]] || { cp .env.example .env; echo "  .env dibuat dari .env.example"; }
# Kode milik root (service tidak bisa mengubahnya); hanya data/ dan .env milik user service.
chown -R root:root "$APP_DIR"
chown -R "$APP_USER:$APP_USER" data .env
chmod 700 data
chmod 600 .env

# ------------------------------------------------------------------ python
info "Membuat virtualenv dan memasang library Python…"
[[ -x .venv/bin/python ]] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

# ------------------------------------------------------------------ .env
set_env() {  # set_env KEY VALUE : ganti baris KEY= atau tambahkan di akhir
  if grep -qE "^$1=" .env; then sed -i -E "s#^$1=.*#$1=$2#" .env; else echo "$1=$2" >> .env; fi
}
# Dashboard selalu hanya mendengar di 127.0.0.1; dari internet lewat Caddy (HTTPS) atau SSH tunnel.
set_env DASHBOARD_HOST 127.0.0.1
grep -qE "^DASHBOARD_PORT=[0-9]+" .env || set_env DASHBOARD_PORT "$PORT_DEFAULT"
PORT="$(grep -E '^DASHBOARD_PORT=' .env | cut -d= -f2)"
set_env DASHBOARD_DOMAIN "$DOMAIN"

# ------------------------------------------------------------------ systemd
info "Memasang service systemd '$SERVICE'…"
sed -e "s#__APP_DIR__#$APP_DIR#g" -e "s#__APP_USER__#$APP_USER#g" deploy/ai-ads-team.service \
  > "/etc/systemd/system/$SERVICE.service"
systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null

# Perintah singkat untuk Owner
cat > /usr/local/bin/ai-ads-password <<EOF
#!/usr/bin/env bash
# Buat / ganti password dashboard Tim AI Ads.
set -e
[[ \$EUID -eq 0 ]] || { echo "Jalankan dengan sudo: sudo ai-ads-password"; exit 1; }
cd "$APP_DIR"
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/python" set_password.py && systemctl restart "$SERVICE" && echo "Program dijalankan ulang."
EOF
cat > /usr/local/bin/ai-ads-update <<EOF
#!/usr/bin/env bash
exec bash "$APP_DIR/deploy/update.sh" "\$@"
EOF
cat > /usr/local/bin/ai-ads-backup <<EOF
#!/usr/bin/env bash
exec bash "$APP_DIR/deploy/backup.sh" "\$@"
EOF
cat > /usr/local/bin/ai-ads-restore-test <<EOF
#!/usr/bin/env bash
exec bash "$APP_DIR/deploy/restore-test.sh" "\$@"
EOF
chmod 755 /usr/local/bin/ai-ads-password /usr/local/bin/ai-ads-update /usr/local/bin/ai-ads-backup \
  /usr/local/bin/ai-ads-restore-test

# Backup harian jam 03:00, uji backup tiap Senin 03:30, dan pengawas "program mati" tiap 5 menit.
cat > /etc/cron.d/ai-ads-team <<EOF
0 3 * * * root bash $APP_DIR/deploy/backup.sh >/dev/null 2>&1
30 3 * * 1 root bash $APP_DIR/deploy/restore-test.sh >/dev/null 2>&1
*/5 * * * * root bash $APP_DIR/deploy/heartbeat-check.sh >/dev/null 2>&1
EOF
rm -f /etc/cron.d/ai-ads-backup   # nama lama dari versi sebelumnya

# ------------------------------------------------------------------ HTTPS (Caddy)
if [[ -n "$DOMAIN" ]]; then
  info "Memasang Caddy untuk HTTPS otomatis di https://$DOMAIN …"
  if ! command -v caddy >/dev/null; then
    apt-get install -y -q debian-keyring debian-archive-keyring apt-transport-https gnupg >/dev/null
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
      | gpg --batch --yes --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
    apt-get update -q && apt-get install -y -q caddy >/dev/null
  fi
  CADDY_BLOCK="# ai-ads-team (dibuat deploy/install.sh)
$DOMAIN {
	encode gzip
	reverse_proxy 127.0.0.1:$PORT
}"
  CADDYFILE=/etc/caddy/Caddyfile
  if [[ -f $CADDYFILE ]] && grep -q "^$DOMAIN {" $CADDYFILE; then
    echo "  $CADDYFILE sudah berisi $DOMAIN, tidak diubah."
  else
    [[ -f $CADDYFILE ]] && cp $CADDYFILE "$CADDYFILE.bak-$(date +%Y%m%d%H%M%S)"
    if [[ -f $CADDYFILE ]] && grep -q "^:80 {" $CADDYFILE; then
      echo "$CADDY_BLOCK" > $CADDYFILE       # masih Caddyfile bawaan (halaman contoh :80): ganti
    else
      printf '\n%s\n' "$CADDY_BLOCK" >> $CADDYFILE   # sudah dipakai situs lain: tambahkan saja
    fi
  fi
  caddy validate --config /etc/caddy/Caddyfile >/dev/null 2>&1 || die "Caddyfile tidak valid. Cek /etc/caddy/Caddyfile"
  systemctl enable caddy >/dev/null
  systemctl reload caddy 2>/dev/null || systemctl restart caddy
fi

# ------------------------------------------------------------------ firewall
info "Mengatur firewall (SSH tetap dibuka)…"
SSH_PORTS="$(ss -tlnpH 2>/dev/null | awk '/sshd/ {n=split($4,a,":"); print a[n]}' | sort -u)"
ufw allow OpenSSH >/dev/null
for p in $SSH_PORTS; do ufw allow "$p/tcp" >/dev/null; done
if [[ -n "$DOMAIN" ]]; then ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null; fi
ufw --force enable >/dev/null
echo "  Port terbuka: SSH (${SSH_PORTS:-22})$([[ -n "$DOMAIN" ]] && echo ', 80, 443'). Dashboard ($PORT) TIDAK terbuka langsung ke internet."

# ------------------------------------------------------------------ password dashboard
if ! grep -qE '^DASHBOARD_PASSWORD=.+' .env; then
  if [[ -t 0 ]]; then
    info "Buat password dashboard"
    sudo -u "$APP_USER" .venv/bin/python set_password.py || warn "Password belum dibuat. Jalankan nanti: sudo ai-ads-password"
  else
    warn "Password dashboard belum dibuat. Jalankan: sudo ai-ads-password"
  fi
fi

# ------------------------------------------------------------------ jalankan
info "Menjalankan program…"
systemctl restart "$SERVICE"
sleep 4
if systemctl is-active --quiet "$SERVICE"; then
  echo "  ✓ Service $SERVICE berjalan."
else
  journalctl -u "$SERVICE" -n 30 --no-pager
  die "Service tidak berjalan. Lihat log di atas."
fi

IP="$(curl -s -4 --max-time 5 https://ifconfig.me || echo IP-VPS)"
echo
echo "================================================================"
echo " Selesai!"
if [[ -n "$DOMAIN" ]]; then
  echo " Dashboard : https://$DOMAIN"
  echo "             (sertifikat HTTPS dibuat otomatis, bisa perlu 1-2 menit)"
else
  echo " Dashboard : buka dari komputer Anda lewat SSH tunnel:"
  echo "             ssh -L $PORT:127.0.0.1:$PORT <user>@$IP"
  echo "             lalu buka http://localhost:$PORT di browser"
fi
echo " Isi API key & bot Telegram : dashboard → Pengaturan (lalu klik Jalankan ulang)"
echo " Ganti password dashboard   : sudo ai-ads-password"
echo " Update program             : sudo ai-ads-update"
echo " Backup manual              : sudo ai-ads-backup   (otomatis tiap hari 03:00)"
echo " Uji backup bisa dipakai    : sudo ai-ads-restore-test  (otomatis tiap Senin)"
echo " Program mati/membeku       : dicek tiap 5 menit, Anda diberi tahu lewat Telegram"
echo " Lihat log                  : sudo journalctl -u $SERVICE -f"
echo "================================================================"
