"""Membaca, memvalidasi, dan menyimpan pengaturan di file .env (dipakai halaman Pengaturan di dashboard)."""
import re
import shutil

from dotenv import dotenv_values

import auth
import config

ENV_PATH = config.BASE_DIR / ".env"
EXAMPLE_PATH = config.BASE_DIR / ".env.example"

MODELS = [
    ("claude-opus-5", "Claude Opus 5: paling pintar (± $5 / $25 per 1 juta token)"),
    ("claude-sonnet-5", "Claude Sonnet 5: seimbang, lebih hemat (± $2 / $10)"),
    ("claude-haiku-4-5", "Claude Haiku 4.5: paling murah & cepat (± $1 / $5)"),
    ("claude-fable-5-1", "Claude Fable 5.1: paling mahal (± $10 / $50)"),
]

BOT_TOKENS = [
    ("BOT_TOKEN_HEAD_MARKETING", "Head of Marketing (WAJIB)"),
    ("BOT_TOKEN_ADS_MANAGER", "Ads Manager"),
    ("BOT_TOKEN_MEDIA_BUYER", "Media Buyer"),
    ("BOT_TOKEN_ANALYST", "Analyst"),
    ("BOT_TOKEN_TRACKING", "Tracking Specialist"),
    ("BOT_TOKEN_CREATIVE", "Creative Team"),
    ("BOT_TOKEN_DEVELOPER", "Developer"),
    ("BOT_TOKEN_QA", "QA"),
]


PROVIDER_OPTIONS = [
    {"value": "claude", "label": "Claude (Anthropic)"},
    {"value": "openai", "label": "OpenAI (ChatGPT)"},
    {"value": "gemini", "label": "Gemini (Google)"},
    {"value": "deepseek", "label": "DeepSeek"},
]
PROVIDERS = {o["value"] for o in PROVIDER_OPTIONS}

# (key agent, jabatan, tugas yang relevan) - key sama dengan agents.AGENTS
AGENT_ROLES = [
    ("head_marketing", "Head of Marketing", "memimpin rapat & laporan harian"),
    ("ads_manager", "Ads Manager", "mengkritisi usulan"),
    ("media_buyer", "Media Buyer", "menyusun campaign baru"),
    ("analyst", "Analyst", "membaca angka"),
    ("tracking", "Tracking Specialist", "menjaga tracking"),
    ("creative", "Creative Team", "ide iklan"),
    ("developer", "Developer", "MEMBUAT script tracking website"),
    ("qa", "QA", "MEMERIKSA script & mengetes koneksi tracking"),
]


def _f(key, label, type="text", help="", options=None, **extra) -> dict:
    return {"key": key, "label": label, "type": type, "help": help, "options": options, **extra}


# Semua pengaturan yang bisa diubah dari dashboard, dikelompokkan per bagian.
SCHEMA = [
    {
        "id": "claude", "title": "🤖 Claude AI", "test": "claude",
        "intro": "Otak para agent. Buat API key di console.anthropic.com → API Keys, dan pastikan saldo terisi.",
        "fields": [
            _f("ANTHROPIC_API_KEY", "API key Claude", "secret", "Diawali sk-ant-…"),
            _f("CLAUDE_MODEL", "Model AI", "select", "Model lebih murah = biaya lebih kecil, jawaban sedikit kurang tajam.",
               [{"value": v, "label": l} for v, l in MODELS]),
        ],
    },
    {
        "id": "ai", "title": "🧠 Penyedia AI & pembagian tugas", "test": "ai",
        "intro": "Pilih AI untuk tiap anggota tim, mis. Claude membuat script (Developer) dan OpenAI memeriksanya (QA). "
                 "Jika AI pilihan gagal (key kosong, saldo habis, error), tugasnya otomatis dibantu AI cadangan. "
                 "Isi API key AI yang ingin dipakai saja.",
        "fields": [
            _f("OPENAI_API_KEY", "API key OpenAI", "secret", "platform.openai.com → API keys. Diawali sk-…"),
            _f("OPENAI_MODEL", "Model OpenAI", "text", "Nama model persis seperti di akun OpenAI Anda, mis. gpt-5.",
               default="gpt-5"),
            _f("GEMINI_API_KEY", "API key Gemini", "secret", "aistudio.google.com → Get API key."),
            _f("GEMINI_MODEL", "Model Gemini", "text", "Mis. gemini-2.5-pro.", default="gemini-2.5-pro"),
            _f("DEEPSEEK_API_KEY", "API key DeepSeek", "secret", "platform.deepseek.com → API keys."),
            _f("DEEPSEEK_MODEL", "Model DeepSeek", "text", "Mis. deepseek-chat.", default="deepseek-chat"),
            *[_f(f"AI_{key.upper()}", f"AI untuk {name}", "select", f"Tugas: {task}.", PROVIDER_OPTIONS,
               default="claude")
              for key, name, task in AGENT_ROLES],
            _f("AI_FALLBACK", "Urutan AI cadangan", "text",
               "Dicoba berurutan jika AI utama gagal, pisahkan koma, mis. claude,openai,gemini,deepseek. "
               "Hanya AI yang API key-nya terisi yang dipakai. Kosongkan jika tidak ingin cadangan.",
               default="claude,openai,gemini,deepseek"),
        ],
    },
    {
        "id": "telegram", "title": "💬 Telegram", "test": "telegram",
        "intro": "Buat bot di @BotFather dengan /newbot. Minimal bot Head of Marketing. Bot lain opsional: "
                 "agent tanpa bot akan bicara lewat bot Head of Marketing.",
        "fields": [
            *[_f(k, f"Token bot {l}", "secret", "Format: 123456789:ABC…") for k, l in BOT_TOKENS],
            _f("TELEGRAM_GROUP_ID", "ID grup", "text", "Angka negatif, mis. -1001234567890. Pakai tombol Deteksi.",
               detect="group"),
            _f("OWNER_TELEGRAM_IDS", "ID Owner (admin Telegram)", "text",
               "ID Telegram yang boleh memberi perintah & menekan tombol. Lebih dari satu: pisahkan koma.",
               detect="user"),
        ],
    },
    {
        "id": "data", "title": "📥 Sumber data", "test": "data",
        "intro": "Dari mana angka performa iklan diambil.",
        "fields": [
            _f("DATA_SOURCE", "Sumber data", "select", "", [
                {"value": "demo", "label": "Demo: angka contoh untuk uji coba"},
                {"value": "csv", "label": "CSV: unggah file export laporan BeMob"},
                {"value": "bemob", "label": "BeMob API: ambil otomatis dari BeMob"},
                {"value": "rollerads", "label": "RollerAds API: ambil otomatis dari RollerAds (tanpa campaign archived)"},
            ]),
            _f("ROLLERADS_API_KEY", "Token API RollerAds", "secret",
               "Diawali sk_api_. Minta ke account manager RollerAds. Dipakai juga untuk auto-pause & membuat "
               "campaign, apa pun sumber datanya."),
            _f("ROLLERADS_PAYOUT_USD", "Payout per konversi (USD)", "number",
               "Dipakai jika BeMob API belum diisi. Pendapatan = konversi x payout. 0 = tidak dihitung."),
            _f("BEMOB_ACCESS_KEY", "BeMob API: Access Key", "secret",
               "BeMob → Settings → Security → Create API Key (Read Only). Pendapatan & konversi asli diambil dari BeMob."),
            _f("BEMOB_SECRET_KEY", "BeMob API: Secret Key", "secret", "Hanya tampil sekali saat key dibuat."),
            _f("BEMOB_REPORT_URL", "URL laporan BeMob API", "text",
               "Dari dokumentasi API BeMob. Boleh memakai {date_from} dan {date_to}.", show_if="bemob"),
            _f("BEMOB_API_KEY", "API key BeMob", "secret", "Buat key Read Only di BeMob.", show_if="bemob"),
            _f("BEMOB_AUTH_HEADER", "Nama header auth BeMob", "text", "Biasanya Authorization.", show_if="bemob"),
            _f("BEMOB_ROWS_KEY", "Kunci JSON daftar baris", "text", "Mis. rows. Kosongkan jika respons berupa list.",
               show_if="bemob"),
            _f("COL_CAMPAIGN", "Nama kolom: campaign", show_if="csv,bemob"),
            _f("COL_ZONE", "Nama kolom: zone", show_if="csv,bemob"),
            _f("COL_VISITS", "Nama kolom: visits", show_if="csv,bemob"),
            _f("COL_CONVERSIONS", "Nama kolom: konversi", show_if="csv,bemob"),
            _f("COL_COST", "Nama kolom: biaya (cost)", show_if="csv,bemob"),
            _f("COL_REVENUE", "Nama kolom: pendapatan (revenue)", show_if="csv,bemob"),
        ],
    },
    {
        "id": "limits", "title": "🛡️ Batas pengaman", "test": None,
        "intro": "Usulan AI di luar batas ini otomatis ditolak sistem.",
        "fields": [
            _f("MAX_DAILY_SPEND_USD", "Batas spend harian (USD)", "number", "Lewat dari ini → alert darurat."),
            _f("MAX_BID_CHANGE_PCT", "Perubahan bid maksimal (%)", "number"),
            _f("MAX_CAMPAIGN_DAILY_BUDGET_USD", "Budget harian maks per campaign (USD)", "number"),
            _f("ZONE_WASTE_USD", "Zone boros jika spend ≥ (USD) tanpa konversi", "number"),
            _f("ZONE_MIN_ROI_PCT", "Zone rugi jika ROI < (%)", "number"),
            _f("ZONE_GOOD_ROI_PCT", "Zone bagus jika ROI ≥ (%)", "number"),
            _f("CAMPAIGN_MAX_BID_USD", "Bid maksimal campaign baru (USD)", "number",
               "Campaign baru dari panel atau agent dengan bid di atas ini ditolak."),
        ],
    },
    {
        "id": "autopause", "title": "⏸️ Auto-pause campaign", "test": None,
        "intro": "Campaign RollerAds yang aktif diperiksa otomatis (tanpa AI). Campaign di-pause jika spend hari ini "
                 "melewati budget harian per campaign atau batas spend harian akun, atau jika hasilnya jelek. "
                 "Campaign archived tidak pernah disentuh.",
        "fields": [
            _f("AUTOPAUSE_ENABLED", "Auto-pause", "select", "", [
                {"value": "1", "label": "Aktif"},
                {"value": "0", "label": "Mati"},
            ]),
            _f("AUTOPAUSE_MINUTES", "Cek tiap (menit)", "number"),
            _f("AUTOPAUSE_MIN_SPEND_USD", "Nilai hasil setelah spend ≥ (USD)", "number",
               "Di bawah ini campaign dianggap masih tes. Di atasnya, campaign tanpa konversi di-pause "
               "(butuh postback BeMob → RollerAds)."),
            _f("AUTOPAUSE_MAX_CPA_USD", "Pause jika CPA > (USD)", "number", "0 = tidak dipakai."),
            _f("AUTOPAUSE_MIN_ROI_PCT", "Pause jika ROI < (%)", "number",
               "Hanya dipakai jika payout per konversi RollerAds diisi."),
        ],
    },
    {
        "id": "pages", "title": "🛰️ Landing page & tracking", "test": None,
        "intro": "Alamat yang dicek otomatis. Satu alamat per baris.",
        "fields": [
            _f("LANDING_PAGE_URLS", "Landing page", "list", "Mis. https://domain-anda.com/"),
            _f("TRACKING_URLS", "Link tracking (BeMob)", "list", "Opsional."),
        ],
    },
    {
        "id": "tracking", "title": "🔌 Script tracking website", "test": None,
        "intro": "Setiap campaign baru, Developer membuat script tracking untuk website tujuannya dan QA memeriksa "
                 "serta mengetes koneksinya. AI yang dipakai keduanya diatur di bagian Penyedia AI.",
        "fields": [
            _f("BEMOB_POSTBACK_URL", "URL postback BeMob", "text",
               "BeMob → Settings → Tracking domain / Postback. Mis. https://xxxxx.bemobtrcks.com/postback"),
            _f("TRACK_REG_PAYOUT_USD", "Nilai per pendaftaran (USD)", "number", "0 = pendaftaran dicatat tanpa nilai.",
               default="0"),
            _f("TRACK_KURS_USD", "Kurs 1 USD dalam rupiah", "number", "Untuk mengubah nominal deposit ke USD.",
               default="16000"),
            _f("TRACK_DEPOSIT_SHARE", "Porsi deposit yang dihitung pendapatan", "number",
               "1 = seluruh deposit, 0.3 = 30% (mis. jika pendapatan Anda komisi 30%).", default="1"),
        ],
    },
    {
        "id": "schedule", "title": "⏰ Jadwal kerja", "test": None,
        "intro": "Jam memakai format 24 jam, mis. 16:00.",
        "fields": [
            _f("MEETING_TIMES", "Jam rapat rutin", "text", "Pisahkan koma, mis. 10:00,16:00,22:00. Tiap rapat memakai kredit AI."),
            _f("DAILY_REPORT_TIME", "Jam laporan harian", "time"),
            _f("QA_TIME", "Jam QA landing page", "time"),
            _f("HEALTHCHECK_MINUTES", "Cek landing page tiap (menit)", "number"),
            _f("ANALYST_MINUTES", "Laporan angka tiap (menit)", "number"),
            _f("MEETING_MIN_GAP_MINUTES", "Jeda minimal rapat darurat (menit)", "number"),
            _f("TASK_REMIND_HOURS", "Ingatkan tugas manual tiap (jam)", "number"),
            _f("DAILY_REMINDERS", "Pengingat harian", "reminders", "Satu per baris: JAM|pesan, mis. 09:00|Cek saldo RollerAds"),
        ],
    },
    {
        "id": "admin", "title": "🔐 Admin dashboard", "test": None,
        "intro": "Kunci dashboard dengan password, terutama jika dijalankan di VPS.",
        "fields": [
            _f("DASHBOARD_PASSWORD", "Password dashboard", "secret",
               "Password untuk halaman login dashboard. Minimal 10 karakter campuran; disimpan sebagai hash. "
               "Ganti password = perangkat lain otomatis keluar. 5x salah dari satu IP = diblokir 15 menit."),
            _f("DASHBOARD_HOST", "Siapa yang bisa membuka dashboard", "select", "", [
                {"value": "127.0.0.1", "label": "Hanya komputer ini (disarankan di laptop)"},
                {"value": "0.0.0.0", "label": "Semua perangkat di jaringan / internet (VPS). WAJIB password"},
            ]),
            _f("DASHBOARD_PORT", "Port dashboard", "number", "Default 8090. Jangan pakai port yang sudah dipakai program lain (Laragon memakai 80/8080)."),
            _f("TIMEZONE", "Zona waktu", "select", "", [
                {"value": "Asia/Jakarta", "label": "WIB (Asia/Jakarta)"},
                {"value": "Asia/Makassar", "label": "WITA (Asia/Makassar)"},
                {"value": "Asia/Jayapura", "label": "WIT (Asia/Jayapura)"},
            ]),
        ],
    },
]

FIELDS = {f["key"]: f for group in SCHEMA for f in group["fields"]}
_TIME = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")


class SettingsError(ValueError):
    pass


def ensure_env() -> None:
    if not ENV_PATH.exists() and EXAMPLE_PATH.exists():
        shutil.copy(EXAMPLE_PATH, ENV_PATH)


def current() -> dict[str, str]:
    ensure_env()
    values = dotenv_values(ENV_PATH) if ENV_PATH.exists() else {}
    return {k: (v or "").strip() for k, v in values.items()}


def is_placeholder(value: str) -> bool:
    return not value or "xxxx" in value


def mask(value: str) -> str:
    return "" if is_placeholder(value) else "…" + value[-4:]


def to_display(key: str, value: str) -> str:
    """Nilai .env → isi kotak di form."""
    kind = FIELDS[key]["type"]
    if kind == "list":
        return "\n".join(x.strip() for x in value.split(",") if x.strip())
    if kind == "reminders":
        return "\n".join(x.strip() for x in value.split(";") if x.strip())
    return value


def view() -> list[dict]:
    """Skema + nilai sekarang. Rahasia tidak pernah dikirim utuh ke browser."""
    values = current()
    out = []
    for group in SCHEMA:
        fields = []
        for f in group["fields"]:
            value = values.get(f["key"], f.get("default", ""))
            if f["type"] == "secret":
                fields.append({**f, "value": "", "is_set": not is_placeholder(value), "hint": mask(value)})
            else:
                fields.append({**f, "value": to_display(f["key"], value)})
        out.append({**group, "fields": fields})
    return out


def _normalize(key: str, raw) -> str:
    """Validasi satu nilai dari form → string untuk .env. Melempar SettingsError jika salah."""
    f = FIELDS[key]
    label = f["label"]
    value = str(raw if raw is not None else "").strip()
    kind = f["type"]
    if kind == "list":
        items = [x.strip() for x in re.split(r"[\n,]+", value) if x.strip()]
        for url in items:
            if not url.startswith(("http://", "https://")):
                raise SettingsError(f"{label}: '{url}' harus diawali http:// atau https://")
        return ",".join(items)
    if kind == "reminders":
        items = [x.strip() for x in re.split(r"[\n;]+", value) if x.strip()]
        for item in items:
            jam, _, pesan = item.partition("|")
            if not _TIME.match(jam.strip()) or not pesan.strip():
                raise SettingsError(f"{label}: '{item}' harus berformat JAM|pesan, mis. 09:00|Cek saldo")
        return ";".join(items)
    if kind == "time":
        if not _TIME.match(value):
            raise SettingsError(f"{label}: '{value}' bukan jam yang valid (contoh 08:00)")
        return value
    if kind == "number":
        try:
            float(value)
        except ValueError:
            raise SettingsError(f"{label}: harus berupa angka") from None
        if key.endswith(("_MINUTES", "_HOURS", "_PORT")) and float(value) <= 0:
            raise SettingsError(f"{label}: harus lebih dari 0")
        if key in ("HEALTHCHECK_MINUTES", "ANALYST_MINUTES", "MEETING_MIN_GAP_MINUTES", "DASHBOARD_PORT",
                   "AUTOPAUSE_MINUTES"):
            if not value.isdigit():
                raise SettingsError(f"{label}: harus bilangan bulat")
        return value
    if kind == "select":
        if value not in {o["value"] for o in f["options"]}:
            raise SettingsError(f"{label}: pilihan tidak dikenal")
        return value
    if key == "TELEGRAM_GROUP_ID" and value and not re.fullmatch(r"-?\d+", value):
        raise SettingsError("ID grup harus berupa angka, mis. -1001234567890")
    if key == "OWNER_TELEGRAM_IDS":
        ids = [x.strip() for x in value.split(",") if x.strip()]
        if any(not re.fullmatch(r"\d+", x) for x in ids):
            raise SettingsError("ID Owner harus angka, pisahkan dengan koma")
        return ",".join(ids)
    if key == "AI_FALLBACK":
        items = [x.strip().lower() for x in value.split(",") if x.strip()]
        unknown = [x for x in items if x not in PROVIDERS]
        if unknown:
            raise SettingsError(f"Urutan AI cadangan: '{', '.join(unknown)}' tidak dikenal. "
                                f"Pilihan: {', '.join(sorted(PROVIDERS))}")
        return ",".join(dict.fromkeys(items))
    if key == "BEMOB_POSTBACK_URL" and value:
        if not value.startswith("https://") or "/postback" not in value:
            raise SettingsError("URL postback BeMob harus seperti https://xxxxx.bemobtrcks.com/postback")
        return value.split("?")[0]
    if key == "MEETING_TIMES":
        times = [x.strip() for x in value.split(",") if x.strip()]
        if any(not _TIME.match(t) for t in times):
            raise SettingsError("Jam rapat harus berformat 10:00,16:00")
        return ",".join(times)
    if kind == "secret" and key.startswith("BOT_TOKEN_") and value and not re.fullmatch(r"\d+:[\w-]+", value):
        raise SettingsError(f"{label}: format token tidak valid (contoh 123456789:ABC-def…)")
    return value


def merged(updates: dict) -> dict[str, str]:
    """Nilai tersimpan + perubahan dari form. Rahasia kosong = tidak diubah, None = dihapus."""
    values = current()
    for key, raw in updates.items():
        if key not in FIELDS:
            continue
        if FIELDS[key]["type"] == "secret":
            if raw is None:
                values[key] = ""
            elif str(raw).strip():
                values[key] = _normalize(key, raw)
        elif str(raw if raw is not None else "").strip() != to_display(
                key, values.get(key, FIELDS[key].get("default", ""))):
            values[key] = _normalize(key, raw)
    return values


def _quote(value: str) -> str:
    if value and (value != value.strip() or " #" in value or value[0] in "'\"#"):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return value


def save(updates: dict) -> list[str]:
    """Simpan ke .env dengan mempertahankan komentar & urutan. Mengembalikan daftar kunci yang berubah."""
    before = current()
    values = merged(updates)
    password = values.get("DASHBOARD_PASSWORD") or ""
    if before.get("DASHBOARD_PASSWORD") and not password:
        raise SettingsError("Password dashboard tidak bisa dihapus, hanya bisa diganti.")
    if password and password != before.get("DASHBOARD_PASSWORD") and not auth.is_hashed(password):
        problem = auth.weakness(password)
        if problem:
            raise SettingsError(f"Password dashboard: {problem}")
        values["DASHBOARD_PASSWORD"] = auth.hash_password(password)  # tidak pernah disimpan sebagai teks asli
    changed = [k for k in FIELDS if values.get(k, "") != before.get(k, "")]
    if not changed:
        return []

    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    done = set()
    for i, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if "=" in line and not line.lstrip().startswith("#") and key in changed and key not in done:
            lines[i] = f"{key}={_quote(values[key])}"
            done.add(key)
    for key in changed:
        if key not in done:
            lines.append(f"{key}={_quote(values[key])}")
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return changed
