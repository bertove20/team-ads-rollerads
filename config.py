"""Semua pengaturan dibaca dari file .env."""
import datetime as dt
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _int(name: str, default: int) -> int:
    value = _str(name)
    return int(value) if value else default


def _float(name: str, default: float) -> float:
    value = _str(name)
    return float(value) if value else default


def _list(name: str, sep: str = ",") -> list[str]:
    return [item.strip() for item in _str(name).split(sep) if item.strip()]


def _time(value: str) -> dt.time:
    hour, minute = value.split(":")
    return dt.time(int(hour), int(minute), tzinfo=TIMEZONE)


TIMEZONE = ZoneInfo(_str("TIMEZONE", "Asia/Jakarta"))
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "team.db"

# Claude
CLAUDE_MODEL = _str("CLAUDE_MODEL", "claude-opus-5")

# Penyedia AI lain (opsional). Tiap agent memilih AI-nya sendiri lewat AI_<KEY AGENT>, mis. AI_QA=openai.
OPENAI_API_KEY = _str("OPENAI_API_KEY")
OPENAI_MODEL = _str("OPENAI_MODEL", "gpt-5")
GEMINI_API_KEY = _str("GEMINI_API_KEY")
GEMINI_MODEL = _str("GEMINI_MODEL", "gemini-2.5-pro")
DEEPSEEK_API_KEY = _str("DEEPSEEK_API_KEY")
DEEPSEEK_MODEL = _str("DEEPSEEK_MODEL", "deepseek-chat")
# OpenRouter: satu API key untuk banyak model (Claude, GPT, Gemini, DeepSeek, ...). Model bisa dipilih per agent
# lewat MODEL_<KEY AGENT>, mis. MODEL_QA=openai/gpt-5.6-terra. Jika model itu gagal, OpenRouter otomatis mencoba
# OPENROUTER_FALLBACK_MODELS berurutan.
OPENROUTER_API_KEY = _str("OPENROUTER_API_KEY")
OPENROUTER_MODEL = _str("OPENROUTER_MODEL", "anthropic/claude-sonnet-5")
OPENROUTER_FALLBACK_MODELS = _list("OPENROUTER_FALLBACK_MODELS") if os.getenv("OPENROUTER_FALLBACK_MODELS") is not None \
    else ["anthropic/claude-sonnet-5", "openai/gpt-5.6-sol", "google/gemini-3.5-flash"]
# Urutan AI cadangan jika AI utama agent gagal (kosong = tanpa cadangan)
AI_FALLBACK = [p.lower() for p in _list("AI_FALLBACK")] if os.getenv("AI_FALLBACK") is not None \
    else ["claude", "openrouter", "openai", "gemini", "deepseek"]


def agent_provider(agent_key: str) -> str:
    """AI yang dipakai agent, mis. AI_DEVELOPER=claude. Default Claude."""
    return _str(f"AI_{agent_key.upper()}", "claude").lower()


def agent_model(agent_key: str) -> str:
    """Model OpenRouter khusus agent (MODEL_<KEY>), kosong = OPENROUTER_MODEL."""
    return _str(f"MODEL_{agent_key.upper()}")

# Telegram
GROUP_ID = _int("TELEGRAM_GROUP_ID", 0)
OWNER_IDS = {int(x) for x in _list("OWNER_TELEGRAM_IDS")}

# Sumber data
DATA_SOURCE = _str("DATA_SOURCE", "demo").lower()
COLUMNS = {
    "campaign": _str("COL_CAMPAIGN", "Campaign"),
    "zone": _str("COL_ZONE", "Zone"),
    "visits": _str("COL_VISITS", "Visits"),
    "conversions": _str("COL_CONVERSIONS", "Conversions"),
    "cost": _str("COL_COST", "Cost"),
    "revenue": _str("COL_REVENUE", "Revenue"),
}
BEMOB_REPORT_URL = _str("BEMOB_REPORT_URL")
BEMOB_AUTH_HEADER = _str("BEMOB_AUTH_HEADER", "Authorization")
BEMOB_API_KEY = _str("BEMOB_API_KEY")
BEMOB_ROWS_KEY = _str("BEMOB_ROWS_KEY", "rows")
ROLLERADS_API_KEY = _str("ROLLERADS_API_KEY")
# BeMob REST API (Settings -> Security): pendapatan & konversi asli per campaign/zone RollerAds
BEMOB_ACCESS_KEY = _str("BEMOB_ACCESS_KEY")
BEMOB_SECRET_KEY = _str("BEMOB_SECRET_KEY")
# RollerAds tidak tahu pendapatan Anda: pendapatan = konversi x payout ini (0 = tidak dihitung)
ROLLERADS_PAYOUT_USD = _float("ROLLERADS_PAYOUT_USD", 0)
# Bid maksimal untuk campaign baru (dibuat dari panel atau agent)
CAMPAIGN_MAX_BID_USD = _float("CAMPAIGN_MAX_BID_USD", 5)

# Auto-pause campaign RollerAds (tanpa AI, gratis)
AUTOPAUSE_ENABLED = _str("AUTOPAUSE_ENABLED", "1") == "1"
AUTOPAUSE_MINUTES = _int("AUTOPAUSE_MINUTES", 15)
AUTOPAUSE_MIN_SPEND_USD = _float("AUTOPAUSE_MIN_SPEND_USD", 10)
AUTOPAUSE_MAX_CPA_USD = _float("AUTOPAUSE_MAX_CPA_USD", 0)
AUTOPAUSE_MIN_ROI_PCT = _float("AUTOPAUSE_MIN_ROI_PCT", -50)

# Guardrails
MAX_DAILY_SPEND_USD = _float("MAX_DAILY_SPEND_USD", 100)
MAX_BID_CHANGE_PCT = _float("MAX_BID_CHANGE_PCT", 25)
MAX_CAMPAIGN_DAILY_BUDGET_USD = _float("MAX_CAMPAIGN_DAILY_BUDGET_USD", 50)
ZONE_WASTE_USD = _float("ZONE_WASTE_USD", 3)
ZONE_MIN_ROI_PCT = _float("ZONE_MIN_ROI_PCT", -50)
ZONE_GOOD_ROI_PCT = _float("ZONE_GOOD_ROI_PCT", 30)

# Script tracking website (dibuat Developer, dicek QA saat campaign dibuat)
BEMOB_POSTBACK_URL = _str("BEMOB_POSTBACK_URL")  # mis. https://xxxxx.bemobtrcks.com/postback
TRACK_REG_PAYOUT_USD = _float("TRACK_REG_PAYOUT_USD", 0)
TRACK_KURS_USD = _float("TRACK_KURS_USD", 16000)
TRACK_DEPOSIT_SHARE = _float("TRACK_DEPOSIT_SHARE", 1)

# Pemeriksaan
LANDING_PAGE_URLS = _list("LANDING_PAGE_URLS")
TRACKING_URLS = _list("TRACKING_URLS")

# Jadwal
HEALTHCHECK_MINUTES = _int("HEALTHCHECK_MINUTES", 15)
ANALYST_MINUTES = _int("ANALYST_MINUTES", 60)
MEETING_TIMES = [_time(t) for t in _list("MEETING_TIMES")]
MEETING_MIN_GAP_MINUTES = _int("MEETING_MIN_GAP_MINUTES", 120)
DAILY_REPORT_TIME = _time(_str("DAILY_REPORT_TIME", "08:00"))
QA_TIME = _time(_str("QA_TIME", "07:00"))
DAILY_REMINDERS = [
    (_time(item.split("|", 1)[0].strip()), item.split("|", 1)[1].strip())
    for item in _list("DAILY_REMINDERS", sep=";")
    if "|" in item
]
TASK_REMIND_HOURS = _float("TASK_REMIND_HOURS", 3)

# Dashboard web
DASHBOARD_HOST = _str("DASHBOARD_HOST", "127.0.0.1")
DASHBOARD_PORT = _int("DASHBOARD_PORT", 8090)  # 8080 sering dipakai Laragon/nginx
DASHBOARD_PASSWORD = _str("DASHBOARD_PASSWORD")
# Domain publik dashboard di VPS (mis. ads.domain-anda.com), dilayani HTTPS oleh Caddy di depan program.
DASHBOARD_DOMAIN = _str("DASHBOARD_DOMAIN").lower().removeprefix("https://").removeprefix("http://").strip("/")

# Topic di grup Telegram: key -> nama yang tampil
TOPICS = {
    "diskusi": "💬 Diskusi Strategi",
    "approval": "🎯 Campaign & Approval",
    "laporan": "📊 Laporan",
    "alert": "🚨 Alert Tracking",
    "kreatif": "🎨 Creative & LP",
    "qa": "✅ QA",
    "reminder": "⏰ Reminder",
}
