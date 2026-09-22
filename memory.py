"""Ingatan jangka panjang tim AI, supaya agent tidak mulai dari nol di setiap rapat/obrolan.

Isi ingatan:
- Arahan Owner: disimpan permanen (tidak pernah dihapus otomatis). Dari "ingat: ..." di Telegram, dashboard,
  atau terdeteksi saat merangkum obrolan.
- Keputusan, pelajaran, rencana, fakta: dirangkum AI setelah setiap rapat dan tiap hari dari obrolan 24 jam.
- Keputusan Owner atas usulan (disetujui/ditolak) dan dampak tindakan (angka campaign sebelum vs sesudah),
  dihitung dari data tanpa AI.
"""
import datetime as dt
import logging
import time

import autopause
import config
import llm
import storage
from agents import AGENTS, LEADER

log = logging.getLogger(__name__)

KEY = "team_memory"
MAX_NOTES = 40          # catatan hasil rangkuman (arahan Owner tidak dihitung)
KINDS = {"arahan": "Arahan Owner", "keputusan": "Keputusan", "pelajaran": "Pelajaran",
         "rencana": "Rencana/tes berjalan", "fakta": "Fakta penting"}
IMPACT_ACTIONS = {"paused": "pause", "active": "aktifkan", "created": "dibuat", "blacklist": "blacklist zone",
                  "bid": "ubah bid", "budget": "ubah budget"}

SCHEMA = {
    "type": "object",
    "properties": {
        "notes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["keputusan", "pelajaran", "rencana", "fakta"]},
                    "text": {"type": "string"},
                    "campaign": {"type": "string"},
                },
                "required": ["kind", "text", "campaign"],
                "additionalProperties": False,
            },
        },
        "owner_instructions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["notes", "owner_instructions"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------- data

def notes() -> list[dict]:
    data = storage.get(KEY, [])
    return data if isinstance(data, list) else []


def _save(items: list[dict]) -> None:
    storage.put(KEY, items)


def _day(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, config.TIMEZONE).strftime("%d-%m")


def remember(text: str, kind: str = "arahan", source: str = "Owner", campaign: str = "") -> dict | None:
    """Tambah catatan. Arahan Owner selalu disimpan permanen. None jika teksnya sudah ada."""
    text = " ".join((text or "").split())[:500]
    items = notes()
    if not text or any(n["text"].lower() == text.lower() for n in items):
        return None
    note = {"id": max([n["id"] for n in items] or [0]) + 1, "ts": time.time(), "kind": kind, "text": text,
            "campaign": campaign, "source": source, "pinned": kind == "arahan"}
    _save(items + [note])
    return note


def forget(note_id: int) -> bool:
    items = notes()
    kept = [n for n in items if n["id"] != note_id]
    _save(kept)
    return len(kept) != len(items)


# ---------------------------------------------------------------- dirangkum untuk prompt

def notes_text(limit_chars: int = 5000, ids: bool = False) -> str:
    items = notes()
    pinned = [n for n in items if n.get("pinned")]
    other = sorted([n for n in items if not n.get("pinned")], key=lambda n: -n["ts"])
    tag = (lambda n: f"#{n['id']} ") if ids else (lambda n: "")
    lines = []
    if pinned:
        lines.append("ARAHAN OWNER (wajib dipatuhi, berlaku sampai Owner mengubahnya):")
        lines += [f"- {tag(n)}[{_day(n['ts'])}] {n['text']}" for n in pinned]
    if other:
        lines.append("CATATAN TIM DARI RAPAT & OBROLAN SEBELUMNYA (terbaru di atas):")
        lines += [f"- {tag(n)}[{_day(n['ts'])}] {KINDS.get(n['kind'], n['kind'])}"
                  + (f" ({n['campaign']})" if n.get("campaign") else "") + f": {n['text']}" for n in other]
    text = "\n".join(lines)
    return text[:limit_chars] + (" …" if len(text) > limit_chars else "")


def decisions_text(limit: int = 12) -> str:
    """Usulan yang sudah diputuskan Owner: menunjukkan apa yang Owner sukai/tolak."""
    decided = [p for p in storage.list_proposals(60) if p["status"] in ("approved", "rejected")][:limit]
    if not decided:
        return ""
    import meeting  # di sini supaya tidak saling impor saat program dimulai
    return "KEPUTUSAN OWNER ATAS USULAN TERAKHIR (pelajari polanya, jangan ulangi usulan yang ditolak tanpa alasan baru):\n" \
        + "\n".join(f"- #{p['id']} {'DISETUJUI' if p['status'] == 'approved' else 'DITOLAK'}: "
                    f"{meeting.describe_action(p['action']).splitlines()[0]}" for p in decided)


def _metrics(days: list[dict], title: str, dates: list[str]) -> dict | None:
    rows = [d["campaigns"].get(title) for d in days if d["day"] in dates and d["campaigns"].get(title)]
    if not rows:
        return None
    cost = sum(r.get("cost", 0) for r in rows)
    conv = sum(r.get("conversions", 0) for r in rows)
    revenue = sum(r.get("revenue", 0) for r in rows)
    return {"days": len(rows), "cost": cost / len(rows), "conv": conv / len(rows),
            "roi": (revenue - cost) / cost * 100 if cost else None, "cpa": cost / conv if conv else None}


def _fmt(m: dict) -> str:
    roi = f"{m['roi']:.0f}%" if m["roi"] is not None else "-"
    cpa = f"${m['cpa']:.2f}" if m["cpa"] is not None else "-"
    return f"spend ${m['cost']:.2f}/hari, konv {m['conv']:.1f}/hari, ROI {roi}, CPA {cpa}"


def impact_text(max_age_days: int = 14) -> str:
    """Dampak tindakan: rata-rata harian 3 hari sebelum vs sesudah (hari tindakan tidak dihitung)."""
    days = storage.daily_snapshots(max_age_days + 4)
    today = dt.datetime.now(config.TIMEZONE).date()
    lines = []
    for e in storage.get(autopause.LOG_KEY, [])[-40:]:
        if e.get("action") not in IMPACT_ACTIONS or time.time() - e.get("ts", 0) > max_age_days * 86400:
            continue
        day = dt.datetime.fromtimestamp(e["ts"], config.TIMEZONE).date()
        before = _metrics(days, e.get("title", ""), [(day - dt.timedelta(days=i)).isoformat() for i in (1, 2, 3)])
        after_dates = [(day + dt.timedelta(days=i)) for i in (1, 2, 3)]
        after = _metrics(days, e.get("title", ""), [d.isoformat() for d in after_dates if d < today])
        what = f"{e.get('title')}: {IMPACT_ACTIONS[e['action']]} {e.get('reason') or ''} pada {day:%d-%m}".strip()
        if not after:
            lines.append(f"- {what}. Dampak belum terlihat (belum ada hari penuh sesudahnya).")
        else:
            lines.append(f"- {what}. Sebelum: {_fmt(before) if before else 'tidak ada data'}. "
                         f"Sesudah ({after['days']} hari): {_fmt(after)}.")
    return ("DAMPAK TINDAKAN SEBELUMNYA (pakai untuk menilai apa yang berhasil; ingat faktor lain juga berpengaruh):\n"
            + "\n".join(lines[-15:])) if lines else ""


def context() -> str:
    """Semua ingatan untuk disisipkan ke prompt agent."""
    return "\n\n".join(filter(None, [notes_text(), decisions_text(), impact_text()]))


# ---------------------------------------------------------------- merangkum otomatis

async def consolidate(transcript: str, source: str) -> int:
    """Perbarui catatan tim dari transkrip rapat/obrolan. Mengembalikan jumlah catatan sesudahnya."""
    if not transcript.strip():
        return len(notes())
    items = notes()
    current = "\n".join(f"- {n['kind']}" + (f" ({n['campaign']})" if n.get("campaign") else "") + f": {n['text']}"
                        for n in items if not n.get("pinned")) or "(kosong)"
    pinned = "\n".join(f"- {n['text']}" for n in items if n.get("pinned")) or "(kosong)"
    result = await llm.ask(
        AGENTS[LEADER],
        f"Anda merawat buku catatan tim supaya tim tidak lupa di rapat/obrolan berikutnya.\n\n"
        f"ARAHAN OWNER YANG SUDAH TERSIMPAN (jangan diulang):\n{pinned}\n\n"
        f"CATATAN TIM SAAT INI:\n{current}\n\nSUMBER BARU ({source}):\n{transcript[-15000:]}\n\n"
        f"Tulis ulang CATATAN TIM lengkap (maks {MAX_NOTES}): pertahankan yang masih berlaku, gabungkan yang mirip, "
        "perbarui yang berubah, hapus yang sudah basi/selesai, tambah yang baru dari sumber. Isi hanya hal yang "
        "berguna nanti: keputusan & alasannya, pelajaran (apa yang berhasil/gagal dengan angka), rencana/tes yang "
        "sedang berjalan & kapan dievaluasi, fakta penting (mis. website, payout, kendala). Satu catatan = 1-2 "
        "kalimat padat, sebut nama campaign di field campaign jika ada (kosongkan jika umum). Jangan mengarang.\n"
        "owner_instructions: HANYA arahan/preferensi baru yang diucapkan Owner sendiri dan berlaku ke depan "
        "(mis. 'jangan naikkan bid di atas $2', 'fokus Indonesia'). Kosongkan jika tidak ada.",
        schema=SCHEMA,
    )
    storage.put(KEY + "_prev", items)  # cadangan jika rangkuman AI keliru
    kept = [n for n in items if n.get("pinned")]
    next_id = max([n["id"] for n in items] or [0])
    old = {n["text"].lower(): n for n in items}
    for note in (result.get("notes") or [])[:MAX_NOTES]:
        text = " ".join(note["text"].split())[:500]
        if not text:
            continue
        same = old.get(text.lower())
        if same and not same.get("pinned"):
            kept.append(same)  # tidak berubah: tanggal aslinya dipertahankan
            continue
        next_id += 1
        kept.append({"id": next_id, "ts": time.time(), "kind": note["kind"], "text": text,
                     "campaign": note.get("campaign", ""), "source": source, "pinned": False})
    for text in result.get("owner_instructions") or []:
        text = " ".join(text.split())[:500]
        if text and all(n["text"].lower() != text.lower() for n in kept):
            next_id += 1
            kept.append({"id": next_id, "ts": time.time(), "kind": "arahan", "text": text, "campaign": "",
                         "source": f"terdeteksi dari {source}", "pinned": True})
    _save(kept)
    log.info("Ingatan tim diperbarui dari %s: %d catatan", source, len(kept))
    return len(kept)


def recent_chat_text(hours: float = 24) -> str:
    """Obrolan semua topic sejak rangkuman terakhir (maks `hours` jam), urut waktu."""
    since = max(time.time() - hours * 3600, storage.get(KEY + "_chat_ts", 0))
    rows = [r for r in storage.chat_feed(limit=400) if r["ts"] > since]
    return "\n".join(f"[{r.get('topic') or '-'}] {r['speaker']}: {r['text'][:1200]}" for r in reversed(rows))


async def consolidate_chat() -> None:
    """Dipanggil tiap hari: rangkum obrolan & laporan 24 jam terakhir ke catatan tim."""
    text = recent_chat_text()
    started = time.time()
    if text:
        await consolidate(text, "obrolan 24 jam")
    storage.put(KEY + "_chat_ts", started)
