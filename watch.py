"""Pengawas otomatis tanpa AI (gratis): hal-hal yang membuat uang hilang diam-diam.

1. Biaya AI: rem darurat jika biaya AI hari ini melewati batas (AI_DAILY_BUDGET_USD).
2. Saldo RollerAds: alert sebelum saldo habis (iklan berhenti tanpa pemberitahuan).
3. Moderasi & status campaign: campaign ditolak moderasi atau berhenti karena limit -> alert.
4. Rekonsiliasi konversi: selisih konversi RollerAds vs BeMob -> tanda postback bocor / trafik curang.

Semua alert hanya dikirim saat keadaan BERUBAH, supaya grup tidak spam.
"""
import datetime as dt
import logging
import time

import bemob
import config
import llm
import rollerads
import storage
from agents import LEADER
from telegram_team import Team

log = logging.getLogger(__name__)

STATE_KEY = "watch_state"
MODERATION_BAD = {"declined": "DITOLAK moderasi", "malicious": "ditandai berbahaya",
                  "moderation": "masih menunggu moderasi", "review": "sedang ditinjau ulang"}
STATE_NOTE = {
    "pending": "belum mulai jalan (pending)",
    "paused_day_revenue": "berhenti hari ini: batas revenue harian tercapai",
    "paused_day_impressions": "berhenti hari ini: batas impresi harian tercapai",
    "paused_day_clicks": "berhenti hari ini: batas klik harian tercapai",
    "paused_day_conversions": "berhenti hari ini: batas konversi harian tercapai",
    "paused_total_revenue": "berhenti: batas revenue total tercapai",
    "paused_total_impressions": "berhenti: batas impresi total tercapai",
    "paused_total_clicks": "berhenti: batas klik total tercapai",
    "paused_total_conversions": "berhenti: batas konversi total tercapai",
}


def _state() -> dict:
    data = storage.get(STATE_KEY, {})
    return data if isinstance(data, dict) else {}


def _remember(**fields) -> None:
    storage.put(STATE_KEY, {**_state(), **fields})


def _today() -> str:
    return dt.datetime.now(config.TIMEZONE).date().isoformat()


# ---------------------------------------------------------------- 1. rem darurat biaya AI

def ai_budget_left() -> float | None:
    """Sisa jatah biaya AI hari ini (USD). None = tanpa batas."""
    if config.AI_DAILY_BUDGET_USD <= 0:
        return None
    spent, _ = llm.cost_since(llm.start_of_day_ts())
    return config.AI_DAILY_BUDGET_USD - spent


async def check_ai_budget(team: Team) -> None:
    left = ai_budget_left()
    if left is None:
        return
    cap = config.AI_DAILY_BUDGET_USD
    spent = cap - left
    state, today = _state(), _today()
    if left <= 0 and state.get("ai_stop_day") != today:
        _remember(ai_stop_day=today, ai_warn_day=today)
        await team.send(LEADER, "alert", f"🛑 REM DARURAT BIAYA AI: hari ini sudah ${spent:.2f} dari batas "
                        f"${cap:.2f}. Semua pemanggilan AI dihentikan sampai besok.\nCek dashboard → 💰 Biaya AI "
                        "untuk melihat pekerjaan mana yang boros. Batasnya bisa diubah di Pengaturan → Batas pengaman.")
    elif 0 < left <= cap * 0.2 and state.get("ai_warn_day") != today:
        _remember(ai_warn_day=today)
        await team.send(LEADER, "alert", f"⚠️ Biaya AI hari ini ${spent:.2f} dari batas ${cap:.2f} (sisa "
                        f"${left:.2f}). Kalau habis, AI berhenti sampai besok.")


# ---------------------------------------------------------------- 2. saldo RollerAds

async def check_balance(team: Team) -> None:
    if not config.ROLLERADS_API_KEY:
        return
    try:
        account = await rollerads.account()
    except rollerads.RollerAdsError as e:
        log.warning("Saldo RollerAds tidak bisa dibaca: %s", e)
        return
    balance = account["balance"]
    days = storage.daily_snapshots(8)[:-1] or []
    per_day = sum(d["total"].get("cost", 0) for d in days) / len(days) if days else 0
    left_days = balance / per_day if per_day > 0 else None
    state = _state()
    low = balance < config.MIN_BALANCE_USD or (left_days is not None and left_days < 2)
    if low and time.time() - state.get("balance_alert_ts", 0) > 12 * 3600:
        _remember(balance_alert_ts=time.time(), balance_low=True)
        habis = f" Dengan rata-rata spend ${per_day:.2f}/hari, saldo habis sekitar {left_days:.1f} hari lagi." \
            if left_days is not None else ""
        await team.send(LEADER, "alert", f"💳 Saldo RollerAds tinggal ${balance:.2f}.{habis}\nTop-up sebelum habis, "
                        "karena kalau saldo nol semua iklan berhenti tanpa pemberitahuan.")
    elif not low and state.get("balance_low"):
        _remember(balance_low=False)
        await team.send(LEADER, "alert", f"✅ Saldo RollerAds sudah terisi lagi: ${balance:.2f}.")


# ---------------------------------------------------------------- 3. moderasi & status campaign

async def check_campaign_health(team: Team) -> None:
    """Campaign yang ditolak moderasi, masih pending, atau berhenti karena limit: uang diam."""
    if not config.ROLLERADS_API_KEY:
        return
    try:
        campaigns = await rollerads.campaigns()
    except rollerads.RollerAdsError as e:
        log.warning("Campaign RollerAds tidak bisa dibaca: %s", e)
        return
    seen = dict(_state().get("campaign_health") or {})
    changed = {}
    for cid, info in list(campaigns.items())[:40]:
        if info["status"] != "active":
            continue
        try:
            detail = await rollerads.detail(cid)
        except rollerads.RollerAdsError:
            continue
        moderation = (detail.get("campaign_moderation") or "").lower()
        state = (detail.get("campaign_state") or "").lower()
        problem = MODERATION_BAD.get(moderation) or STATE_NOTE.get(state)
        key = f"{moderation}|{state}"
        changed[str(cid)] = key
        if not problem or seen.get(str(cid)) == key:
            continue
        note = detail.get("campaign_status_help") or ""
        urgent = moderation in ("declined", "malicious")
        await team.send(LEADER, "alert", f"{'🔴' if urgent else '🟠'} Campaign {info['title']} (#{cid}) {problem}."
                        + (f"\nCatatan RollerAds: {note}" if note else "")
                        + ("\nCampaign aktif tetapi tidak menghasilkan trafik. Perbaiki di panel RollerAds "
                           "(URL/creative yang ditolak), atau pause supaya tidak membingungkan laporan."
                           if urgent else "\nCek di panel RollerAds apakah perlu tindakan."))
    for cid, key in seen.items():  # pulih: dulu bermasalah, sekarang normal
        if cid in changed and changed[cid] != key and not any(
                b in changed[cid] for b in ("declined", "malicious", "moderation", "review", "pending", "paused_")):
            title = campaigns.get(int(cid), {}).get("title", cid)
            await team.send(LEADER, "alert", f"🟢 Campaign {title} (#{cid}) sudah normal kembali (disetujui & jalan).")
    _remember(campaign_health=changed)


# ---------------------------------------------------------------- 4. rekonsiliasi konversi

async def check_conversions(team: Team, day: str | None = None) -> dict | None:
    """Bandingkan konversi RollerAds vs BeMob kemarin. Selisih besar = postback bocor atau trafik curang."""
    if not (config.ROLLERADS_API_KEY and bemob.enabled()):
        return None
    day = day or (dt.datetime.now(config.TIMEZONE).date() - dt.timedelta(days=1)).isoformat()
    try:
        campaigns = await rollerads.campaigns()
        ids = list(campaigns)
        rows = await rollerads.stats("campaign", ids, day)
        tracked = await bemob.by_campaign(day, ids)
    except (rollerads.RollerAdsError, bemob.BeMobError) as e:
        log.warning("Rekonsiliasi konversi gagal: %s", e)
        return None
    result = []
    for row in rows:
        cid = int(row.get("campaign_id") or 0)
        ra = int(row.get("conversions") or 0)
        bm = int((tracked.get(cid) or {}).get("conversions") or 0)
        if not (ra or bm):
            continue
        base = max(ra, bm)
        diff_pct = abs(ra - bm) / base * 100 if base else 0
        result.append({"id": cid, "title": campaigns.get(cid, {}).get("title", str(cid)),
                       "rollerads": ra, "bemob": bm, "diff_pct": round(diff_pct, 1)})
    if not result:
        return None
    bad = [r for r in result if r["diff_pct"] >= config.CONV_DIFF_ALERT_PCT and max(r["rollerads"], r["bemob"]) >= 3]
    report = {"day": day, "rows": result, "checked_at": time.time()}
    _remember(reconcile=report)
    if bad and _state().get("reconcile_alert_day") != day:
        _remember(reconcile_alert_day=day)
        lines = "\n".join(f"- {r['title']}: BeMob {r['bemob']} vs RollerAds {r['rollerads']} (selisih {r['diff_pct']:.0f}%)"
                          for r in bad[:8])
        await team.send("tracking", "alert", f"🔍 Selisih konversi {day} (BeMob vs RollerAds):\n{lines}\n\n"
                        "Kemungkinan penyebab: postback BeMob → RollerAds tidak terkirim semua, konversi tercatat "
                        "dobel, atau trafik curang. Cek dashboard → 🔌 Script Tracking → Tes koneksi, dan postback di BeMob.")
    return report


# ---------------------------------------------------------------- dijalankan berkala

def beat() -> None:
    """Tanda program masih hidup. Dicek dari luar oleh deploy/heartbeat-check.sh; jika basi, Owner diberi tahu."""
    storage.put("heartbeat_ts", time.time())


async def run(team: Team) -> None:
    beat()
    """Dipanggil tiap pemeriksaan rutin. Setiap bagian dijaga agar kegagalannya tidak menghentikan yang lain."""
    for name, job in (("biaya AI", check_ai_budget), ("saldo", check_balance),
                      ("kesehatan campaign", check_campaign_health)):
        try:
            await job(team)
        except Exception:  # noqa: BLE001 - pengawas tidak boleh mematikan program
            log.exception("Pengawas %s gagal", name)
    # Rekonsiliasi cukup sekali sehari (butuh laporan BeMob hari sebelumnya)
    if _state().get("reconcile_day") != _today():
        _remember(reconcile_day=_today())
        try:
            await check_conversions(team)
        except Exception:  # noqa: BLE001
            log.exception("Rekonsiliasi konversi gagal")
