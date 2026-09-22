"""Nilai pemain (LTV): apakah pemain yang didapat benar-benar bernilai, bukan cuma murah di awal.

Script tracking di website mengirim setiap pendaftaran & deposit ke dashboard ini (endpoint /collect), selain
ke BeMob. Dari situ sistem bisa menjawab yang tidak bisa dijawab BeMob:
- Berapa nilai rata-rata satu pemain setelah 1, 7, dan 30 hari (ARPU D1/D7/D30)?
- Berapa persen pemain yang deposit lagi (repeat)?
- Campaign/zone mana yang pemainnya bertahan, bukan cuma banyak pendaftar lalu hilang?

Perhitungan berbasis "cohort": pemain dikelompokkan menurut hari pertama dia muncul.
"""
import datetime as dt
import logging
import secrets

import config
import storage

log = logging.getLogger(__name__)

TOKEN_KEY = "collect_token"
WINDOWS = (1, 7, 30)   # hari setelah pemain pertama muncul
MAX_EVENT_AGE_DAYS = 120


def token() -> str:
    """Token rahasia yang dipasang di script website supaya orang lain tidak bisa mengirim data palsu."""
    value = storage.get(TOKEN_KEY)
    if not value:
        value = secrets.token_urlsafe(18)
        storage.put(TOKEN_KEY, value)
    return value


def collect_url() -> str | None:
    """Alamat penerima data. Hanya tersedia jika dashboard punya domain publik (VPS)."""
    return f"https://{config.DASHBOARD_DOMAIN}/collect" if config.DASHBOARD_DOMAIN else None


def _players(days: int) -> dict[str, dict]:
    """Kumpulkan kejadian per pemain (click_id): kapan pertama muncul, campaign, dan semua depositnya."""
    since = dt.datetime.now(config.TIMEZONE).timestamp() - min(days + max(WINDOWS), MAX_EVENT_AGE_DAYS) * 86400
    players: dict[str, dict] = {}
    for e in storage.player_events_since(since):
        p = players.setdefault(e["click_id"], {"first": e["ts"], "campaign": e["campaign"] or "-",
                                               "zone": e["zone"] or "-", "site": e["site"] or "-",
                                               "reg": 0, "deposits": [], "value": 0.0})
        p["first"] = min(p["first"], e["ts"])
        if e["campaign"] and p["campaign"] == "-":
            p["campaign"] = e["campaign"]
        if e["event"] == "reg":
            p["reg"] += 1
        else:
            p["deposits"].append((e["ts"], float(e["value"] or 0)))
            p["value"] += float(e["value"] or 0)
    return players


def _metrics(players: list[dict]) -> dict:
    n = len(players)
    if not n:
        return {}
    now = dt.datetime.now(config.TIMEZONE).timestamp()
    out = {"players": n, "depositors": 0, "repeat": 0, "revenue": 0.0, "first_deposit": 0.0}
    firsts = []
    for p in players:
        deposits = sorted(p["deposits"])
        out["revenue"] += p["value"]
        if deposits:
            out["depositors"] += 1
            firsts.append(deposits[0][1])
        if len(deposits) >= 2:
            out["repeat"] += 1
    for window in WINDOWS:
        matured = [p for p in players if now - p["first"] >= window * 86400]
        if matured:
            value = sum(sum(v for ts, v in p["deposits"] if ts - p["first"] <= window * 86400) for p in matured)
            out[f"arpu_d{window}"] = round(value / len(matured), 4)
            out[f"matured_d{window}"] = len(matured)
        else:
            out[f"arpu_d{window}"] = None
            out[f"matured_d{window}"] = 0
    out["revenue"] = round(out["revenue"], 2)
    out["arpu"] = round(out["revenue"] / n, 4)
    out["cr_deposit"] = round(out["depositors"] / n * 100, 1)
    out["repeat_rate"] = round(out["repeat"] / out["depositors"] * 100, 1) if out["depositors"] else None
    out["first_deposit"] = round(sum(firsts) / len(firsts), 2) if firsts else None
    return out


def report(days: int = 30) -> dict:
    """Ringkasan keseluruhan, per campaign, dan per hari pendaftaran."""
    players = _players(days)
    if not players:
        return {"ready": False, "players": 0, "collect_url": collect_url(), "events": storage.player_event_count()}
    by_campaign: dict[str, list] = {}
    by_day: dict[str, list] = {}
    for p in players.values():
        by_campaign.setdefault(p["campaign"], []).append(p)
        day = dt.datetime.fromtimestamp(p["first"], config.TIMEZONE).date().isoformat()
        by_day.setdefault(day, []).append(p)
    return {
        "ready": True,
        "collect_url": collect_url(),
        "events": storage.player_event_count(),
        "total": _metrics(list(players.values())),
        "by_campaign": sorted(({"name": k, **_metrics(v)} for k, v in by_campaign.items()),
                              key=lambda x: -x["revenue"]),
        "by_day": [{"day": d, **_metrics(v)} for d, v in sorted(by_day.items())][-30:],
    }


def text(limit: int = 6) -> str:
    """Ringkasan untuk prompt agent & laporan harian (dipakai saat menilai campaign)."""
    data = report()
    if not data.get("ready"):
        return ""
    t = data["total"]
    lines = [f"NILAI PEMAIN (dari script di website, {t['players']} pemain 30 hari terakhir):",
             f"- Rata-rata nilai per pemain: ${t['arpu']:.2f} (D1 ${t['arpu_d1'] or 0:.2f}, D7 ${t['arpu_d7'] or 0:.2f}, "
             f"D30 ${t['arpu_d30'] or 0:.2f})",
             f"- {t['cr_deposit']}% pendaftar melakukan deposit; "
             + (f"{t['repeat_rate']}% di antaranya deposit lagi" if t["repeat_rate"] is not None else "belum ada repeat")]
    for c in data["by_campaign"][:limit]:
        if c["name"] == "-":
            continue
        lines.append(f"- {c['name']}: {c['players']} pemain, nilai/pemain ${c['arpu']:.2f}, "
                     f"D7 ${c['arpu_d7'] or 0:.2f}, repeat {c['repeat_rate'] if c['repeat_rate'] is not None else 0}%")
    lines.append("Pakai angka ini untuk menilai campaign: CPA murah tetapi nilai pemain kecil = rugi.")
    return "\n".join(lines)
