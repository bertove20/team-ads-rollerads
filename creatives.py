"""Optimasi creative: judul & gambar iklan mana yang menang, mana yang buang-buang uang.

Di push/in-page/native, creative adalah pengungkit terbesar: dua judul untuk campaign yang sama bisa berbeda
CTR berkali lipat. RollerAds menyimpan statistik per creative (group=creative), tetapi selama ini tidak pernah
dilihat tim.

Modul ini menarik angka per creative, lalu mengusulkan (tetap butuh Setuju Owner):
- Hentikan creative yang boros: sudah cukup spend tetapi tanpa konversi / CPA di atas target.
- Tambah creative baru: kalau sudah ada pemenang jelas, Creative menulis variasi baru memakai gambar pemenang,
  supaya pengujian terus berjalan.
"""
import logging
import time

import bemob
import config
import rollerads
import storage

log = logging.getLogger(__name__)

KEY = "creative_days"
STATE_KEY = "creative_state"
GROUP = "creative"
MAX_DAYS = 14
LOOKBACK = 3
MIN_CREATIVES = 1      # minimal creative yang harus tersisa di campaign
MAX_CREATIVES = 5      # jangan menambah creative kalau sudah sebanyak ini
COOLDOWN_HOURS = 48

COPY_SCHEMA = {
    "type": "object",
    "properties": {"title": {"type": "string"}, "descr": {"type": "string"}, "angle": {"type": "string"}},
    "required": ["title", "descr", "angle"],
    "additionalProperties": False,
}


def _blank() -> dict:
    return {"impressions": 0, "clicks": 0, "conversions": 0, "cost": 0.0, "revenue": 0.0}


def _finish(b: dict) -> dict:
    cost, conv, clicks = b["cost"], b["conversions"], b["clicks"]
    return {**{k: round(v, 4) if isinstance(v, float) else v for k, v in b.items()},
            "profit": round(b["revenue"] - cost, 4),
            "roi": round((b["revenue"] - cost) / cost * 100, 1) if cost else None,
            "cpa": round(cost / conv, 4) if conv else None,
            "ctr": round(clicks / b["impressions"] * 100, 3) if b["impressions"] else None,
            "cr": round(conv / clicks * 100, 2) if clicks else None}


async def collect(day: str | None = None) -> dict | None:
    """Ambil statistik per creative hari ini, hitung pendapatannya, lalu simpan ringkasannya."""
    if not config.ROLLERADS_API_KEY:
        return None
    day = day or rollerads.today()
    try:
        campaigns = await rollerads.campaigns()
        ids = list(campaigns)
        rows = await rollerads.stats(GROUP, ids, day)
    except rollerads.RollerAdsError as e:
        log.warning("Statistik creative tidak bisa diambil: %s", e)
        return None
    if not rows:
        return None

    per_conversion: dict[int, float] = {}
    if bemob.enabled():
        try:
            for cid, value in (await bemob.by_campaign(day, ids)).items():
                conv = value.get("conversions") or 0
                if conv:
                    per_conversion[int(cid)] = float(value.get("revenue") or 0) / conv
        except bemob.BeMobError:
            pass

    summary: dict[str, dict] = {}
    for row in rows:
        creative_id = int(row.get("creative_id") or 0)
        if not creative_id:
            continue
        cid = int(row.get("campaign_id") or 0)
        conversions = int(row.get("cnt_conversion") or 0)
        value = per_conversion.get(cid, config.ROLLERADS_PAYOUT_USD)
        key = f"{row.get('campaign_title') or campaigns.get(cid, {}).get('title', cid)}|{creative_id}"
        b = summary.setdefault(key, _blank())
        b["impressions"] += int(row.get("cnt_impression") or 0)
        b["clicks"] += int(row.get("cnt_click") or 0)
        b["conversions"] += conversions
        b["cost"] += float(row.get("amt_imoney") or 0)
        b["revenue"] += conversions * value

    data = storage.get(KEY, {})
    data = data if isinstance(data, dict) else {}
    data[day] = {k: _finish(v) for k, v in summary.items()}
    for old in sorted(data)[:-MAX_DAYS]:
        data.pop(old, None)
    storage.put(KEY, data)
    return data[day]


def combined(limit: int = LOOKBACK) -> dict[str, dict]:
    """Gabungan beberapa hari terakhir: "campaign|creative_id" -> angka."""
    data = storage.get(KEY, {})
    data = data if isinstance(data, dict) else {}
    total: dict[str, dict] = {}
    for _, summary in sorted(data.items())[-limit:]:
        for key, value in summary.items():
            b = total.setdefault(key, _blank())
            for field in ("impressions", "clicks", "conversions", "cost", "revenue"):
                b[field] += value.get(field, 0)
    return {k: _finish(v) for k, v in total.items()}


def by_campaign(limit: int = LOOKBACK) -> dict[str, dict[str, dict]]:
    out: dict[str, dict[str, dict]] = {}
    for key, stat in combined(limit).items():
        campaign, creative_id = key.rsplit("|", 1)
        out.setdefault(campaign, {})[creative_id] = stat
    return out


def _state() -> dict:
    data = storage.get(STATE_KEY, {})
    return data if isinstance(data, dict) else {}


def _cooling(key: str) -> bool:
    return time.time() - _state().get(key, 0) < COOLDOWN_HOURS * 3600


def mark(key: str) -> None:
    storage.put(STATE_KEY, {**_state(), key: time.time()})


def suggestions() -> list[dict]:
    """Usulan berbasis data creative (belum dikirim)."""
    import targets
    target_cpa, _ = targets.max_cpa()
    min_spend = max(config.ZONE_WASTE_USD, 1.0)
    out: list[dict] = []
    for campaign, items in by_campaign().items():
        if len(items) < 2:
            # Hanya satu creative: tidak ada pembanding. Usulkan variasi baru supaya bisa diuji.
            only = next(iter(items.values()), None)
            if only and only["clicks"] >= 50 and not _cooling(f"new|{campaign}"):
                out.append({"type": "new_creative", "campaign": campaign, "values": [], "hours": [], "zones": [],
                            "current_value": 0, "new_value": 0, "website": "", "brief": "variasi judul baru",
                            "reason": f"Campaign ini cuma punya 1 creative ({only['clicks']} klik, CTR "
                                      f"{only['ctr'] or 0:.2f}%). Tanpa pembanding, tidak ada yang bisa dioptimasi."})
            continue
        bad, good = [], []
        for creative_id, s in items.items():
            if s["cost"] >= min_spend and (s["conversions"] == 0 or (target_cpa and s["cpa"] and s["cpa"] > target_cpa)):
                bad.append((creative_id, s))
            elif s["conversions"] > 0:
                good.append((creative_id, s))
        if bad and len(items) - len(bad) >= MIN_CREATIVES and good and not _cooling(f"pause|{campaign}"):
            waste = sum(s["cost"] for _, s in bad)
            keep = [cid for cid in items if cid not in {c for c, _ in bad}]
            out.append({"type": "pause_creative", "campaign": campaign, "values": sorted(c for c, _ in bad),
                        "zones": keep, "hours": [], "current_value": 0, "new_value": 0, "website": "", "brief": "",
                        "reason": f"{len(bad)} creative menghabiskan ${waste:.2f} dalam {LOOKBACK} hari tanpa hasil, "
                                  f"sedangkan creative {good[0][0]} menghasilkan "
                                  f"{good[0][1]['conversions']} konversi."})
        if good and len(items) < MAX_CREATIVES and not _cooling(f"new|{campaign}"):
            best = max(good, key=lambda kv: kv[1]["profit"])
            out.append({"type": "new_creative", "campaign": campaign, "values": [best[0]], "hours": [], "zones": [],
                        "current_value": 0, "new_value": 0, "website": "",
                        "brief": f"buat variasi dari creative pemenang (CTR {best[1]['ctr'] or 0:.2f}%, "
                                 f"{best[1]['conversions']} konversi)",
                        "reason": f"Creative {best[0]} menang (profit ${best[1]['profit']:.2f}). Uji variasi baru "
                                  "memakai gambar yang sama supaya hasilnya bisa lebih baik lagi."})
    return out


def text(limit: int = 8) -> str:
    """Ringkasan untuk rapat & laporan."""
    items = combined()
    if not items:
        return ""
    rows = sorted(items.items(), key=lambda kv: -kv[1]["cost"])[:limit]
    lines = [f"PERFORMA CREATIVE {LOOKBACK} HARI TERAKHIR (campaign|id creative):"]
    for key, s in rows:
        lines.append(f"- {key}: CTR {s['ctr'] or 0:.2f}%, klik {s['clicks']}, konv {s['conversions']}, "
                     f"spend ${s['cost']:.2f}, profit ${s['profit']:.2f}")
    lines.append("Creative dengan CTR bagus tetapi konversi nol = judulnya menipu; ganti pesannya.")
    return "\n".join(lines)


async def run(team) -> list[dict]:
    """Ambil data terbaru lalu kirim usulan creative ke Owner."""
    await collect()
    import meeting
    sent = []
    for action in suggestions():
        proposal_id = storage.add_proposal(action)
        mark(f"{'pause' if action['type'] == 'pause_creative' else 'new'}|{action['campaign']}")
        sent.append({"id": proposal_id, **action})
        await team.send("creative", "approval",
                        f"🎨 Usulan #{proposal_id} (dari performa creative)\n\n{meeting.describe_action(action)}\n\n"
                        "Tekan Setuju untuk menjalankannya.",
                        reply_markup=meeting.approval_keyboard(proposal_id), via_leader=True)
    return sent
