"""Login dashboard: password disimpan sebagai hash, sesi login lewat cookie yang bisa di-logout.

- Password di .env disimpan sebagai hash PBKDF2 (bukan teks asli). Password lama berupa teks biasa tetap bisa
  dipakai login dan otomatis diubah menjadi hash.
- Sesi: token acak di cookie HttpOnly + SameSite=Strict. Di server hanya disimpan hash token-nya, jadi isi
  database yang bocor tidak bisa dipakai login. Sesi berakhir jika 24 jam tidak dipakai atau sudah 7 hari.
- Ganti password = semua sesi lain otomatis keluar.
"""
import hashlib
import hmac
import secrets
import time

import config
import storage

COOKIE = "aiads_session"
SESSIONS_KEY = "dashboard_sessions"
IDLE_SECONDS = 24 * 3600
MAX_AGE_SECONDS = 7 * 24 * 3600
ITERATIONS = 390_000
PREFIX = "pbkdf2_sha256:"
MIN_LENGTH = 10
WEAK = {"password123", "admin12345", "1234567890", "qwertyuiop", "passwordku1", "rahasia123"}


# ---------------------------------------------------------------- password

def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), ITERATIONS).hex()
    return f"{PREFIX}{ITERATIONS}:{salt}:{digest}"


def is_hashed(value: str) -> bool:
    return (value or "").startswith(PREFIX)


def verify_password(password: str, stored: str | None = None) -> bool:
    stored = config.DASHBOARD_PASSWORD if stored is None else stored
    if not stored or not password:
        return False
    if not is_hashed(stored):  # format lama (teks biasa)
        return hmac.compare_digest(password.encode(), stored.encode())
    try:
        iterations, salt, digest = stored[len(PREFIX):].split(":")
        check = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(iterations)).hex()
    except ValueError:
        return False
    return hmac.compare_digest(check, digest)


def weakness(password: str) -> str | None:
    """Alasan password ditolak, atau None jika cukup kuat."""
    if len(password) < MIN_LENGTH:
        return f"Password minimal {MIN_LENGTH} karakter."
    if password.isdigit() or (password.isalpha() and len(password) < 16) or password.lower() in WEAK \
            or len(set(password)) < 5:
        return "Password terlalu mudah ditebak. Campur huruf, angka, dan simbol (mis. 3 kata acak + angka)."
    return None


def enabled() -> bool:
    return bool(config.DASHBOARD_PASSWORD)


# ---------------------------------------------------------------- sesi

def _key(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _sessions() -> dict[str, dict]:
    data = storage.get(SESSIONS_KEY, {})
    now = time.time()
    return {k: s for k, s in (data if isinstance(data, dict) else {}).items()
            if now - s["last"] < IDLE_SECONDS and now - s["created"] < MAX_AGE_SECONDS}


def create_session(ip: str, agent: str) -> str:
    token = secrets.token_urlsafe(32)
    data = _sessions()
    data[_key(token)] = {"created": time.time(), "last": time.time(), "ip": ip, "agent": agent[:160]}
    storage.put(SESSIONS_KEY, data)
    return token


def check_session(token: str | None) -> bool:
    """Sesi masih berlaku? Sekaligus memperpanjang waktu idle (dicatat maks tiap 5 menit)."""
    if not token:
        return False
    data = _sessions()
    session = data.get(_key(token))
    if not session:
        return False
    if time.time() - session["last"] > 300:
        session["last"] = time.time()
        storage.put(SESSIONS_KEY, data)
    return True


def end_session(token: str | None) -> None:
    data = _sessions()
    if token and data.pop(_key(token), None) is not None:
        storage.put(SESSIONS_KEY, data)


def end_all_sessions(keep: str | None = None) -> int:
    """Keluarkan semua perangkat (kecuali sesi `keep`). Mengembalikan jumlah sesi yang diakhiri."""
    data = _sessions()
    kept = {k: s for k, s in data.items() if keep and k == _key(keep)}
    storage.put(SESSIONS_KEY, kept)
    return len(data) - len(kept)


def session_list(current: str | None) -> list[dict]:
    mine = _key(current) if current else ""
    return sorted(({"created": s["created"], "last": s["last"], "ip": s["ip"], "agent": s["agent"],
                    "current": k == mine} for k, s in _sessions().items()), key=lambda s: -s["last"])
