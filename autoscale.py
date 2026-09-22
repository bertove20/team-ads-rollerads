"""Auto-scale: campaign yang terbukti untung diusulkan dinaikkan budget/bid-nya (tanpa AI, gratis).

Sistem lama hanya bisa mengerem (auto-pause). Modul ini yang "menggas": tiap pemeriksaan rutin, campaign yang
kemarin untung dan budget-nya habis akan diusulkan naik. Usulan tetap menunggu tombol Setuju dari Owner, dan
tetap dibatasi guardrail (MAX_CAMPAIGN_DAILY_BUDGET_USD, CAMPAIGN_MAX_BID_USD, MAX_BID_CHANGE_PCT).

Syarat aman sebelum diusulkan:
- Campaign berstatus active, moderasi beres, dan bukan campaign archived.
- Kemarin: konversi >= AUTOSCALE_MIN_CONVERSIONS dan ROI >= AUTOSCALE_MIN_ROI_PCT.
- Budget: naik hanya jika budget harian benar-benar habis terpakai (>=85%). Kalau budget masih sisa,
  yang kurang adalah trafik -> yang diusulkan naik adalah bid.
- Jeda AUTOSCALE_COOLDOWN_HOURS per campaign, dan tidak menumpuk usulan yang sama.
- Berhenti jika spend seluruh akun sudah mendekati MAX_DAILY_SPEND_USD.
"""
import datetime as dt
import logging
import time

import config
import rollerads
import storage
from agents import LEADER
from telegram_team import Team

log = logging.getLogger(__name__)

STATE_KEY = "autoscale_state"
SAFE_SPEND_SHARE = 0.9   # spend akun kemarin >= 90% batas harian -> jangan scale dulu
BUDGET_USED_SHARE = 0.85  # budget campaign terpakai >= 85% -> layak dinaikkan


def _state() -> dict:
    data = storage.get(STATE_KEY, {})
    return data if isinstance(data, dict) else {}


def _cooling_down(cid: int) -> bool:
    last = _state().get(str(cid), 0)
    return time.time() - last < config.AUTOSCALE_COOLDOWN_HOURS * 3600


def _mark(cid: int) -> None:
    storage.put(STATE_KEY, {**_state(), str(cid): time.time()})


def _pending_for(title: str) -> bool:
    """Sudah ada usulan serupa yang belum diputuskan Owner?"""
    return any(p["status"] == "pending" and p["action"].get("campaign") == title
               and p["action"]["type"] in ("change_daily_budget", "change_bid")
               for p in storage.list_proposals(40))


def yesterday_results() -> dict[str, dict]:
    """Hasil akhir kemarin per campaign (dari riwayat harian yang disimpan sendiri)."""
    day = (dt.datetime.now(config.TIMEZONE).date() - dt.timedelta(days=1)).isoformat()
    for snap in storage.daily_snapshots(5):
        if snap["day"] == day:
            return snap["campaigns"]
    return {}


def decide(result: dict, detail: dict, campaign: str = "") -> tuple[str, float, float, str] | None:
    """(jenis, nilai sekarang, nilai usulan, alasan) atau None jika belum layak di-scale."""
    import targets  # di sini supaya tidak saling impor saat program dimulai
    conversions = result.get("conversions", 0)
    roi = result.get("roi")
    cost = result.get("cost", 0)
    if conversions < config.AUTOSCALE_MIN_CONVERSIONS or roi is None or roi < config.AUTOSCALE_MIN_ROI_PCT:
        return None
    target_cpa, _ = targets.max_cpa(campaign)
    cpa = cost / conversions if conversions else None
    if target_cpa and cpa and cpa > target_cpa:
        return None  # CPA sudah di atas target: jangan tambah budget untuk kerugian
    budget = float(detail.get("campaign_spent_day") or 0)
    bid = float(detail.get("campaign_bid") or 0)
    step = 1 + config.AUTOSCALE_STEP_PCT / 100
    profit = result.get("profit", 0)
    base = (f"kemarin {conversions} konversi, ROI {roi:.0f}%, profit ${profit:.2f} dari spend ${cost:.2f}")

    if budget > 0 and cost >= budget * BUDGET_USED_SHARE:
        new_budget = min(round(budget * step, 2), config.MAX_CAMPAIGN_DAILY_BUDGET_USD)
        if new_budget <= budget + 0.01:
            return None  # sudah mentok batas pengaman
        return ("change_daily_budget", budget, new_budget,
                f"{base}. Budget harian ${budget:g} habis terpakai, jadi masih ada untung yang tertinggal.")
    if bid > 0:
        new_bid = min(round(bid * step, 4), config.CAMPAIGN_MAX_BID_USD)
        if new_bid <= bid + 0.0001 or (new_bid - bid) / bid * 100 > config.MAX_BID_CHANGE_PCT:
            return None
        return ("change_bid", bid, new_bid,
                f"{base}. Budget ${budget:g} belum habis (spend ${cost:.2f}), jadi yang kurang adalah trafik: "
                "naikkan bid supaya menang lelang lebih sering.")
    return None


async def run(team: Team) -> list[dict]:
    """Periksa semua campaign aktif, kirim usulan scale ke Owner. Mengembalikan usulan yang dikirim."""
    if not (config.AUTOSCALE_ENABLED and config.ROLLERADS_API_KEY):
        return []
    results = yesterday_results()
    if not results:
        return []
    total_spend = sum(r.get("cost", 0) for r in results.values())
    if config.MAX_DAILY_SPEND_USD and total_spend >= config.MAX_DAILY_SPEND_USD * SAFE_SPEND_SHARE:
        log.info("Auto-scale dilewati: spend akun kemarin ${:.2f} sudah dekat batas harian".format(total_spend))
        return []
    try:
        campaigns = await rollerads.campaigns()
    except rollerads.RollerAdsError as e:
        log.warning("Auto-scale tidak bisa membaca campaign: %s", e)
        return []

    import meeting  # di sini supaya tidak saling impor saat program dimulai
    sent = []
    for cid, info in campaigns.items():
        result = results.get(info["title"])
        if info["status"] != "active" or not result or _cooling_down(cid) or _pending_for(info["title"]):
            continue
        try:
            detail = await rollerads.detail(cid)
        except rollerads.RollerAdsError:
            continue
        if (detail.get("campaign_moderation") or "approved").lower() != "approved":
            continue
        decision = decide(result, detail, info["title"])
        if not decision:
            continue
        kind, old, new, reason = decision
        action = {"type": kind, "campaign": info["title"], "zones": [], "current_value": old, "new_value": new,
                  "reason": f"Auto-scale: {reason}", "website": "", "brief": ""}
        proposal_id = storage.add_proposal(action)
        _mark(cid)
        sent.append({"id": proposal_id, **action})
        await team.send(
            "media_buyer", "approval",
            f"📈 Usulan #{proposal_id} (otomatis, campaign untung)\n\n{meeting.describe_action(action)}\n\n"
            "Tekan Setuju untuk langsung menjalankannya di RollerAds.",
            reply_markup=meeting.approval_keyboard(proposal_id), via_leader=True,
        )
        log.info("Auto-scale mengusulkan %s untuk %s: %s -> %s", kind, info["title"], old, new)
    sent += await zone_bids(team, campaigns)
    return sent


async def zone_bids(team: Team, campaigns: dict) -> list[dict]:
    """Zone yang terbukti untung diusulkan diberi bid khusus lebih tinggi, supaya dapat lebih banyak trafik."""
    import analysis
    import data_source
    import meeting
    try:
        snap = await analysis.latest()
    except data_source.DataSourceError:
        return []
    step = 1 + config.AUTOSCALE_STEP_PCT / 100
    by_campaign: dict[str, list] = {}
    for row in snap.good_zones:
        if row.cost >= config.ZONE_WASTE_USD and row.conversions >= 2:
            by_campaign.setdefault(row.campaign, []).append(row)

    sent = []
    for cid, info in campaigns.items():
        zones = by_campaign.get(info["title"]) or []
        if info["status"] != "active" or len(zones) < 2 or _cooling_down_key(f"zonebid|{cid}"):
            continue
        try:
            detail = await rollerads.detail(cid)
        except rollerads.RollerAdsError:
            continue
        bid = float(detail.get("campaign_bid") or 0)
        new_bid = min(round(bid * step, 4), config.CAMPAIGN_MAX_BID_USD)
        if bid <= 0 or new_bid <= bid or (new_bid - bid) / bid * 100 > config.MAX_BID_CHANGE_PCT:
            continue
        top = sorted(zones, key=lambda r: -r.profit)[:10]
        profit = sum(r.profit for r in top)
        action = {"type": "zone_bid", "campaign": info["title"], "zones": [r.zone for r in top],
                  "current_value": bid, "new_value": new_bid, "values": [], "hours": [], "website": "", "brief": "",
                  "reason": f"{len(top)} zone ini menghasilkan profit ${profit:.2f} dengan bid biasa ${bid:g}. "
                            f"Bid khusus ${new_bid:g} membuat iklan menang lelang lebih sering di zone itu saja."}
        proposal_id = storage.add_proposal(action)
        _mark_key(f"zonebid|{cid}")
        sent.append({"id": proposal_id, **action})
        await team.send("media_buyer", "approval",
                        f"🎯 Usulan #{proposal_id} (bid khusus zone bagus)\n\n{meeting.describe_action(action)}\n\n"
                        "Tekan Setuju untuk memasangnya di RollerAds.",
                        reply_markup=meeting.approval_keyboard(proposal_id), via_leader=True)
    return sent


def _cooling_down_key(key: str) -> bool:
    return time.time() - _state().get(key, 0) < config.AUTOSCALE_COOLDOWN_HOURS * 3600


def _mark_key(key: str) -> None:
    storage.put(STATE_KEY, {**_state(), key: time.time()})
