# Tim AI Ads 24 Jam (Telegram + Claude)

Delapan agent AI (Head of Marketing, Ads Manager, Media Buyer, Analyst, Tracking Specialist,
Creative, Developer, QA) bekerja di **satu grup Telegram** yang dibagi per topic. Anda (Owner)
memberi keputusan akhir lewat tombol ✅ Setuju / ❌ Tolak.

## Dashboard (tampilan di browser)

Saat program berjalan, buka **http://localhost:8090** (dibuka otomatis oleh `jalankan.bat`).
Jika Python belum terpasang, `jalankan.bat` otomatis memakai Python bawaan Laragon (`C:\laragon\bin\python`).

| Menu | Isi |
|---|---|
| 🏠 Ringkasan | Angka utama hari ini, **evaluasi otomatis dalam bahasa sederhana**, pemakaian batas spend, grafik biaya vs pendapatan |
| 📦 Campaign | Profit per campaign (grafik + tabel) dengan penilaian: bagus / untung tipis / rugi |
| 🚀 RollerAds | Saldo, campaign + tombol Pause/Aktifkan, aturan & riwayat auto-pause, **form buat campaign baru** (bisa diisi AI) |
| 🧩 Zone | Semua zone, filter boros/bagus, urutkan, cari, dan **salin ID zone** untuk blacklist RollerAds |
| 📈 Tren | Riwayat profit, biaya, dan pendapatan per hari (30 hari) |
| 🎯 Usulan & Tugas | Tombol Setuju/Tolak usulan tim AI dan daftar tugas manual |
| 💬 Rapat & Laporan | Mulai rapat, buat laporan harian AI, baca semua obrolan tim per topic |
| 🙋 Tanya Tim | Tanya langsung ke agent mana pun, minta ide kreatif iklan |
| 🔌 Script Tracking | Script tracking per website (salin/unduh), siapa pembuat & pemeriksanya, tes koneksi |
| 🛰️ Landing Page | Status aktif/mati, kecepatan, dan hasil QA landing page |
| 💰 Biaya AI | Biaya Claude per hari dan per agent |
| 🔑 Pengaturan | Isi API key Claude, token bot, ID grup/Owner (ada tombol **Deteksi**), sumber data, batas pengaman, jadwal, password dashboard. Tiap bagian punya tombol **Tes**, lalu **Simpan** dan **Jalankan ulang** |
| ❓ Bantuan | Checklist pemasangan, batas pengaman aktif, unggah CSV BeMob, kamus istilah |

Dashboard sudah bisa dipakai **sebelum Telegram dipasang**. Jika `BOT_TOKEN_HEAD_MARKETING` kosong,
program berjalan dalam mode dashboard saja: rapat, laporan, dan cek landing page bisa dijalankan dari
browser, tetapi jadwal otomatis baru aktif setelah bot Telegram diisi.

Di VPS: isi `DASHBOARD_HOST=0.0.0.0` **dan** `DASHBOARD_PASSWORD`, supaya dashboard tidak terbuka
untuk semua orang.

## Apa yang dikerjakan otomatis

| Kapan | Siapa | Apa | Topic |
|---|---|---|---|
| Tiap 15 menit | Tracking Specialist | Cek landing page & link tracker. Alert hanya saat mati/pulih/lambat | 🚨 Alert Tracking |
| Tiap 1 jam | Analyst | Laporan angka per campaign, deteksi zone boros | 📊 Laporan |
| Jika banyak zone boros baru | Semua | Rapat darurat (maks 1x per 2 jam) | 💬 Diskusi Strategi |
| 10:00, 16:00, 22:00 | Semua | Rapat rutin → usulan dikirim ke Owner | 💬 Diskusi → 🎯 Approval |
| 07:00 | QA | Cek kecepatan & tampilan mobile LP | ✅ QA |
| 08:00 | Head of Marketing | Laporan harian | 📊 Laporan |
| Terjadwal | Head of Marketing | Pengingat harian (top-up saldo, dll.) | ⏰ Reminder |
| Tiap 3 jam | Media Buyer | Mengingatkan tugas manual yang belum dikerjakan | ⏰ Reminder |
| Tiap 15 menit | Sistem (tanpa AI) | **Auto-pause** campaign RollerAds yang boros / hasilnya jelek | 🚨 Alert Tracking |

Semua jadwal bisa diubah di menu Pengaturan pada dashboard (tersimpan di `.env`).

**Alur usulan:** rapat → usulan final → dicek sistem pengaman (batas bid, budget, dan zone harus
ada di data) → tombol Setuju/Tolak untuk Owner → jika disetujui: **pause campaign** dan **campaign baru**
langsung dijalankan lewat RollerAds API; blacklist zone, ubah bid, dan ubah budget menjadi **tugas manual**
→ diingatkan sampai Anda klik "Sudah dikerjakan".

## RollerAds API
Isi `ROLLERADS_API_KEY` (token `sk_api_...` dari account manager RollerAds). Campaign berstatus
**archived tidak pernah dibaca atau diubah**.

**Auto-pause** (tiap `AUTOPAUSE_MINUTES`, hanya campaign *active*). Campaign di-pause jika:
- spend seluruh akun hari ini ≥ `MAX_DAILY_SPEND_USD` (semua campaign aktif di-pause),
- spend campaign hari ini ≥ `MAX_CAMPAIGN_DAILY_BUDGET_USD` (tidak wajar),
- spend ≥ `AUTOPAUSE_MIN_SPEND_USD` tanpa konversi, atau CPA > `AUTOPAUSE_MAX_CPA_USD`, atau
  ROI < `AUTOPAUSE_MIN_ROI_PCT` (ROI hanya jika `ROLLERADS_PAYOUT_USD` diisi).

Jika `BEMOB_ACCESS_KEY` & `BEMOB_SECRET_KEY` diisi (BeMob → Settings → Security → Create API Key, Read Only),
konversi & pendapatan asli diambil dari BeMob (traffic source RollerAds: custom1 = campaign ID, custom3 = zone ID),
dan sistem memberi alert jika BeMob mencatat konversi tetapi RollerAds 0 (postback bermasalah).
Tanpa BeMob API, konversi dibaca dari RollerAds, jadi **postback BeMob → RollerAds wajib terpasang**. Tanpa postback,
setiap campaign yang spend ≥ `AUTOPAUSE_MIN_SPEND_USD` akan di-pause. Setiap auto-pause dikirim ke
Telegram dengan tombol **▶️ Aktifkan lagi**; campaign yang Anda aktifkan lagi tidak di-pause ulang hari itu.

**Buat campaign**: dari dashboard (menu 🚀 RollerAds), `/campaignbaru <permintaan>`, atau minta ke agent
(mis. "@bot_media_buyer buatkan campaign popunder ID CPM 1.5 budget 20 ke https://..."). Campaign dari
Telegram dikirim sebagai usulan dan baru dibuat setelah Anda tekan Setuju. Bid maks `CAMPAIGN_MAX_BID_USD`,
budget harian wajib dan maks `MAX_CAMPAIGN_DAILY_BUDGET_USD`. Status awal default *paused*.

## Pilih AI per agent (Claude, OpenAI, Gemini, DeepSeek)
Di menu **🔑 Pengaturan → 🧠 Penyedia AI & pembagian tugas**, pilih AI untuk tiap agent (`AI_<AGENT>` di `.env`),
mis. `AI_DEVELOPER=claude` dan `AI_QA=openai`. Isi API key AI yang dipakai saja, lalu klik **Tes semua AI**.
Jika AI pilihan agent gagal (key kosong, saldo habis, error, jawaban tidak valid), tugasnya otomatis dikerjakan
AI berikutnya di `AI_FALLBACK` (default `claude,openai,gemini,deepseek`, hanya yang key-nya terisi). Kosongkan
`AI_FALLBACK` jika tidak ingin cadangan. Biaya AI selain Claude dihitung perkiraan ($5 / $25 per 1 juta token).

## Script tracking website
Setiap campaign baru (dari dashboard atau usulan yang disetujui), tim otomatis menyiapkan script yang ditempel di
**footer** website tujuan supaya pendaftaran & deposit tercatat di BeMob (lalu diteruskan ke RollerAds):

1. **Developer** membuat script dari HTML website (+ contoh HTML dari Owner jika ada).
2. Sistem mengecek otomatis: sintaks (jika Node.js terpasang), URL postback, `click_id`, `txid`, tidak ada domain luar.
3. **QA** me-review kodenya. Jika ditolak, Developer merevisi; jika AI Developer gagal/ditolak dua kali, AI cadangan
   berikutnya membantu (maks 3 AI). Jika semua AI gagal, dipakai template standar (`snippets/bemob-footer.html`).
4. Script dikirim ke topic 🎨 Creative & LP (sebagai file) dan tampil di dashboard **🔌 Script Tracking** (tombol Salin).
5. **QA** mengetes koneksi: script terpasang (versi terbaru), link BeMob meneruskan `click_id` ke website, postback
   BeMob → RollerAds terpasang, dan konversi hari ini. Hasil + kesimpulan dikirim ke topic ✅ QA.

Setiap cek landing page (tiap 15 menit), sistem juga memeriksa apakah script masih terpasang. Saat script baru
terdeteksi terpasang, QA otomatis mengetes koneksinya; jika script hilang, Tracking Specialist mengirim alert.
Satu website cukup satu script: campaign berikutnya ke website yang sama tidak perlu tempel ulang.

Isi `BEMOB_POSTBACK_URL` (mis. `https://xxxxx.bemobtrcks.com/postback`), `TRACK_REG_PAYOUT_USD`, `TRACK_KURS_USD`,
dan `TRACK_DEPOSIT_SHARE` di Pengaturan → 🔌 Script tracking website. Di Telegram: `/script <url>` dan `/testracking`.

## Langkah pemasangan

### 1. Pasang Python
Unduh Python 3.12 dari https://www.python.org/downloads/ dan **centang "Add python.exe to PATH"**
saat instalasi.

### 2. Dapatkan API key Claude
Daftar di https://console.anthropic.com, isi saldo, buat API key.

### 3. Buat bot Telegram (di @BotFather)
Buat 8 bot dengan `/newbot`, satu per agent (nama bebas, mis. "HoM Rina", "Analyst Budi").
Simpan tokennya. Minimal **bot Head of Marketing** wajib ada; agent tanpa bot akan menumpang
bicara lewat bot Head of Marketing.

Khusus bot **Head of Marketing**:
- `/setprivacy` → pilih bot → **Disable** (agar bisa membaca pesan grup).

### 4. Siapkan grup
1. Buat grup baru, masukkan semua bot.
2. Pengaturan grup → aktifkan **Topics**.
3. Jadikan bot Head of Marketing **admin** dengan izin **Manage Topics**.
   (Bot lain sebaiknya juga admin agar bisa posting di semua topic.)

### 5. Isi konfigurasi dan jalankan
1. Klik dua kali `jalankan.bat`. Pertama kali, ia menginstal kebutuhan lalu membuka dashboard di browser.
2. Buka menu **🔑 Pengaturan**. Isi API key Claude dan token bot, klik **Tes**, lalu **Simpan** →
   **Jalankan ulang sekarang**.
3. Di grup Telegram, ketik `/id`. Kembali ke Pengaturan → Telegram, klik **Deteksi** pada ID grup dan
   ID Owner, pilih yang benar, klik **Tes Telegram**, lalu **Simpan** → **Jalankan ulang**.
4. Di grup, ketik `/setup` untuk membuat semua topic.
5. Coba `/rapat` (atau tombol **Mulai rapat** di dashboard) untuk melihat para agent berdiskusi.

## Sumber data
- `DATA_SOURCE=demo`: data contoh, untuk uji coba.
- `DATA_SOURCE=csv`: export laporan BeMob (dikelompokkan per zone) ke folder `data/inbox/`.
  Sistem membaca file terbaru. Sesuaikan nama kolom `COL_...` di `.env` dengan header CSV Anda.
- `DATA_SOURCE=bemob`: tarik langsung dari BeMob API. Buat API key (Read Only) di BeMob, lalu isi
  `BEMOB_REPORT_URL`, `BEMOB_AUTH_HEADER`, `BEMOB_API_KEY`, dan `BEMOB_ROWS_KEY` sesuai dokumentasi
  API di akun BeMob Anda.
- `DATA_SOURCE=rollerads`: tarik langsung dari RollerAds API (biaya & konversi per zone). RollerAds tidak tahu
  pendapatan Anda; isi `ROLLERADS_PAYOUT_USD` agar pendapatan = konversi × payout.

## Perintah di grup (khusus Owner)
`/rapat` `/laporan` `/cek` `/kreatif [brief]` `/tugas` `/campaign` `/campaignbaru [permintaan]` `/script [url]` `/testracking [domain]` `/biaya` `/pause` `/lanjut` `/setup` `/id`

Ngobrol dengan agent: **mention** bot-nya, **reply** pesannya, atau awali pesan dengan jabatannya
(mis. "Analyst, kenapa ROI turun?").

## Biaya & keamanan
- Semua agent memakai `claude-opus-5` (atur di `CLAUDE_MODEL`). Angka dihitung oleh kode, bukan
  AI, dan pemeriksaan LP tidak memakai AI sama sekali. Cek pemakaian dengan `/biaya`.
- Hanya ID di `OWNER_TELEGRAM_IDS` yang bisa memberi perintah, menekan tombol, dan memakai AI.
- `/pause` menghentikan otomatisasi AI, tetapi **tidak** mem-pause campaign di RollerAds.
- Jangan pernah membagikan file `.env`.
- Selama berjalan di laptop, sistem hanya aktif jika laptop menyala dan tidak sleep.
  Untuk benar-benar 24 jam, pindahkan folder ini ke VPS.
