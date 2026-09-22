"""Rapor kinerja tim AI: usulan mana yang benar-benar menghasilkan, mana yang meleset (tanpa AI, gratis).

Tiap tindakan yang dijalankan (pause, blacklist, ubah bid/budget, whitelist, jam tayang, frequency cap) dicatat
di riwayat tindakan. Modul ini membandingkan angka campaign 3 hari SEBELUM vs 3 hari SESUDAH tindakan, lalu
memberi nilai: berhasil / gagal / belum jelas. Hasilnya dipakai tim saat rapat, muncul di laporan harian, dan
bisa dibaca Owner di dashboard.

Penilaian memakai profit harian (pendapatan - biaya iklan). Kalau data pendapatan belum ada (BeMob/payout belum
diisi), penilaian memakai CPA; kalau itu pun tidak ada, hasilnya "belum jelas" dan bukan tebakan.
"""
import datetime as dt
import logging
import time

import autopause
import config
import memory
import storage

log = logging.getLogger(__name__)

WINDOW_DAYS = 3        # berapa hari dibandingkan sebelum vs sesudah
MIN_AGE_HOURS = 24     # tindakan yang lebih baru dari ini belum bisa dinilai
ACTIONS = memory.IMPACT_ACTIONS | {"whitelist": "whitelist zone", "jam": "atur jam tayang",
                                   "frekuensi": "frequency cap", "negara": "kecualikan negara",
                                   "device": "kecualikan device"}


def _source(entry: dict) -> str:
    """Siapa yang mengusulkan: rapat tim, auto-scale, auto-pause, atau Owner sendiri."""
    by = (entry.get("by") or "").lower()
    if "auto-scale" in by or "auto-scale" in (entry.get("reason") or "").lower():
        return "Auto-scale"
    if "usulan" in by:
        return "Rapat tim"
    if "dashboard" in by or "owner" in by:
        return "Owner"
    return "Auto-pause"


def _verdict(before: dict | None, after: dict | None) -> tuple[str, str]:
    """(nilai, penjelasan singkat). nilai: berhasil | gagal | netral | belum jelas."""
    if not before or not after:
        return "belum jelas", "data sebelum/sesudah belum lengkap"
    b_profit = (before.get("profit") if before.get("profit") is not None else None)
    a_profit = (after.get("profit") if after.get("profit") is not None else None)
    if b_profit is not None and a_profit is not None and (abs(b_profit) > 0.01 or abs(a_profit) > 0.01):
        delta = a_profit - b_profit
        arah = "naik" if delta > 0 else "turun"
        nilai = "berhasil" if delta > 0.5 else "gagal" if delta < -0.5 else "netral"
        return nilai, f"profit/hari {arah} ${abs(delta):.2f} (${b_profit:.2f} → ${a_profit:.2f})"
    if before.get("cpa") and after.get("cpa"):
        delta = after["cpa"] - before["cpa"]
        nilai = "berhasil" if delta < -0.01 else "gagal" if delta > 0.01 else "netral"
        return nilai, f"CPA ${before['cpa']:.2f} → ${after['cpa']:.2f}"
    return "belum jelas", "pendapatan & CPA belum terbaca (isi BeMob API atau payout)"


def _metrics(days: list[dict], title: str, dates: list[str]) -> dict | None:
    rows = [d["campaigns"].get(title) for d in days if d["day"] in dates and d["campaigns"].get(title)]
    if not rows:
        return None
    n = len(rows)
    cost = sum(r.get("cost", 0) for r in rows)
    revenue = sum(r.get("revenue", 0) for r in rows)
    conv = sum(r.get("conversions", 0) for r in rows)
    return {"days": n, "cost": cost / n, "revenue": revenue / n, "conversions": conv / n,
            "profit": (revenue - cost) / n, "cpa": cost / conv if conv else None}


def outcomes(max_age_days: int = 30) -> list[dict]:
    """Semua tindakan yang sudah bisa dinilai, terbaru di atas."""
    days = storage.daily_snapshots(max_age_days + WINDOW_DAYS + 2)
    today = dt.datetime.now(config.TIMEZONE).date()
    out = []
    for entry in storage.get(autopause.LOG_KEY, [])[-120:]:
        action = entry.get("action")
        age = time.time() - entry.get("ts", 0)
        if action not in ACTIONS or age > max_age_days * 86400:
            continue
        day = dt.datetime.fromtimestamp(entry["ts"], config.TIMEZONE).date()
        before = _metrics(days, entry.get("title", ""),
                          [(day - dt.timedelta(days=i)).isoformat() for i in range(1, WINDOW_DAYS + 1)])
        after_days = [day + dt.timedelta(days=i) for i in range(1, WINDOW_DAYS + 1)]
        after = _metrics(days, entry.get("title", ""), [d.isoformat() for d in after_days if d < today])
        if age < MIN_AGE_HOURS * 3600:
            verdict, note = "belum jelas", "baru dijalankan, tunggu 1-3 hari"
        else:
            verdict, note = _verdict(before, after)
        out.append({
            "ts": entry["ts"], "campaign": entry.get("title", "-"), "action": action,
            "action_name": ACTIONS[action], "reason": entry.get("reason", ""), "by": entry.get("by", ""),
            "source": _source(entry), "before": before, "after": after, "verdict": verdict, "note": note,
        })
    return sorted(out, key=lambda x: -x["ts"])


def summary(items: list[dict] | None = None) -> dict:
    """Ringkasan per sumber usulan dan per jenis tindakan."""
    items = outcomes() if items is None else items
    def bucket(key_of):
        result: dict[str, dict] = {}
        for it in items:
            b = result.setdefault(key_of(it), {"total": 0, "berhasil": 0, "gagal": 0, "netral": 0,
                                               "belum jelas": 0, "profit_delta": 0.0})
            b["total"] += 1
            b[it["verdict"]] += 1
            if it["before"] and it["after"]:
                b["profit_delta"] += it["after"]["profit"] - it["before"]["profit"]
        for b in result.values():
            dinilai = b["total"] - b["belum jelas"]
            b["akurasi"] = round(b["berhasil"] / dinilai * 100) if dinilai else None
            b["profit_delta"] = round(b["profit_delta"], 2)
        return result
    return {"by_source": bucket(lambda i: i["source"]), "by_action": bucket(lambda i: i["action_name"]),
            "total": len(items)}


def text(limit: int = 10) -> str:
    """Ringkasan untuk prompt agent & laporan harian."""
    items = outcomes()
    if not items:
        return ""
    s = summary(items)
    lines = ["RAPOR TINDAKAN TIM (dinilai dari profit harian 3 hari sebelum vs sesudah):"]
    for source, b in sorted(s["by_source"].items(), key=lambda kv: -kv[1]["total"]):
        akurasi = f"{b['akurasi']}% berhasil" if b["akurasi"] is not None else "belum bisa dinilai"
        lines.append(f"- {source}: {b['total']} tindakan, {akurasi}, perubahan profit total "
                     f"${b['profit_delta']:+.2f}/hari")
    lines.append("Tindakan terakhir:")
    for it in items[:limit]:
        lines.append(f"- {it['campaign']}: {it['action_name']} ({it['source']}) → {it['verdict'].upper()}, {it['note']}")
    return "\n".join(lines)
