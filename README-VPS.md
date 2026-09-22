# Deploy ke VPS Ubuntu

Panduan memindahkan Tim AI Ads dari laptop ke VPS supaya jalan **24 jam** tanpa laptop menyala.
Semua perintah tinggal disalin dan ditempel di terminal VPS.

Hasil akhirnya:
- Program jalan terus sebagai *service*. Kalau program error atau VPS reboot, program otomatis hidup lagi.
- Dashboard dibuka di `https://ads.domain-anda.com` dengan **HTTPS otomatis** (gratis, lewat Caddy).
- Dashboard dikunci dengan **halaman login**. Port program tidak dibuka langsung ke internet.
- Database dan `.env` **di-backup otomatis** setiap hari.

---

## 1. Yang perlu disiapkan

| Kebutuhan | Keterangan |
|---|---|
| VPS **Ubuntu 22.04 atau 24.04** | Minimal 1 CPU, 1 GB RAM, dan 10 GB disk sudah cukup. 2 GB RAM lebih lega. |
| Akses SSH ke VPS | User `root`, atau user lain yang bisa `sudo` |
| Domain / subdomain (disarankan) | Misalnya `ads.domain-anda.com`. Tanpa domain tetap bisa, lihat [bagian 8](#8-tanpa-domain-lewat-ssh-tunnel). |
| Akses ke repo GitHub | `github.com/bertove20/team-ads-rollerads` |

## 2. Arahkan domain ke VPS

Buka pengaturan DNS domain Anda (Cloudflare, Niagahoster, Namecheap, dll.), lalu buat record:

| Type | Name | Value |
|---|---|---|
| A | `ads` | IP VPS Anda |

Kalau memakai Cloudflare, set proxy ke **DNS only (awan abu-abu)** dulu sampai HTTPS berhasil dibuat.
Perubahan DNS biasanya aktif dalam beberapa menit.

## 3. Masuk ke VPS

Dari laptop (PowerShell / Terminal):

```bash
ssh root@IP-VPS-ANDA
```

## 4. Ambil kode dari GitHub

Repo ini private, jadi VPS butuh **deploy key sendiri** (hanya-baca). Deploy key ini berbeda dari kunci di laptop.

```bash
# 4a. Buat kunci khusus untuk VPS
ssh-keygen -t ed25519 -N "" -C "vps ai-ads" -f ~/.ssh/ai-ads-deploy
cat ~/.ssh/ai-ads-deploy.pub
```

Salin baris yang muncul (diawali `ssh-ed25519`), lalu daftarkan di GitHub:
**github.com/bertove20/team-ads-rollerads → Settings → Deploy keys → Add deploy key**
- Title: `VPS`
- Key: tempel baris tadi
- **Jangan** centang *Allow write access*, karena VPS cukup membaca.

```bash
# 4b. Pakai kunci itu untuk GitHub, lalu clone ke /opt/ai-ads-team
cat >> ~/.ssh/config <<'EOF'
Host github.com
  IdentityFile ~/.ssh/ai-ads-deploy
  IdentitiesOnly yes
EOF
chmod 600 ~/.ssh/config
ssh-keyscan github.com >> ~/.ssh/known_hosts
git clone git@github.com:bertove20/team-ads-rollerads.git /opt/ai-ads-team
```

> Folder **harus** `/opt/ai-ads-team`, jangan di `/root` atau `/home`, karena service dikunci agar tidak bisa membaca folder home.

## 5. (Disarankan) Salin pengaturan dari laptop

Supaya tidak perlu mengisi ulang semua API key, token bot, dan password, salin `.env` dari laptop.
Jalankan **di laptop** (PowerShell), **sebelum** langkah 6:

```powershell
scp "C:\Users\ASUS\Documents\kerja\ai-ads-team\.env" root@IP-VPS-ANDA:/opt/ai-ads-team/.env
```

Kalau ingin ingatan tim, riwayat usulan, dan grafik tren ikut pindah, salin juga database-nya.
**Matikan dulu program di laptop**, lalu:

```powershell
scp "C:\Users\ASUS\Documents\kerja\ai-ads-team\data\team.db" root@IP-VPS-ANDA:/opt/ai-ads-team/data/team.db
```

(Buat foldernya dulu di VPS kalau belum ada: `mkdir -p /opt/ai-ads-team/data`)

## 6. Instal (satu perintah)

Di VPS:

```bash
cd /opt/ai-ads-team
sudo bash deploy/install.sh ads.domain-anda.com
```

Script ini otomatis:
1. Memasang Python, Git, firewall, dan Node.js (untuk mengecek sintaks script tracking).
2. Membuat user khusus `aiads` untuk menjalankan program (bukan root).
3. Membuat virtualenv dan memasang library.
4. Memasang service `ai-ads-team`, yang jalan 24 jam dan hidup lagi otomatis.
5. Memasang **Caddy** untuk HTTPS di `https://ads.domain-anda.com`.
6. Mengatur firewall: hanya **SSH, 80, dan 443** yang terbuka.
7. Menjadwalkan **backup harian** jam 03:00.
8. Meminta Anda **membuat password dashboard**, kalau `.env` belum punya password.

Kalau selesai, akan muncul tulisan **Selesai!** beserta alamat dashboard.

## 7. Buka dashboard & lengkapi pengaturan

1. Buka `https://ads.domain-anda.com` lalu login.
2. Buka **🔑 Pengaturan**, lalu isi yang belum ada:
   - **Penyedia AI**: API key OpenRouter (atau Claude). Klik **Tes**.
   - **Telegram**: token bot, ID grup, dan ID Owner.
   - **Sumber data**: RollerAds API dan BeMob API.
   - **Script tracking**: URL postback BeMob.
3. Klik **Simpan**, lalu **Jalankan ulang**.
4. Di grup Telegram, ketik `/setup` (cukup sekali, kalau topic belum dibuat).

> ⚠️ **PENTING: matikan program di laptop.** Dua program yang memakai token bot Telegram yang sama akan
> saling berebut (muncul error *Conflict* di log), dan auto-pause bisa berjalan dua kali.
> Setelah pindah ke VPS, tutup jendela `jalankan.bat` di laptop.

## 8. Tanpa domain (lewat SSH tunnel)

Kalau belum punya domain, instal dengan:

```bash
sudo bash deploy/install.sh --no-domain
```

Dashboard **tidak** dibuka ke internet sama sekali, jadi ini paling aman. Untuk membukanya, di laptop jalankan:

```powershell
ssh -L 8090:127.0.0.1:8090 root@IP-VPS-ANDA
```

Selama jendela itu terbuka, buka **http://localhost:8090** di browser laptop.
Kalau nanti punya domain, cukup jalankan ulang `sudo bash deploy/install.sh ads.domain-anda.com`.

---

## Perintah sehari-hari (di VPS)

| Keperluan | Perintah |
|---|---|
| Cek program jalan atau tidak | `sudo systemctl status ai-ads-team` |
| Lihat log langsung | `sudo journalctl -u ai-ads-team -f` (keluar: Ctrl+C) |
| Jalankan ulang | `sudo systemctl restart ai-ads-team` (atau tombol di dashboard) |
| Hentikan / nyalakan | `sudo systemctl stop ai-ads-team` / `sudo systemctl start ai-ads-team` |
| Ganti password dashboard | `sudo ai-ads-password` |
| Update ke versi terbaru | `sudo ai-ads-update` |
| Backup sekarang | `sudo ai-ads-backup` |

## Update program

1. Setelah ada perubahan kode, **push ke GitHub** dari laptop.
2. Di VPS jalankan:

```bash
sudo ai-ads-update
```

Perintah ini otomatis membuat backup, mengambil kode terbaru, memasang library baru, lalu menjalankan ulang program.
`.env` dan database **tidak** tersentuh.

## Backup & restore

- Backup otomatis setiap hari jam **03:00** ke `/var/backups/ai-ads-team/`, dan disimpan **14 hari**.
- Isinya database tim (usulan, ingatan, riwayat, sesi login) dan `.env`.
- File backup berisi API key, jadi **jangan dibagikan**.

Mengambil backup ke laptop:

```powershell
scp root@IP-VPS-ANDA:/var/backups/ai-ads-team/ai-ads-XXXXXXXX-XXXXXX.tar.gz .
```

Mengembalikan backup di VPS:

```bash
sudo systemctl stop ai-ads-team
cd /tmp && mkdir restore && tar -xzf /var/backups/ai-ads-team/ai-ads-XXXXXXXX-XXXXXX.tar.gz -C restore
sudo cp restore/team.db /opt/ai-ads-team/data/team.db
sudo cp restore/.env /opt/ai-ads-team/.env
sudo chown aiads:aiads /opt/ai-ads-team/data/team.db /opt/ai-ads-team/.env
sudo systemctl start ai-ads-team
```

## Keamanan yang sudah terpasang

- Program jalan sebagai user `aiads` yang terbatas. Program **tidak bisa mengubah kodenya sendiri**, dan hanya
  bisa menulis ke `data/` dan `.env`.
- Dashboard hanya mendengar di `127.0.0.1`. Dari internet, satu-satunya jalan adalah lewat Caddy (HTTPS).
- Login pakai password yang disimpan sebagai **hash**, cookie aman (HttpOnly, Secure, SameSite), dan sesi
  berakhir sendiri.
- **5x salah password dari satu IP = IP diblokir 15 menit**, dan Anda diberi tahu di Telegram.
- Password pertama **tidak bisa dibuat dari internet**, hanya lewat `sudo ai-ads-password` di VPS.
- Domain lain yang diarahkan ke VPS ini ditolak.
- Firewall hanya membuka SSH, 80, dan 443.

Disarankan juga: login SSH memakai **SSH key** (bukan password), lalu matikan login password SSH.

## Kalau ada masalah

| Gejala | Penyebab & solusi |
|---|---|
| `https://ads…` tidak bisa dibuka / sertifikat error | DNS belum mengarah ke IP VPS, atau port 80/443 diblokir firewall penyedia VPS. Cek dengan `ping ads.domain-anda.com`. Lihat log Caddy: `sudo journalctl -u caddy -n 50`. Kalau pakai Cloudflare, set ke *DNS only* dulu. |
| Tulisan *"Alamat tidak dikenal"* (421) | Domain di browser berbeda dari `DASHBOARD_DOMAIN` di `.env`. Jalankan ulang `sudo bash deploy/install.sh domain-yang-benar`. |
| Lupa password dashboard | `sudo ai-ads-password` |
| Bot Telegram diam / log berisi *Conflict* | Program masih jalan di laptop atau di tempat lain. Matikan salah satunya. |
| Service tidak mau jalan | `sudo journalctl -u ai-ads-team -n 50`. Biasanya ada isi `.env` yang salah. |
| Tombol *Jalankan ulang* tidak muncul | Normal di Windows tanpa `jalankan.bat`. Di VPS tombol ini selalu tersedia. |
| `git pull` ditolak saat update | Deploy key VPS belum didaftarkan di GitHub (langkah 4), atau file di VPS diubah manual. |

## Isi folder `deploy/`

| File | Fungsi |
|---|---|
| `install.sh` | Instalasi lengkap (aman dijalankan ulang) |
| `update.sh` | Update dari GitHub + restart (`sudo ai-ads-update`) |
| `backup.sh` | Backup database & `.env` (`sudo ai-ads-backup`, otomatis jam 03:00) |
| `ai-ads-team.service` | Template service systemd |
