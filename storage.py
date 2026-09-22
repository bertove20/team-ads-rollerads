"""Database SQLite kecil: topic, usulan, tugas manual, log obrolan, dan biaya AI."""
import json
import sqlite3
import time

from config import DB_PATH

_db = sqlite3.connect(DB_PATH, check_same_thread=False)
_db.row_factory = sqlite3.Row
_db.executescript(
    """
    CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE IF NOT EXISTS proposals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created REAL, action TEXT, status TEXT DEFAULT 'pending',
        decided_by INTEGER, decided_at REAL
    );
    CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created REAL, text TEXT, status TEXT DEFAULT 'open',
        proposal_id INTEGER, last_reminded REAL
    );
    CREATE TABLE IF NOT EXISTS chat_log (
        ts REAL, thread_id INTEGER, speaker TEXT, text TEXT
    );
    CREATE TABLE IF NOT EXISTS usage (
        ts REAL, agent TEXT, model TEXT,
        input_tokens INTEGER, output_tokens INTEGER,
        cache_read INTEGER, cache_write INTEGER
    );
    CREATE TABLE IF NOT EXISTS snapshots (
        ts REAL, day TEXT, total TEXT, campaigns TEXT
    );
    -- Catatan audit: siapa melakukan apa di dashboard (login, ubah pengaturan, setujui usulan, dll.)
    CREATE TABLE IF NOT EXISTS audit (
        ts REAL, who TEXT, role TEXT, ip TEXT, action TEXT, detail TEXT
    );
    -- Kejadian pemain dari script di website (pendaftaran & deposit), untuk menghitung nilai pemain / LTV.
    -- txid unik supaya kejadian yang sama tidak dihitung dua kali walau script mengirim ulang.
    CREATE TABLE IF NOT EXISTS player_events (
        ts REAL, click_id TEXT, event TEXT, value REAL,
        txid TEXT PRIMARY KEY, site TEXT, campaign TEXT, zone TEXT
    );
    CREATE INDEX IF NOT EXISTS player_events_click ON player_events (click_id);
    CREATE INDEX IF NOT EXISTS player_events_ts ON player_events (ts);
    """
)
# Kolom `topic` & `task` ditambahkan belakangan; database lama di-upgrade otomatis.
if "topic" not in {r["name"] for r in _db.execute("PRAGMA table_info(chat_log)")}:
    _db.execute("ALTER TABLE chat_log ADD COLUMN topic TEXT")
if "task" not in {r["name"] for r in _db.execute("PRAGMA table_info(usage)")}:
    _db.execute("ALTER TABLE usage ADD COLUMN task TEXT")  # jenis pekerjaan, mis. rapat / script tracking
_db.commit()


# --- key/value ---
def get(key: str, default=None):
    row = _db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def put(key: str, value) -> None:
    _db.execute(
        "INSERT INTO kv (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, json.dumps(value)),
    )
    _db.commit()


# --- usulan (proposal) ---
def add_proposal(action: dict) -> int:
    cur = _db.execute(
        "INSERT INTO proposals (created, action) VALUES (?, ?)",
        (time.time(), json.dumps(action)),
    )
    _db.commit()
    return cur.lastrowid


def get_proposal(proposal_id: int):
    row = _db.execute("SELECT * FROM proposals WHERE id = ?", (proposal_id,)).fetchone()
    if not row:
        return None
    return {**dict(row), "action": json.loads(row["action"])}


def decide_proposal(proposal_id: int, status: str, user_id: int) -> bool:
    """Mengembalikan False jika usulan sudah diputuskan sebelumnya."""
    cur = _db.execute(
        "UPDATE proposals SET status = ?, decided_by = ?, decided_at = ? "
        "WHERE id = ? AND status = 'pending'",
        (status, user_id, time.time(), proposal_id),
    )
    _db.commit()
    return cur.rowcount == 1


def list_proposals(limit: int = 100) -> list[dict]:
    return [
        {**dict(r), "action": json.loads(r["action"])}
        for r in _db.execute("SELECT * FROM proposals ORDER BY id DESC LIMIT ?", (limit,))
    ]


def recently_proposed_zones(hours: float = 24) -> set[str]:
    since = time.time() - hours * 3600
    zones: set[str] = set()
    for row in _db.execute(
        "SELECT action FROM proposals WHERE created >= ? AND status != 'rejected'", (since,)
    ):
        zones.update(json.loads(row["action"]).get("zones", []))
    return zones


# --- tugas manual ---
def add_task(text: str, proposal_id: int | None = None) -> int:
    cur = _db.execute(
        "INSERT INTO tasks (created, text, proposal_id, last_reminded) VALUES (?, ?, ?, ?)",
        (time.time(), text, proposal_id, time.time()),
    )
    _db.commit()
    return cur.lastrowid


def open_tasks() -> list[dict]:
    return [dict(r) for r in _db.execute("SELECT * FROM tasks WHERE status = 'open' ORDER BY id")]


def list_tasks(limit: int = 100) -> list[dict]:
    return [dict(r) for r in _db.execute("SELECT * FROM tasks ORDER BY id DESC LIMIT ?", (limit,))]


def finish_task(task_id: int) -> bool:
    cur = _db.execute(
        "UPDATE tasks SET status = 'done' WHERE id = ? AND status = 'open'", (task_id,)
    )
    _db.commit()
    return cur.rowcount == 1


def mark_task_reminded(task_id: int) -> None:
    _db.execute("UPDATE tasks SET last_reminded = ? WHERE id = ?", (time.time(), task_id))
    _db.commit()


# --- log obrolan (konteks untuk agent saat menjawab manusia) ---
def log_chat(thread_id: int | None, speaker: str, text: str, topic: str | None = None) -> None:
    _db.execute(
        "INSERT INTO chat_log (ts, thread_id, speaker, text, topic) VALUES (?, ?, ?, ?, ?)",
        (time.time(), thread_id or 0, speaker, text, topic),
    )
    _db.commit()


def chat_feed(topic: str | None = None, limit: int = 100) -> list[dict]:
    """Pesan terbaru untuk dashboard, terbaru di atas."""
    if topic:
        rows = _db.execute(
            "SELECT ts, topic, speaker, text FROM chat_log WHERE topic = ? ORDER BY ts DESC LIMIT ?",
            (topic, limit),
        )
    else:
        rows = _db.execute(
            "SELECT ts, topic, speaker, text FROM chat_log ORDER BY ts DESC LIMIT ?", (limit,)
        )
    return [dict(r) for r in rows]


def recent_chat(thread_id: int | None, limit: int = 20) -> list[dict]:
    rows = _db.execute(
        "SELECT speaker, text FROM chat_log WHERE thread_id = ? ORDER BY ts DESC LIMIT ?",
        (thread_id or 0, limit),
    ).fetchall()
    return [dict(r) for r in reversed(rows)]


# --- biaya AI ---
def log_usage(agent: str, model: str, usage, task: str = "") -> None:
    _db.execute(
        "INSERT INTO usage (ts, agent, model, input_tokens, output_tokens, cache_read, cache_write, task) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            time.time(),
            agent,
            model,
            usage.input_tokens or 0,
            usage.output_tokens or 0,
            getattr(usage, "cache_read_input_tokens", 0) or 0,
            getattr(usage, "cache_creation_input_tokens", 0) or 0,
            task or "lainnya",
        ),
    )
    _db.commit()


# --- catatan audit ---
def add_audit(who: str, role: str, ip: str, action: str, detail: str = "") -> None:
    _db.execute("INSERT INTO audit VALUES (?, ?, ?, ?, ?, ?)", (time.time(), who, role, ip, action, detail[:500]))
    _db.commit()


def list_audit(limit: int = 200) -> list[dict]:
    return [dict(r) for r in _db.execute("SELECT * FROM audit ORDER BY ts DESC LIMIT ?", (limit,))]


# --- kejadian pemain (untuk nilai pemain / LTV) ---
def add_player_event(click_id: str, event: str, value: float, txid: str, site: str = "",
                     campaign: str = "", zone: str = "") -> bool:
    """False jika txid sudah pernah tercatat (kiriman dobel)."""
    cur = _db.execute(
        "INSERT OR IGNORE INTO player_events (ts, click_id, event, value, txid, site, campaign, zone) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (time.time(), click_id, event, value, txid, site, campaign, zone),
    )
    _db.commit()
    return cur.rowcount == 1


def player_events_since(since_ts: float) -> list[dict]:
    return [dict(r) for r in _db.execute(
        "SELECT * FROM player_events WHERE ts >= ? ORDER BY ts", (since_ts,))]


def player_event_count() -> int:
    return _db.execute("SELECT COUNT(*) AS c FROM player_events").fetchone()["c"]


def usage_rows_since(since_ts: float) -> list[dict]:
    return [dict(r) for r in _db.execute("SELECT * FROM usage WHERE ts >= ? ORDER BY ts", (since_ts,))]


# --- riwayat performa (untuk grafik tren) ---
def save_snapshot(day: str, total: dict, campaigns: dict) -> None:
    _db.execute(
        "INSERT INTO snapshots VALUES (?, ?, ?, ?)",
        (time.time(), day, json.dumps(total), json.dumps(campaigns)),
    )
    _db.commit()


def snapshots_of_day(day: str) -> list[dict]:
    return [
        {"ts": r["ts"], "total": json.loads(r["total"])}
        for r in _db.execute("SELECT ts, total FROM snapshots WHERE day = ? ORDER BY ts", (day,))
    ]


def daily_snapshots(days: int = 30) -> list[dict]:
    """Snapshot terakhir tiap hari (angka hari itu bersifat kumulatif)."""
    rows = _db.execute(
        "SELECT s.day, s.total, s.campaigns FROM snapshots s "
        "JOIN (SELECT day, MAX(ts) AS ts FROM snapshots GROUP BY day) last "
        "ON s.day = last.day AND s.ts = last.ts ORDER BY s.day DESC LIMIT ?",
        (days,),
    ).fetchall()
    return [
        {"day": r["day"], "total": json.loads(r["total"]), "campaigns": json.loads(r["campaigns"])}
        for r in reversed(rows)
    ]


def usage_since(since_ts: float) -> list[dict]:
    return [
        dict(r)
        for r in _db.execute(
            "SELECT model, SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens, "
            "SUM(cache_read) AS cache_read, SUM(cache_write) AS cache_write "
            "FROM usage WHERE ts >= ? GROUP BY model",
            (since_ts,),
        )
    ]
