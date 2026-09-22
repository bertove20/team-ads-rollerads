"""Auto-pause campaign RollerAds yang melewati ambang batas atau hasilnya jelek (tanpa AI, jadi gratis).

Hanya campaign berstatus active yang diperiksa; campaign archived tidak pernah dibaca atau diubah.
Campaign yang sudah di-pause otomatis lalu diaktifkan lagi oleh Owner tidak di-pause ulang di hari yang sama.
"""
import logging
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import bemob
import config
import rollerads
import storage
from agents import LEADER
from telegram_team import Team

log = logging.getLogger(__name__)

LOG_KEY = "autopause_log"
MAX_LOG = 200


def resume_keyboard(campaign_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("▶️ Aktifkan lagi", callback_data=f"rc:{campaign_id}:on")]]
    )


def judge(cost: float, conversions: int, account_cost: float, revenue: float | None = None) -> str | None:
    """Alasan pause, atau None jika campaign boleh tetap jalan."""
    if account_cost >= config.MAX_DAILY_SPEND_USD:
        return (f"Spend seluruh akun hari ini ${account_cost:.2f} sudah mencapai batas harian "
                f"${config.MAX_DAILY_SPEND_USD:g}.")
    if cost >= config.MAX_CAMPAIGN_DAILY_BUDGET_USD:
        return (f"Spend tidak wajar: ${cost:.2f} hari ini, melewati batas per campaign "
                f"${config.MAX_CAMPAIGN_DAILY_BUDGET_USD:g}.")
    if cost < config.AUTOPAUSE_MIN_SPEND_USD:
        return None  # masih fase tes
    if conversions == 0:
        return f"Hasil jelek: spend ${cost:.2f} tanpa satu pun konversi."
    cpa = cost / conversions
    if config.AUTOPAUSE_MAX_CPA_USD and cpa > config.AUTOPAUSE_MAX_CPA_USD:
        return f"Hasil jelek: CPA ${cpa:.2f} di atas batas ${config.AUTOPAUSE_MAX_CPA_USD:g}."
    if revenue is None and config.ROLLERADS_PAYOUT_USD:
        revenue = conversions * config.ROLLERADS_PAYOUT_USD
    if revenue:  # pendapatan 0 padahal ada konversi = payout offer belum diatur, ROI tidak dinilai
        roi = (revenue - cost) / cost * 100
        if roi < config.AUTOPAUSE_MIN_ROI_PCT:
            return f"Hasil jelek: ROI {roi:.0f}% di bawah batas {config.AUTOPAUSE_MIN_ROI_PCT:g}%."
    return None


def _state(day: str) -> dict:
    state = storage.get("autopause", {})
    if state.get("day") != day:
        state = {"day": day, "paused": {}, "failed": {}, "postback": {}}
    return state


def paused_today() -> dict[str, str]:
    """ID campaign (string) -> alasan, untuk campaign yang di-pause otomatis hari ini."""
    return _state(rollerads.today())["paused"]


def history(limit: int = 50) -> list[dict]:
    return storage.get(LOG_KEY, [])[-limit:][::-1]


def record(entry: dict) -> None:
    items = storage.get(LOG_KEY, [])
    items.append({"ts": time.time(), **entry})
    storage.put(LOG_KEY, items[-MAX_LOG:])


async def _tracked(team: Team, day: str, campaigns: dict, spend: dict, state: dict) -> dict | None:
    """Data BeMob per campaign, sekaligus cek postback BeMob -> RollerAds. None jika BeMob tidak dipakai."""
    if not bemob.enabled():
        return None
    alerts = state.setdefault("postback", {})
    try:
        tracked = await bemob.by_campaign(day, campaigns)
        postback_url = "ok" if alerts.get("url_checked") else await bemob.rollerads_postback_url()
    except bemob.BeMobError as e:
        log.warning("Data BeMob tidak bisa diambil, memakai konversi RollerAds: %s", e)
        return None
    if not postback_url and not alerts.get("url_checked"):
        await team.send("tracking", "alert", "🚨 Traffic source RollerAds di BeMob tidak punya Postback URL"
                        + (" (atau belum dibuat)" if postback_url is None else "")
                        + ". Konversi tidak akan dikirim ke RollerAds. Isi di BeMob → Traffic Sources → RollerAds.")
    alerts["url_checked"] = True
    for cid, t in tracked.items():
        # Konversi tercatat di BeMob tapi tidak sampai ke RollerAds -> postback bermasalah
        if t["conversions"] >= 2 and spend.get(cid, [0, 0])[1] == 0 and not alerts.get(str(cid)):
            alerts[str(cid)] = True
            await team.send("tracking", "alert",
                            f"⚠️ Campaign {campaigns[cid]['title']} (#{cid}): BeMob mencatat {t['conversions']} "
                            "konversi hari ini, tetapi RollerAds 0. Postback BeMob → RollerAds kemungkinan tidak jalan "
                            "(cek Postback URL traffic source & parameter click_id di URL campaign).")
    return tracked


async def run(team: Team) -> list[dict]:
    """Periksa semua campaign aktif sekali. Mengembalikan daftar campaign yang di-pause."""
    if not config.AUTOPAUSE_ENABLED or not config.ROLLERADS_API_KEY:
        return []
    day = rollerads.today()
    campaigns = await rollerads.campaigns()  # tanpa archived
    active = {cid: c for cid, c in campaigns.items() if c["status"] == "active"}
    if not active:
        return []

    spend: dict[int, list[float]] = {cid: [0.0, 0] for cid in campaigns}
    for row in await rollerads.stats("campaign", campaigns, day):
        cid = int(row.get("campaign_id") or 0)
        if cid in spend:
            spend[cid][0] += float(row.get("amt_imoney") or 0)
            spend[cid][1] += int(row.get("cnt_conversion") or 0)
    account_cost = sum(cost for cost, _ in spend.values())

    state = _state(day)
    tracked = await _tracked(team, day, campaigns, spend, state)
    done = []
    for cid, campaign in active.items():
        if str(cid) in state["paused"]:
            continue  # sudah di-pause otomatis hari ini lalu diaktifkan lagi oleh Owner
        cost, conversions = spend[cid]
        revenue = None
        if tracked is not None:  # konversi & pendapatan asli dari BeMob
            conversions = tracked.get(cid, {}).get("conversions", 0)
            revenue = tracked.get(cid, {}).get("revenue", 0.0)
        reason = judge(cost, int(conversions), account_cost, revenue)
        if not reason:
            continue
        title = campaign["title"]
        try:
            await rollerads.set_status(cid, "paused")
        except rollerads.RollerAdsError as e:
            log.error("Auto-pause campaign %s gagal: %s", cid, e)
            if state["failed"].get(str(cid)) != reason:
                state["failed"][str(cid)] = reason
                await team.send(LEADER, "alert", f"🚨 Campaign {title} (#{cid}) harus di-pause tetapi GAGAL: {e}\n"
                                f"Alasan: {reason}\nOwner, mohon pause manual di dashboard RollerAds.")
            continue
        state["paused"][str(cid)] = reason
        entry = {"campaign_id": cid, "title": title, "reason": reason, "cost": round(cost, 2),
                 "conversions": int(conversions), "action": "paused", "by": "Auto-pause"}
        record(entry)
        done.append(entry)
        log.warning("Campaign %s (#%s) di-pause otomatis: %s", title, cid, reason)
        await team.send(
            LEADER,
            "alert",
            f"⏸️ Campaign {title} (#{cid}) di-PAUSE otomatis.\nAlasan: {reason}\n"
            f"Hari ini: spend ${cost:.2f}, konversi {int(conversions)}.\n"
            "Tekan tombol di bawah jika ingin mengaktifkan lagi (tidak akan di-pause otomatis lagi hari ini).",
            reply_markup=resume_keyboard(cid),
            via_leader=True,
        )
    storage.put("autopause", state)
    return done
