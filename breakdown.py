"""Rincian performa per NEGARA, DEVICE/OS, dan JAM — bagian yang paling menentukan untung/rugi.

Sebelumnya tim hanya melihat angka per campaign dan per zone, jadi tidak tahu negara mana yang rugi,
device apa yang boros, dan jam berapa uang terbuang. Modul ini menariknya dari RollerAds
(group=campaign-country-hour-os), menyimpan riwayatnya, lalu membuat usulan otomatis:

- Jam boros      -> usulan atur jam tayang (dayparting)
- Negara rugi    -> usulan kecualikan negara itu
- OS/device rugi -> usulan kecualikan OS itu

Semua usulan tetap menunggu tombol Setuju dari Owner dan tetap lewat batas pengaman.
Pendapatan dihitung dari BeMob per campaign (revenue per konversi), atau ROLLERADS_PAYOUT_USD jika BeMob kosong.
"""
import datetime as dt
import logging
import time

import bemob
import config
import rollerads
import storage

log = logging.getLogger(__name__)

KEY = "breakdown_days"       # hari -> ringkasan (disimpan MAX_DAYS hari terakhir)
STATE_KEY = "breakdown_state"
GROUP = "campaign-country-hour-os"
MAX_DAYS = 14
LOOKBACK = 3                 # usulan dinilai dari gabungan 3 hari terakhir
MIN_HOURS_KEPT = 6           # jam tayang tidak boleh kurang dari ini
COOLDOWN_HOURS = 48          # jeda usulan sejenis untuk campaign yang sama


def _blank() -> dict:
    return {"clicks": 0, "conversions": 0, "cost": 0.0, "revenue": 0.0}


def _add(bucket: dict, row: dict) -> None:
    bucket["clicks"] += row["clicks"]
    bucket["conversions"] += row["conversions"]
    bucket["cost"] += row["cost"]
    bucket["revenue"] += row["revenue"]


def _finish(bucket: dict) -> dict:
    cost, revenue, conv = bucket["cost"], bucket["revenue"], bucket["conversions"]
    return {**{k: round(v, 4) if isinstance(v, float) else v for k, v in bucket.items()},
            "profit": round(revenue - cost, 4),
            "roi": round((revenue - cost) / cost * 100, 1) if cost else None,
            "cpa": round(cost / conv, 4) if conv else None}


def _hour_of(row: dict) -> int | None:
    """Jam dari kolom waktu RollerAds (mis. "2026-09-22 14:00:00")."""
    for key in ("ts_title", "ts", "dt", "hour"):
        value = str(row.get(key) or "")
        if not value:
            continue
        if value.isdigit() and len(value) <= 2:
            return int(value)
        for part in value.replace("T", " ").split():
            if ":" in part:
                try:
                    return int(part.split(":")[0])
                except ValueError:
                    continue
    return None


async def collect(day: str | None = None) -> dict | None:
    """Ambil rincian satu hari dari RollerAds, hitung pendapatannya, lalu simpan ringkasannya."""
    if not config.ROLLERADS_API_KEY:
        return None
    day = day or rollerads.today()
    try:
        campaigns = await rollerads.campaigns()
        ids = list(campaigns)
        rows = await rollerads.stats(GROUP, ids, day)
    except rollerads.RollerAdsError as e:
        log.warning("Rincian negara/jam/OS tidak bisa diambil: %s", e)
        return None
    if not rows:
        return None

    # Pendapatan per konversi: dari BeMob kalau ada, kalau tidak dari payout tetap.
    per_conversion: dict[int, float] = {}
    if bemob.enabled():
        try:
            for cid, value in (await bemob.by_campaign(day, ids)).items():
                conv = value.get("conversions") or 0
                if conv:
                    per_conversion[int(cid)] = float(value.get("revenue") or 0) / conv
        except bemob.BeMobError as e:
            log.info("Pendapatan BeMob untuk rincian tidak terbaca: %s", e)

    normalized = []
    for row in rows:
        cid = int(row.get("campaign_id") or 0)
        conversions = int(row.get("cnt_conversion") or 0)
        value = per_conversion.get(cid, config.ROLLERADS_PAYOUT_USD)
        normalized.append({
            "campaign_id": cid,
            "campaign": row.get("campaign_title") or campaigns.get(cid, {}).get("title", str(cid)),
            "country": (row.get("country_iso2") or "?").upper(),
            "country_name": row.get("country_name") or "",
            "os": row.get("dev_os_family") or "?",
            "hour": _hour_of(row),
            "clicks": int(row.get("cnt_click") or 0),
            "conversions": conversions,
            "cost": float(row.get("amt_imoney") or 0),
            "revenue": conversions * value,
        })

    groups = {"country": {}, "os": {}, "hour": {}, "campaign_country": {}, "campaign_os": {}, "campaign_hour": {}}
    for row in normalized:
        keys = {
            "country": row["country"],
            "os": row["os"],
            "hour": str(row["hour"]) if row["hour"] is not None else "?",
            "campaign_country": f"{row['campaign']}|{row['country']}",
            "campaign_os": f"{row['campaign']}|{row['os']}",
            "campaign_hour": f"{row['campaign']}|{row['hour'] if row['hour'] is not None else '?'}",
        }
        for name, key in keys.items():
            _add(groups[name].setdefault(key, _blank()), row)

    summary = {name: {key: _finish(b) for key, b in bucket.items()} for name, bucket in groups.items()}
    summary["has_hour"] = any(r["hour"] is not None for r in normalized)
    data = storage.get(KEY, {})
    data = data if isinstance(data, dict) else {}
    data[day] = summary
    for old in sorted(data)[:-MAX_DAYS]:
        data.pop(old, None)
    storage.put(KEY, data)
    return summary


def days(limit: int = LOOKBACK) -> list[tuple[str, dict]]:
    data = storage.get(KEY, {})
    data = data if isinstance(data, dict) else {}
    return sorted(data.items())[-limit:]


def combined(group: str, limit: int = LOOKBACK) -> dict[str, dict]:
    """Gabungan beberapa hari terakhir untuk satu pengelompokan."""
    total: dict[str, dict] = {}
    for _, summary in days(limit):
        for key, value in (summary.get(group) or {}).items():
            _add(total.setdefault(key, _blank()), value)
    return {k: _finish(v) for k, v in total.items()}


def _bad(stat: dict, min_spend: float) -> bool:
    """Boros = sudah menghabiskan cukup uang tetapi tidak menghasilkan."""
    if stat["cost"] < min_spend:
        return False
    if stat["conversions"] == 0:
        return True
    return stat["roi"] is not None and stat["roi"] < config.ZONE_MIN_ROI_PCT


def _good(stat: dict) -> bool:
    return stat["conversions"] > 0 and (stat["roi"] is None or stat["roi"] >= 0)


def text(limit: int = 6) -> str:
    """Ringkasan untuk rapat & laporan: negara, OS, dan jam terbaik/terboros."""
    if not days(1):
        return ""
    lines = [f"RINCIAN {LOOKBACK} HARI TERAKHIR (dari RollerAds, per negara/device/jam):"]

    def block(title: str, group: str, fmt=lambda k: k):
        rows = [(k, v) for k, v in combined(group).items() if v["cost"] > 0]
        if not rows:
            return
        rows.sort(key=lambda kv: -kv[1]["cost"])
        lines.append(title)
        for key, s in rows[:limit]:
            roi = f"{s['roi']:.0f}%" if s["roi"] is not None else "-"
            lines.append(f"- {fmt(key)}: spend ${s['cost']:.2f}, konv {s['conversions']}, ROI {roi}")

    block("Per negara:", "country")
    block("Per device/OS:", "os")
    block("Per jam (waktu akun RollerAds):", "hour", lambda h: f"jam {h}")
    lines.append("Pakai ini untuk usul: matikan jam boros (dayparting), kecualikan negara/OS rugi, "
                 "fokuskan budget ke yang untung.")
    return "\n".join(lines)


# ---------------------------------------------------------------- usulan otomatis

def _state() -> dict:
    data = storage.get(STATE_KEY, {})
    return data if isinstance(data, dict) else {}


def _cooling(key: str) -> bool:
    return time.time() - _state().get(key, 0) < COOLDOWN_HOURS * 3600


def _mark(key: str) -> None:
    storage.put(STATE_KEY, {**_state(), key: time.time()})


def suggestions() -> list[dict]:
    """Usulan berbasis data (belum dikirim). Tiap usulan sudah lolos pengaman dasar."""
    min_spend = max(config.ZONE_WASTE_USD, 1.0)
    out: list[dict] = []
    per_campaign: dict[str, dict] = {}
    for group, field in (("campaign_hour", "hour"), ("campaign_country", "country"), ("campaign_os", "os")):
        for key, stat in combined(group).items():
            campaign, value = key.split("|", 1)
            per_campaign.setdefault(campaign, {"hour": {}, "country": {}, "os": {}})[field][value] = stat

    for campaign, data in per_campaign.items():
        # 1. Jam boros -> atur jam tayang
        hours = {h: s for h, s in data["hour"].items() if h != "?"}
        bad_hours = sorted(int(h) for h, s in hours.items() if _bad(s, min_spend))
        keep = sorted(int(h) for h in hours if int(h) not in bad_hours)
        if bad_hours and len(keep) >= MIN_HOURS_KEPT and not _cooling(f"hour|{campaign}"):
            waste = sum(hours[str(h)]["cost"] for h in bad_hours)
            out.append({"type": "set_dayparting", "campaign": campaign, "hours": keep, "values": [],
                        "zones": [], "current_value": 0, "new_value": 0, "website": "", "brief": "",
                        "reason": f"Jam {', '.join(f'{h:02d}' for h in bad_hours)} menghabiskan ${waste:.2f} "
                                  f"dalam {LOOKBACK} hari tanpa hasil. Iklan dimatikan pada jam itu."})
        # 2. Negara rugi -> kecualikan
        bad_countries = [c for c, s in data["country"].items() if c != "?" and _bad(s, min_spend * 2)]
        good_countries = [c for c, s in data["country"].items() if c != "?" and _good(s)]
        if bad_countries and good_countries and not _cooling(f"country|{campaign}"):
            waste = sum(data["country"][c]["cost"] for c in bad_countries)
            out.append({"type": "exclude_country", "campaign": campaign, "values": sorted(bad_countries),
                        "hours": [], "zones": [], "current_value": 0, "new_value": 0, "website": "", "brief": "",
                        "reason": f"Negara {', '.join(sorted(bad_countries))} menghabiskan ${waste:.2f} dalam "
                                  f"{LOOKBACK} hari tanpa untung, sedangkan {', '.join(sorted(good_countries)[:3])} "
                                  "menghasilkan."})
        # 3. OS/device rugi -> kecualikan
        bad_os = [o for o, s in data["os"].items() if o != "?" and _bad(s, min_spend * 2)]
        good_os = [o for o, s in data["os"].items() if o != "?" and _good(s)]
        if bad_os and good_os and not _cooling(f"os|{campaign}"):
            waste = sum(data["os"][o]["cost"] for o in bad_os)
            out.append({"type": "exclude_os", "campaign": campaign, "values": sorted(bad_os), "hours": [],
                        "zones": [], "current_value": 0, "new_value": 0, "website": "", "brief": "",
                        "reason": f"{', '.join(sorted(bad_os))} menghabiskan ${waste:.2f} dalam {LOOKBACK} hari "
                                  f"tanpa untung, sedangkan {', '.join(sorted(good_os)[:3])} menghasilkan."})
    return out


async def run(team) -> list[dict]:
    """Ambil data terbaru, lalu kirim usulan (bertombol Setuju) ke Owner."""
    await collect()
    yesterday = (dt.datetime.now(config.TIMEZONE).date() - dt.timedelta(days=1)).isoformat()
    if yesterday not in storage.get(KEY, {}):
        await collect(yesterday)
    import meeting  # di sini supaya tidak saling impor saat program dimulai
    sent = []
    for action in suggestions():
        proposal_id = storage.add_proposal(action)
        _mark(f"{action['type'].replace('set_dayparting', 'hour').replace('exclude_', '')}|{action['campaign']}")
        sent.append({"id": proposal_id, **action})
        await team.send("analyst", "approval",
                        f"🌍 Usulan #{proposal_id} (dari rincian negara/jam/device)\n\n"
                        f"{meeting.describe_action(action)}\n\nTekan Setuju untuk menjalankannya di RollerAds.",
                        reply_markup=meeting.approval_keyboard(proposal_id), via_leader=True)
        log.info("Usulan rincian: %s untuk %s", action["type"], action["campaign"])
    return sent
