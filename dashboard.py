"""Dashboard web (http://localhost:8080) untuk membaca data, mengevaluasi, dan memberi keputusan."""
import asyncio
import datetime as dt
import logging
import os
import re
import time
import csv
from collections import defaultdict
from urllib.parse import urlparse

import anthropic
import httpx
from aiohttp import web

import actions
import analysis
import auth
import autopause
import bemob
import checks
import config
import data_source
import landing
import llm
import ltv
import meeting
import memory
import rollerads
import scorecard
import settings
import storage
import tracking
import watch
from agents import AGENTS, LEADER
from telegram_team import Team

RESTART_EXIT_CODE = 3  # jalankan.bat menyalakan ulang program jika keluar dengan kode ini

log = logging.getLogger(__name__)

WEB_DIR = config.BASE_DIR / "web"
DASHBOARD_THREAD = -1  # percakapan "Tanya Tim" di dashboard terpisah dari topic Telegram
routes = web.RouteTableDef()


# ---------------------------------------------------------------- helpers

def team_of(request: web.Request) -> Team:
    return request.app["team"]


async def body_of(request: web.Request) -> dict:
    try:
        data = await request.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def api_key_ok() -> bool:
    """Minimal satu AI (Claude/OpenAI/Gemini/DeepSeek) punya API key."""
    return llm.any_configured()


def local_time(ts: float) -> dt.datetime:
    return dt.datetime.fromtimestamp(ts, config.TIMEZONE)


def setup_status(team: Team) -> list[dict]:
    """Checklist pemasangan agar Owner tahu apa yang masih kurang."""
    return [
        {"ok": api_key_ok(), "label": "API key AI (Claude / OpenRouter / OpenAI / Gemini / DeepSeek)",
         "hint": "Menu Pengaturan → Claude AI atau Penyedia AI (mis. OpenRouter) → isi API key, lalu klik Tes."},
        {"ok": bool(os.getenv("BOT_TOKEN_HEAD_MARKETING", "").strip()), "label": "Bot Telegram Head of Marketing",
         "hint": "Buat bot di @BotFather, lalu isi tokennya di menu Pengaturan → Telegram. Tanpa ini hanya dashboard yang jalan."},
        {"ok": bool(config.GROUP_ID), "label": "ID grup Telegram",
         "hint": "Masukkan bot ke grup, ketik /id di grup, lalu klik Deteksi di menu Pengaturan → Telegram."},
        {"ok": bool(config.OWNER_IDS), "label": "ID Owner",
         "hint": "Ketik /id di grup, lalu klik Deteksi di menu Pengaturan → Telegram."},
        {"ok": bool(storage.get("topics")), "label": "Topic grup dibuat",
         "hint": "Setelah Telegram tersambung, ketik /setup di grup Telegram."},
        {"ok": config.DATA_SOURCE != "demo", "label": "Memakai data asli",
         "hint": "Masih memakai DATA DEMO (angka contoh). Ganti di menu Pengaturan → Sumber data."},
        {"ok": bool(config.ROLLERADS_API_KEY), "label": "RollerAds API tersambung",
         "hint": "Isi token API RollerAds di menu Pengaturan → Sumber data (pilih RollerAds API)."},
        {"ok": bemob.enabled(), "label": "BeMob API tersambung",
         "hint": "Buat API key di BeMob → Settings → Security, isi di menu Pengaturan → Sumber data."},
        {"ok": bool(config.BEMOB_POSTBACK_URL), "label": "URL postback BeMob untuk script tracking",
         "hint": "Menu Pengaturan → Script tracking website → isi URL postback BeMob."},
        {"ok": bool(config.LANDING_PAGE_URLS), "label": "Landing page dipantau",
         "hint": "Isi alamat di menu Pengaturan → Landing page & tracking."},
    ]


def proposal_view(p: dict) -> dict:
    return {
        "id": p["id"],
        "created": p["created"],
        "status": p["status"],
        "decided_at": p["decided_at"],
        "type": p["action"]["type"],
        "campaign": p["action"].get("campaign"),
        "zones": p["action"].get("zones", []),
        "text": meeting.describe_action(p["action"]),
    }


# ---------------------------------------------------------------- read

@routes.get("/")
async def index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(WEB_DIR / "index.html")


@routes.get("/api/overview")
async def overview(request: web.Request) -> web.Response:
    team = team_of(request)
    today_cost, _ = llm.cost_since(llm.start_of_day_ts())
    month_cost, _ = llm.cost_since(llm.start_of_month_ts())
    health = storage.get("health_detail", {})
    health = health if isinstance(health, dict) else {}
    data = {
        "setup": setup_status(team),
        "online": team.online,
        "paused": bool(storage.get("paused", False)),
        "meeting_running": meeting.is_running(),
        "data_source": config.DATA_SOURCE,
        "limits": {
            "max_daily_spend": config.MAX_DAILY_SPEND_USD,
            "max_bid_change_pct": config.MAX_BID_CHANGE_PCT,
            "max_campaign_budget": config.MAX_CAMPAIGN_DAILY_BUDGET_USD,
            "zone_waste": config.ZONE_WASTE_USD,
            "zone_min_roi": config.ZONE_MIN_ROI_PCT,
            "zone_good_roi": config.ZONE_GOOD_ROI_PCT,
        },
        "ai_cost": {"today": round(today_cost, 2), "month": round(month_cost, 2)},
        "pending_proposals": sum(1 for p in storage.list_proposals(500) if p["status"] == "pending"),
        "open_tasks": len(storage.open_tasks()),
        "health": [{"url": url, **d} for url, d in health.items()],
        "last_meeting": storage.get("last_meeting"),
        "data_error": None,
    }
    try:
        snap = await analysis.latest()
    except data_source.DataSourceError as e:
        data["data_error"] = str(e)
        return web.json_response(data)
    data.update(
        fetched_at=snap.fetched_at,
        total=snap.total.as_dict(),
        campaigns=[{"name": n, **t.as_dict()} for n, t in sorted(snap.campaigns.items())],
        insights=analysis.insights(snap),
        bad_zones=len(snap.bad_zones),
        bad_zones_cost=round(sum(r.cost for r in snap.bad_zones), 2),
        good_zones=len(snap.good_zones),
        zones=len(snap.rows),
    )
    return web.json_response(data)


@routes.get("/api/zones")
async def zones(request: web.Request) -> web.Response:
    try:
        snap = await analysis.latest()
    except data_source.DataSourceError as e:
        return web.json_response({"error": str(e)}, status=503)
    proposed = storage.recently_proposed_zones()
    rows = [
        {
            "campaign": r.campaign,
            "zone": r.zone,
            "visits": r.visits,
            "conversions": r.conversions,
            "cost": round(r.cost, 2),
            "revenue": round(r.revenue, 2),
            "profit": round(r.profit, 2),
            "roi": None if r.roi is None else round(r.roi, 1),
            "status": analysis.zone_status(r, snap),
            "proposed": r.zone in proposed,
        }
        for r in snap.rows
    ]
    return web.json_response({"rows": rows})


@routes.get("/api/trend")
async def trend(request: web.Request) -> web.Response:
    today = dt.datetime.now(config.TIMEZONE).date().isoformat()
    intraday = [
        {"label": local_time(s["ts"]).strftime("%H:%M"), **s["total"]} for s in storage.snapshots_of_day(today)
    ]
    daily = [{"label": d["day"], **d["total"]} for d in storage.daily_snapshots(30)]
    return web.json_response({"today": intraday, "daily": daily})


@routes.get("/api/proposals")
async def proposals(request: web.Request) -> web.Response:
    return web.json_response({"items": [proposal_view(p) for p in storage.list_proposals(100)]})


@routes.get("/api/tasks")
async def tasks(request: web.Request) -> web.Response:
    return web.json_response({"items": storage.list_tasks(100)})


@routes.get("/api/feed")
async def feed(request: web.Request) -> web.Response:
    topic = request.query.get("topic") or None
    limit = min(int(request.query.get("limit", 100)), 500)
    return web.json_response({"items": storage.chat_feed(topic, limit), "topics": config.TOPICS})


@routes.get("/api/checks")
async def checks_view(request: web.Request) -> web.Response:
    health = storage.get("health_detail", {})
    health = health if isinstance(health, dict) else {}
    return web.json_response({
        "health": [{"url": url, **d} for url, d in health.items()],
        "qa": storage.get("qa_last"),
        "configured": bool(config.LANDING_PAGE_URLS or config.TRACKING_URLS),
    })


def _blank() -> dict:
    return {"calls": 0, "input": 0, "output": 0, "cache_read": 0, "cost": 0.0, "models": set()}


def _add(bucket: dict, row: dict, cost: float) -> None:
    bucket["calls"] += 1
    bucket["input"] += row["input_tokens"] + row["cache_write"]
    bucket["output"] += row["output_tokens"]
    bucket["cache_read"] += row["cache_read"]
    bucket["cost"] += cost
    bucket["models"].add(row["model"])


def _rows(buckets: dict, label_of=lambda k: k) -> list[dict]:
    out = []
    for key, b in buckets.items():
        out.append({"key": key, "name": label_of(key), "calls": b["calls"], "input": b["input"],
                    "output": b["output"], "cache_read": b["cache_read"], "cost": round(b["cost"], 4),
                    "per_call": round(b["cost"] / b["calls"], 4) if b["calls"] else 0,
                    "tokens": b["input"] + b["output"] + b["cache_read"],
                    "models": ", ".join(sorted(m.split("/")[-1] for m in b["models"]))})
    return sorted(out, key=lambda x: -x["cost"])


@routes.get("/api/costs")
async def costs(request: web.Request) -> web.Response:
    """Laporan rinci pemakaian token AI: per hari, per tugas, per agent, per model, + penilaian boros/hemat."""
    now = time.time()
    rows = storage.usage_rows_since(now - 30 * 86400)
    by_day: dict[str, float] = defaultdict(float)
    tokens_day: dict[str, int] = defaultdict(int)
    by_task, by_agent, by_model = defaultdict(_blank), defaultdict(_blank), defaultdict(_blank)
    totals = {"today": 0.0, "week": 0.0, "month": 0.0, "all30": 0.0}
    tok = {"input": 0, "output": 0, "cache_read": 0, "calls": 0}
    month_start, day_start = llm.start_of_month_ts(), llm.start_of_day_ts()
    biggest = []
    for row in rows:
        cost = llm.row_cost(row)
        day = local_time(row["ts"]).date().isoformat()
        by_day[day] += cost
        tokens_day[day] += row["input_tokens"] + row["output_tokens"] + row["cache_read"] + row["cache_write"]
        totals["all30"] += cost
        if row["ts"] >= month_start:
            totals["month"] += cost
        if row["ts"] >= day_start:
            totals["today"] += cost
        if row["ts"] >= now - 7 * 86400:  # rincian = 7 hari terakhir
            totals["week"] += cost
            _add(by_task[row["task"] or "lainnya"], row, cost)
            _add(by_agent[row["agent"]], row, cost)
            _add(by_model[row["model"] or "-"], row, cost)
            tok["input"] += row["input_tokens"] + row["cache_write"]
            tok["output"] += row["output_tokens"]
            tok["cache_read"] += row["cache_read"]
            tok["calls"] += 1
            biggest.append({"ts": row["ts"], "agent": AGENTS[row["agent"]].name if row["agent"] in AGENTS
                            else row["agent"], "task": llm.TASKS.get(row["task"] or "lainnya", row["task"]),
                            "model": (row["model"] or "").split("/")[-1],
                            "tokens": row["input_tokens"] + row["output_tokens"] + row["cache_read"] + row["cache_write"],
                            "cost": round(cost, 4)})

    today = dt.datetime.now(config.TIMEZONE).date()
    days = [(today - dt.timedelta(days=i)).isoformat() for i in range(29, -1, -1)]
    task_rows = _rows(by_task, lambda k: llm.TASKS.get(k or "lainnya", k or "lainnya"))
    agent_rows = _rows(by_agent, lambda k: AGENTS[k].name if k in AGENTS else k)

    # Pembanding: biaya iklan & konversi 7 hari terakhir (dari riwayat harian)
    week_days = {(today - dt.timedelta(days=i)).isoformat() for i in range(0, 7)}
    snaps = [d for d in storage.daily_snapshots(10) if d["day"] in week_days]
    ad_spend = sum(d["total"].get("cost", 0) for d in snaps)
    conversions = sum(d["total"].get("conversions", 0) for d in snaps)
    share = (totals["week"] / ad_spend * 100) if ad_spend else None

    tips = []
    if task_rows and totals["week"]:
        top = task_rows[0]
        if top["cost"] / totals["week"] > 0.45:
            tips.append(f"{top['name']} memakan {top['cost'] / totals['week'] * 100:.0f}% biaya AI minggu ini "
                        f"(${top['cost']:.2f}). " + ("Kurangi jadwal rapat di Pengaturan → Jadwal jika terasa boros."
                                                     if top["key"] == "rapat" else "Pertimbangkan model lebih murah "
                                                     "untuk tugas ini di Pengaturan → Penyedia AI."))
    for a in agent_rows[:3]:
        if a["per_call"] > 0.10 and a["calls"] >= 3:
            tips.append(f"{a['name']} rata-rata ${a['per_call']:.3f} per panggilan ({a['models']}). Model lebih murah "
                        "bisa dipakai jika tugasnya tidak berat.")
    if share is not None and share > 10:
        tips.append(f"Biaya AI = {share:.0f}% dari biaya iklan minggu ini. Di atas 10% biasanya tanda boros: "
                    "kurangi rapat, pakai model murah untuk Analyst/Tracking, atau kurangi pertanyaan berulang.")
    elif share is not None and ad_spend:
        tips.append(f"Biaya AI = {share:.1f}% dari biaya iklan minggu ini (${ad_spend:.2f}). Masih wajar (di bawah 10%).")
    if tok["cache_read"] and tok["input"]:
        saved = tok["cache_read"] / (tok["input"] + tok["cache_read"]) * 100
        tips.append(f"{saved:.0f}% teks yang dikirim ulang terbaca dari cache (lebih murah 90%).")

    return web.json_response({
        "by_day": [{"label": d, "cost": round(by_day.get(d, 0), 4), "tokens": tokens_day.get(d, 0)} for d in days],
        "by_task": task_rows, "by_agent": agent_rows,
        "by_model": _rows(by_model, lambda k: k),
        "totals": {k: round(v, 4) for k, v in totals.items()},
        "tokens": tok,
        "biggest": sorted(biggest, key=lambda x: -x["cost"])[:10],
        "ad_spend": round(ad_spend, 2), "conversions": conversions,
        "share_of_spend": None if share is None else round(share, 2),
        "per_conversion": round(totals["week"] / conversions, 4) if conversions else None,
        "projection_month": round(totals["week"] / 7 * 30, 2),
        "tips": tips,
        "model": ", ".join(sorted({llm.provider_name(config.agent_provider(k)) for k in AGENTS})),
    })


_collect_hits: dict[str, list[float]] = defaultdict(list)
COLLECT_LIMIT = 120  # maksimal kiriman per IP per menit


@routes.get("/collect")
@routes.post("/collect")
async def collect(request: web.Request) -> web.Response:
    """Penerima kejadian pemain dari script di website Owner (pendaftaran & deposit) untuk hitung nilai pemain.

    Terbuka tanpa login (dipanggil browser pengunjung), jadi dibatasi: wajib token, dibatasi jumlah per IP,
    nilai dibatasi masuk akal, dan txid yang sama tidak dihitung dua kali.
    """
    data = dict(request.query)
    if request.method == "POST":
        data.update(await body_of(request))
    if data.get("t") != ltv.token():
        return web.json_response({"error": "token salah"}, status=403)
    ip, now = _client_ip(request), time.time()
    hits = [t for t in _collect_hits[ip] if now - t < 60]
    _collect_hits[ip] = hits + [now]
    if len(hits) >= COLLECT_LIMIT:
        return web.json_response({"error": "terlalu banyak kiriman"}, status=429)
    click_id = str(data.get("c") or "")[:120]
    event = "reg" if str(data.get("e") or "").startswith("r") else "dep"
    try:
        value = max(0.0, min(float(data.get("v") or 0), 100000.0))
    except (TypeError, ValueError):
        value = 0.0
    txid = str(data.get("x") or "")[:160] or f"{event}-{click_id}-{int(now)}"
    if not click_id:
        return web.json_response({"error": "click_id kosong"}, status=400)
    fresh = storage.add_player_event(click_id, event, value, txid, str(data.get("s") or "")[:120],
                                     str(data.get("cmp") or "")[:120], str(data.get("z") or "")[:60])
    return web.json_response({"ok": True, "baru": fresh}, headers={"Access-Control-Allow-Origin": "*"})


@routes.get("/api/ltv")
async def ltv_view(request: web.Request) -> web.Response:
    data = ltv.report()
    return web.json_response({**data, "token": ltv.token(), "domain": config.DASHBOARD_DOMAIN})


@routes.get("/api/scorecard")
async def scorecard_view(request: web.Request) -> web.Response:
    """Rapor: tindakan tim mana yang benar-benar menghasilkan profit."""
    items = scorecard.outcomes()
    return web.json_response({
        "items": items[:60],
        "summary": scorecard.summary(items),
        "window_days": scorecard.WINDOW_DAYS,
        "reconcile": (storage.get(watch.STATE_KEY, {}) or {}).get("reconcile"),
        "ai_budget": {"cap": config.AI_DAILY_BUDGET_USD, "spent": round(llm.spent_today(0), 4),
                      "stopped": llm.budget_exceeded()},
        "autoscale": {"enabled": config.AUTOSCALE_ENABLED, "min_conversions": config.AUTOSCALE_MIN_CONVERSIONS,
                      "min_roi": config.AUTOSCALE_MIN_ROI_PCT, "step": config.AUTOSCALE_STEP_PCT},
    })


@routes.get("/api/agents")
async def agents_list(request: web.Request) -> web.Response:
    return web.json_response({"items": [{"key": a.key, "name": a.name, "emoji": a.emoji} for a in AGENTS.values()]})


# ---------------------------------------------------------------- actions

@routes.post("/api/proposals/{id}")
async def decide(request: web.Request) -> web.Response:
    approved = bool((await body_of(request)).get("approved"))
    ok = await actions.decide_proposal(
        team_of(request), int(request.match_info["id"]), approved, "Owner (dashboard)", 0
    )
    if not ok:
        return web.json_response({"error": "Usulan ini sudah diputuskan sebelumnya."}, status=409)
    return web.json_response({"ok": True})


@routes.post("/api/tasks/{id}/done")
async def task_done(request: web.Request) -> web.Response:
    if not await actions.finish_task(team_of(request), int(request.match_info["id"])):
        return web.json_response({"error": "Tugas ini sudah selesai."}, status=409)
    return web.json_response({"ok": True})


@routes.post("/api/meeting")
async def start_meeting(request: web.Request) -> web.Response:
    if not api_key_ok():
        return web.json_response({"error": "Belum ada API key AI. Isi di menu Pengaturan."}, status=400)
    if meeting.is_running():
        return web.json_response({"error": "Rapat sedang berjalan."}, status=409)
    agenda = (await body_of(request)).get("agenda") or "Rapat atas permintaan Owner (dashboard)"
    asyncio.get_running_loop().create_task(meeting.run_meeting(team_of(request), agenda))
    return web.json_response({"ok": True})


@routes.post("/api/ask")
async def ask(request: web.Request) -> web.Response:
    body = await body_of(request)
    agent_key, text = body.get("agent"), (body.get("text") or "").strip()
    if agent_key not in AGENTS or not text:
        return web.json_response({"error": "Pilih agent dan tulis pertanyaan."}, status=400)
    storage.log_chat(DASHBOARD_THREAD, "Owner", text, "dashboard")
    reply = await actions.ask_agent(agent_key, "Owner", DASHBOARD_THREAD)
    storage.log_chat(DASHBOARD_THREAD, AGENTS[agent_key].label, reply, "dashboard")
    return web.json_response({"reply": reply, "agent": AGENTS[agent_key].label})


@routes.get("/api/memory")
async def memory_view(request: web.Request) -> web.Response:
    items = memory.notes()
    return web.json_response({
        "notes": sorted(items, key=lambda n: (not n.get("pinned"), -n["ts"])),
        "kinds": memory.KINDS,
        "decisions": memory.decisions_text(), "impact": memory.impact_text(),
        "last_chat_summary": storage.get(memory.KEY + "_chat_ts", 0),
    })


@routes.post("/api/memory")
async def memory_add(request: web.Request) -> web.Response:
    text = str((await body_of(request)).get("text") or "").strip()
    if len(text) < 3:
        return web.json_response({"error": "Tulis arahannya dulu."}, status=400)
    note = memory.remember(text, source="Owner (dashboard)")
    return web.json_response({"ok": True, "id": note["id"] if note else None, "duplicate": note is None})


@routes.post("/api/memory/{id}/delete")
async def memory_delete(request: web.Request) -> web.Response:
    if not memory.forget(int(request.match_info["id"])):
        return web.json_response({"error": "Catatan tidak ditemukan."}, status=404)
    return web.json_response({"ok": True})


@routes.post("/api/memory/refresh")
async def memory_refresh(request: web.Request) -> web.Response:
    """Rangkum obrolan terbaru ke ingatan sekarang (biasanya otomatis setelah rapat & laporan harian)."""
    if not api_key_ok():
        return web.json_response({"error": "Belum ada API key AI. Isi di menu Pengaturan."}, status=400)
    await memory.consolidate_chat()
    return web.json_response({"ok": True, "count": len(memory.notes())})


@routes.post("/api/creative")
async def creative(request: web.Request) -> web.Response:
    brief = (await body_of(request)).get("brief") or "Buat variasi kreatif untuk campaign yang sedang berjalan."
    ideas = await actions.creative_ideas(brief)
    await team_of(request).send("creative", "kreatif", ideas)
    return web.json_response({"text": ideas})


@routes.post("/api/report")
async def report(request: web.Request) -> web.Response:
    await analysis.latest(max_age=0)  # pastikan AI membaca angka terbaru
    text = await actions.daily_report()
    await team_of(request).send("head_marketing", "laporan", f"🗓️ Laporan harian\n\n{text}")
    return web.json_response({"text": text})


@routes.post("/api/checks/run")
async def run_checks(request: web.Request) -> web.Response:
    await checks.qa_report(team_of(request))
    await checks.health_check(team_of(request))
    return web.json_response({"ok": True})


@routes.post("/api/pause")
async def pause(request: web.Request) -> web.Response:
    storage.put("paused", bool((await body_of(request)).get("paused")))
    return web.json_response({"ok": True})


@routes.post("/api/upload")
async def upload(request: web.Request) -> web.Response:
    """Unggah file CSV export BeMob ke data/inbox (dipakai jika DATA_SOURCE=csv)."""
    reader = await request.multipart()
    part = await reader.next()
    if part is None or not (part.filename or "").lower().endswith(".csv"):
        return web.json_response({"error": "Pilih file .csv."}, status=400)
    name = re.sub(r"[^\w.\-]", "_", os.path.basename(part.filename))
    target = data_source.INBOX_DIR / f"{int(time.time())}_{name}"
    with target.open("wb") as f:
        while chunk := await part.read_chunk():
            f.write(chunk)
    return web.json_response({"ok": True, "file": target.name})


# ---------------------------------------------------------------- RollerAds

@routes.get("/api/rollerads")
async def rollerads_view(request: web.Request) -> web.Response:
    data = {
        "enabled": bool(config.ROLLERADS_API_KEY),
        "autopause": {
            "enabled": config.AUTOPAUSE_ENABLED,
            "minutes": config.AUTOPAUSE_MINUTES,
            "min_spend": config.AUTOPAUSE_MIN_SPEND_USD,
            "max_cpa": config.AUTOPAUSE_MAX_CPA_USD,
            "min_roi": config.AUTOPAUSE_MIN_ROI_PCT,
            "payout": config.ROLLERADS_PAYOUT_USD,
            "campaign_budget": config.MAX_CAMPAIGN_DAILY_BUDGET_USD,
            "daily_spend": config.MAX_DAILY_SPEND_USD,
        },
        "limits": {"max_bid": config.CAMPAIGN_MAX_BID_USD, "max_daily_budget": config.MAX_CAMPAIGN_DAILY_BUDGET_USD},
        "formats": [
            {"id": f, "name": name, "creative": f in rollerads.CREATIVE_FORMATS,
             "bid_models": [{"id": b, "name": rollerads.BID_MODELS[b]} for b in rollerads.FORMAT_BID_MODELS[f]]}
            for f, name in rollerads.FORMATS.items()
        ],
        "landing_pages": config.LANDING_PAGE_URLS,
        "log": autopause.history(50),
        "campaigns": [],
        "error": None,
    }
    if not data["enabled"]:
        return web.json_response(data)
    try:
        me = await rollerads.account()
        campaigns = await rollerads.campaigns()
        rows = await rollerads.stats("campaign", campaigns)
    except rollerads.RollerAdsError as e:
        data["error"] = str(e)
        return web.json_response(data)
    spend = defaultdict(lambda: [0.0, 0, 0])
    for r in rows:
        s = spend[int(r.get("campaign_id") or 0)]
        s[0] += float(r.get("amt_imoney") or 0)
        s[1] += int(r.get("cnt_conversion") or 0)
        s[2] += int(r.get("cnt_click") or r.get("cnt_impression") or 0)
    auto = autopause.paused_today()
    data["account"] = me
    data["campaigns"] = [
        {**c, "format": rollerads.FORMATS.get(c["format_id"], f"Format {c['format_id']}"),
         "bid_model": rollerads.BID_MODELS.get(c["bid_model_id"], str(c["bid_model_id"])),
         "cost": round(spend[cid][0], 2), "conversions": spend[cid][1], "visits": spend[cid][2],
         "cpa": round(spend[cid][0] / spend[cid][1], 2) if spend[cid][1] else None,
         "autopaused": auto.get(str(cid))}
        for cid, c in sorted(campaigns.items(), key=lambda kv: kv[1]["status"] != "active")
    ]
    return web.json_response(data)


@routes.post("/api/rollerads/campaigns/{id}/status")
async def rollerads_status(request: web.Request) -> web.Response:
    status = (await body_of(request)).get("status")
    if status not in ("active", "paused"):
        return web.json_response({"error": "Status harus active atau paused."}, status=400)
    title = await actions.set_campaign_status(team_of(request), int(request.match_info["id"]), status,
                                              "Owner (dashboard)")
    return web.json_response({"ok": True, "title": title})


@routes.post("/api/rollerads/campaigns")
async def rollerads_create(request: web.Request) -> web.Response:
    raw = (await body_of(request)).get("spec") or {}
    site_url = str(raw.get("site_url") or "").strip()
    if site_url and not site_url.startswith(("http://", "https://")):
        return web.json_response({"error": "Website tempat script dipasang harus diawali https://"}, status=400)
    lander_url = click = ""
    if raw.get("flow") == "lander":
        lander_url, click = str(raw.get("lander_url") or "").strip(), str(raw.get("click_url") or "").strip()
        problems = []
        if not lander_url.startswith(("http://", "https://")):
            problems.append("Isi alamat landing page (diawali https://).")
        if not site_url:
            problems.append("Isi website tujuan (tempat daftar/deposit). Script konversi dipasang di sana, "
                            "bukan di landing page.")
        if click and not click.startswith(("http://", "https://")):
            problems.append("Click URL BeMob harus diawali https://")
        target = tracking.host_of(str(raw.get("url") or ""))
        if target and target in (tracking.host_of(lander_url), tracking.host_of(site_url)):
            problems.append("URL tujuan campaign harus link campaign BeMob, bukan alamat landing page atau website. "
                            "Kalau langsung, BeMob tidak mencatat klik dan click_id tidak terbawa.")
        if problems:
            return web.json_response({"error": " ".join(problems), "errors": problems}, status=400)
    spec, errors = await rollerads.validate_spec(raw)
    if errors:
        return web.json_response({"error": " ".join(errors), "errors": errors}, status=400)
    campaign_id = await rollerads.create(spec)
    autopause.record({"campaign_id": campaign_id, "title": spec["title"], "action": "created",
                      "reason": "Dibuat dari dashboard", "by": "Owner (dashboard)"})
    await team_of(request).send(LEADER, "approval", f"🚀 Campaign baru dibuat dari dashboard (#{campaign_id}):\n"
                                f"{rollerads.describe_spec(spec)}\n"
                                + (f"Lewat landing page: {lander_url}\n" if lander_url else "")
                                + "\nDeveloper menyiapkan script tracking untuk website tujuannya"
                                + (" dan mengecek tombol landing page" if lander_url else "")
                                + ", lalu QA mengetes koneksinya.")
    tracking.start_for_campaign(team_of(request), campaign_id, spec, site_url, str(raw.get("site_hints") or ""),
                                lander_url, click)
    return web.json_response({"ok": True, "id": campaign_id})


@routes.post("/api/rollerads/draft")
async def rollerads_draft(request: web.Request) -> web.Response:
    """Media Buyer mengisi form campaign dari permintaan bebas (belum membuat apa pun)."""
    if not api_key_ok():
        return web.json_response({"error": "Belum ada API key AI. Isi di menu Pengaturan."}, status=400)
    brief = ((await body_of(request)).get("brief") or "").strip()
    if not brief:
        return web.json_response({"error": "Tulis permintaannya dulu."}, status=400)
    spec, errors = await rollerads.validate_spec(await actions.draft_campaign(brief))
    return web.json_response({"spec": spec, "errors": errors})


# ---------------------------------------------------------------- script tracking

def _site_view(record: dict) -> dict:
    job = tracking.JOBS.get(record["host"], {})
    return {**record, "job": {"running": job.get("running", False), "log": job.get("log", [])[-20:]}}


@routes.get("/api/tracking")
async def tracking_view(request: web.Request) -> web.Response:
    dev, qa = AGENTS["developer"], AGENTS["qa"]
    return web.json_response({
        "sites": [_site_view(r) for r in sorted(tracking.sites().values(), key=lambda r: r["host"])],
        "postback": config.BEMOB_POSTBACK_URL,
        "maker": llm.label(dev), "checker": llm.label(qa),
        "maker_chain": [llm.provider_name(p) for p in llm.chain(dev)],
        "checker_chain": [llm.provider_name(p) for p in llm.chain(qa)],
        "landing_pages": config.LANDING_PAGE_URLS,
    })


@routes.post("/api/tracking/build")
async def tracking_build(request: web.Request) -> web.Response:
    """Buat (ulang) script. `url` boleh alamat website atau link campaign BeMob."""
    body = await body_of(request)
    url, hints = str(body.get("url") or "").strip(), str(body.get("hints") or "").strip()
    if not url.startswith(("http://", "https://")):
        return web.json_response({"error": "Tulis alamat website (diawali https://)."}, status=400)
    if not api_key_ok():
        return web.json_response({"error": "Belum ada API key AI. Isi di menu Pengaturan."}, status=400)
    host, site_url, lander_url = await tracking.locate(url)
    if not host:
        return web.json_response({"error": "Website tujuan link itu tidak bisa ditemukan."}, status=400)
    record = tracking.site(host) or {}
    tracking.register(url, site_url=site_url if not record else record.get("url", site_url),
                      hints=hints if "hints" in body else "", lander_url=lander_url)
    if not tracking.start_build(team_of(request), host):
        return web.json_response({"error": f"Script untuk {host} sedang dibuat."}, status=409)
    return web.json_response({"ok": True, "host": host})


@routes.post("/api/tracking/test")
async def tracking_test(request: web.Request) -> web.Response:
    host = str((await body_of(request)).get("host") or "")
    result = await tracking.test(team_of(request), host)
    if result is None:
        return web.json_response({"error": "Website tidak dikenal."}, status=404)
    return web.json_response(result)


@routes.post("/api/tracking/lander")
async def tracking_lander(request: web.Request) -> web.Response:
    """Tambah/cek landing page di depan website, atau hapus (remove=true)."""
    body = await body_of(request)
    host, url = str(body.get("host") or ""), str(body.get("url") or "").strip()
    click = str(body.get("click_url") or "").strip()
    record = tracking.site(host)
    if not record:
        return web.json_response({"error": "Website tidak dikenal."}, status=404)
    if body.get("remove"):
        landers = dict(record.get("landers") or {})
        landers.pop(url, None)
        tracking._update(host, landers=landers)
        return web.json_response({"ok": True})
    if not url.startswith(("http://", "https://")) or (click and not click.startswith(("http://", "https://"))):
        return web.json_response({"error": "Alamat landing page dan Click URL harus diawali https://"}, status=400)
    tracking.register("", site_url=record["url"], lander_url=url, click=click)
    return web.json_response(await tracking.check_lander(team_of(request), host, url))


@routes.get("/api/landers")
async def landers_view(request: web.Request) -> web.Response:
    items = sorted(landing.all_landers().values(), key=lambda r: -int(r["id"]))
    return web.json_response({
        "landers": [{**r, "building": r["id"] in landing.BUILDING} for r in items],
        "sites": sorted(tracking.sites()),
        "writer": llm.label(AGENTS["creative"]), "maker": llm.label(AGENTS["developer"]),
        "checker": llm.label(AGENTS["qa"]),
    })


@routes.post("/api/landers")
async def landers_create(request: web.Request) -> web.Response:
    """Owner minta tim AI membuat landing page. `site` = host atau alamat website tujuan."""
    body = await body_of(request)
    site, brief = str(body.get("site") or "").strip(), str(body.get("brief") or "").strip()
    if not api_key_ok():
        return web.json_response({"error": "Belum ada API key AI. Isi di menu Pengaturan."}, status=400)
    host = tracking.host_of(site) if site.startswith(("http://", "https://")) else site.lower()
    if not host:
        return web.json_response({"error": "Pilih website tujuan."}, status=400)
    if not tracking.site(host):
        tracking.register("", site_url=site if site.startswith("http") else f"https://{host}/")
    record = landing.request(team_of(request), host, brief, "Owner (dashboard)")
    return web.json_response({"ok": True, "id": record["id"]})


@routes.post("/api/landers/{id}/{action}")
async def landers_action(request: web.Request) -> web.Response:
    lp_id, action = request.match_info["id"], request.match_info["action"]
    body = await body_of(request)
    if not landing.get(lp_id):
        return web.json_response({"error": "Landing page tidak dikenal."}, status=404)
    if action == "online":
        url = str(body.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            return web.json_response({"error": "Tulis alamat landing page yang sudah online (diawali https://)."},
                                     status=400)
        return web.json_response(await landing.mark_online(team_of(request), lp_id, url))
    if action == "revise":
        if not landing.revise(team_of(request), lp_id, str(body.get("feedback") or "").strip()):
            return web.json_response({"error": "Landing page ini sedang dibuat."}, status=409)
        return web.json_response({"ok": True})
    if action == "delete":
        data = landing.all_landers()
        data.pop(lp_id, None)
        storage.put(landing.KEY, data)
        return web.json_response({"ok": True})
    return web.json_response({"error": "Tindakan tidak dikenal."}, status=400)


@routes.post("/api/tracking/delete")
async def tracking_delete(request: web.Request) -> web.Response:
    host = str((await body_of(request)).get("host") or "")
    data = tracking.sites()
    if data.pop(host, None) is None:
        return web.json_response({"error": "Website tidak dikenal."}, status=404)
    storage.put(tracking.SITES_KEY, data)
    return web.json_response({"ok": True})


@routes.post("/api/rollerads/autopause/run")
async def rollerads_autopause_run(request: web.Request) -> web.Response:
    if not config.AUTOPAUSE_ENABLED:
        return web.json_response({"error": "Auto-pause sedang mati. Aktifkan di menu Pengaturan."}, status=400)
    return web.json_response({"paused": await autopause.run(team_of(request))})


# ---------------------------------------------------------------- pengaturan (admin)

@routes.get("/api/settings")
async def settings_view(request: web.Request) -> web.Response:
    return web.json_response({
        "groups": settings.view(),
        "restart_supported": bool(os.getenv("AIADS_LAUNCHER")),
        "pending_restart": request.app.get("pending_restart", []),
    })


@routes.post("/api/settings")
async def settings_save(request: web.Request) -> web.Response:
    try:
        changed = settings.save((await body_of(request)).get("values", {}))
    except settings.SettingsError as e:
        return web.json_response({"error": str(e)}, status=400)
    request["audit"] = "pengaturan diubah: " + ", ".join(changed) if changed else "tidak ada perubahan"
    if "DASHBOARD_VIEWER_PASSWORD" in changed:  # berlaku langsung, tanpa restart
        config.DASHBOARD_VIEWER_PASSWORD = settings.current().get("DASHBOARD_VIEWER_PASSWORD", "")
        changed = [k for k in changed if k != "DASHBOARD_VIEWER_PASSWORD"]
    if "DASHBOARD_PASSWORD" in changed:
        # Berlaku langsung (tanpa restart): perangkat lain yang login dengan password lama dikeluarkan.
        config.DASHBOARD_PASSWORD = settings.current().get("DASHBOARD_PASSWORD", "")
        auth.end_all_sessions(keep=_token(request))
        changed = [k for k in changed if k != "DASHBOARD_PASSWORD"]
    pending = sorted(set(request.app.get("pending_restart", [])) | set(changed))
    request.app["pending_restart"] = pending
    return web.json_response({"changed": changed, "pending_restart": pending})


@routes.post("/api/restart")
async def restart(request: web.Request) -> web.Response:
    if not os.getenv("AIADS_LAUNCHER"):
        return web.json_response(
            {"error": "Restart otomatis hanya bisa jika program dibuka lewat jalankan.bat. "
                      "Tutup jendela program lalu jalankan lagi."}, status=400)
    log.warning("Program dijalankan ulang dari dashboard untuk memakai pengaturan baru.")
    asyncio.get_running_loop().call_later(1, os._exit, RESTART_EXIT_CODE)
    return web.json_response({"ok": True})


def _values_for_test(request_values: dict) -> dict[str, str]:
    """Nilai yang sedang diisi di form (belum disimpan) + nilai tersimpan."""
    return settings.merged(request_values or {})


async def _test_claude(v: dict) -> list[dict]:
    key = v.get("ANTHROPIC_API_KEY", "")
    if settings.is_placeholder(key):
        return [{"ok": False, "text": "API key Claude belum diisi."}]
    model = v.get("CLAUDE_MODEL") or config.CLAUDE_MODEL
    client = anthropic.AsyncAnthropic(api_key=key)
    try:
        await client.messages.create(
            model=model, max_tokens=16, messages=[{"role": "user", "content": "Balas satu kata: OK"}]
        )
    except anthropic.AuthenticationError:
        return [{"ok": False, "text": "API key salah atau sudah dihapus. Buat key baru di console.anthropic.com."}]
    except anthropic.NotFoundError:
        return [{"ok": False, "text": f"Model {model} tidak tersedia untuk akun ini. Pilih model lain."}]
    except anthropic.APIStatusError as e:
        text = e.message
        if "credit" in text.lower() or "balance" in text.lower():
            text = "Saldo Claude API habis/kurang. Isi saldo di console.anthropic.com → Billing."
        return [{"ok": False, "text": f"Claude menolak: {text}"}]
    except anthropic.APIConnectionError:
        return [{"ok": False, "text": "Tidak bisa terhubung ke Claude. Cek koneksi internet."}]
    finally:
        await client.close()
    return [{"ok": True, "text": f"API key valid dan model {model} bisa dipakai."}]


async def _test_ai(v: dict) -> list[dict]:
    """Tes API key tiap AI yang diisi, lalu tunjukkan siapa mengerjakan apa (dan cadangannya)."""
    out = []
    ready = set()
    if not settings.is_placeholder(v.get("ANTHROPIC_API_KEY", "")):
        claude = await _test_claude(v)
        out += claude
        if claude[0]["ok"]:
            ready.add("claude")
    async with httpx.AsyncClient(timeout=60) as http:
        for p in ("openai", "gemini", "deepseek", "openrouter"):
            info = llm.PROVIDERS[p]
            key = v.get(info["key"], "")
            if settings.is_placeholder(key):
                continue
            model = v.get(info["model"]) or llm.provider_model(p)
            body = {"model": model, "messages": [{"role": "user", "content": "Balas satu kata: OK"}]}
            body["max_completion_tokens" if p == "openai" else "max_tokens"] = 256 if p == "openai" else 16
            try:
                r = await http.post(f"{info['base']}/chat/completions", json=body,
                                    headers={"Authorization": f"Bearer {key}"})
                data = r.json()
            except (httpx.HTTPError, ValueError) as e:
                out.append({"ok": False, "text": f"{info['name']}: tidak bisa terhubung ({type(e).__name__})."})
                continue
            data = data[0] if isinstance(data, list) and data else data
            if r.status_code >= 400 or (isinstance(data, dict) and "error" in data):
                err = data.get("error") if isinstance(data, dict) else None
                msg = err.get("message") if isinstance(err, dict) else err
                out.append({"ok": False, "text": f"{info['name']} menolak (HTTP {r.status_code}): {msg or data}"})
                continue
            ready.add(p)
            out.append({"ok": True, "text": f"{info['name']}: API key valid, model {model} bisa dipakai."})
    if not ready:
        out.append({"ok": False, "text": "Belum ada AI yang siap. Isi minimal satu API key."})
        return out
    fallback = [x.strip().lower() for x in (v.get("AI_FALLBACK", "claude,openai,gemini,deepseek")).split(",") if x.strip()]
    for key, name, task in settings.AGENT_ROLES:
        chosen = (v.get(f"AI_{key.upper()}") or "claude").lower()
        backups = [llm.provider_name(p) for p in fallback if p != chosen and p in ready]
        if chosen in ready:
            model = (v.get(f"MODEL_{key.upper()}") or v.get("OPENROUTER_MODEL") or config.OPENROUTER_MODEL) \
                if chosen == "openrouter" else ""
            text = f"{name} ({task}) memakai {llm.provider_name(chosen)}" + (f" · {model}" if model else "")
            out.append({"ok": True, "text": text + (f", cadangan: {', '.join(backups)}." if backups else ", tanpa cadangan.")})
        else:
            out.append({"ok": False, "text": f"{name} memilih {llm.provider_name(chosen)} tetapi API key-nya belum siap"
                        + (f"; sementara dikerjakan {backups[0]}." if backups else "; tidak ada AI cadangan yang siap.")})
    return out


async def _tg(http: httpx.AsyncClient, token: str, method: str, **params) -> dict:
    try:
        r = await http.get(f"https://api.telegram.org/bot{token}/{method}", params=params)
        return r.json()
    except (httpx.HTTPError, ValueError) as e:
        return {"ok": False, "description": f"tidak bisa terhubung ke Telegram ({type(e).__name__})"}


async def _test_telegram(v: dict) -> list[dict]:
    out = []
    leader_id = None
    async with httpx.AsyncClient(timeout=15) as http:
        for key, label in settings.BOT_TOKENS:
            token = v.get(key, "")
            if not token:
                if key == "BOT_TOKEN_HEAD_MARKETING":
                    out.append({"ok": False, "text": "Token bot Head of Marketing wajib diisi."})
                continue
            me = await _tg(http, token, "getMe")
            if not me.get("ok"):
                out.append({"ok": False, "text": f"{label}: token ditolak Telegram ({me.get('description')})."})
                continue
            bot = me["result"]
            out.append({"ok": True, "text": f"{label}: terhubung sebagai @{bot['username']}"})
            if key == "BOT_TOKEN_HEAD_MARKETING":
                leader_id = bot["id"]
                if not bot.get("can_read_all_group_messages"):
                    out.append({"ok": False, "text": "Bot Head of Marketing belum bisa membaca pesan grup. Di @BotFather: "
                                                     "/setprivacy → pilih bot ini → Disable."})

        group, leader_token = v.get("TELEGRAM_GROUP_ID", ""), v.get("BOT_TOKEN_HEAD_MARKETING", "")
        if not group:
            out.append({"ok": False, "text": "ID grup belum diisi. Pakai tombol Deteksi."})
        elif leader_id:
            chat = await _tg(http, leader_token, "getChat", chat_id=group)
            if not chat.get("ok"):
                out.append({"ok": False, "text": f"Bot Head of Marketing tidak menemukan grup {group}. "
                                                 "Pastikan bot sudah dimasukkan ke grup."})
            else:
                info = chat["result"]
                out.append({"ok": True, "text": f"Grup ditemukan: {info.get('title')}"})
                if not info.get("is_forum"):
                    out.append({"ok": False, "text": "Fitur Topics di grup belum aktif. Buka pengaturan grup → aktifkan Topics."})
                member = await _tg(http, leader_token, "getChatMember", chat_id=group, user_id=leader_id)
                m = member.get("result", {})
                if m.get("status") != "administrator":
                    out.append({"ok": False, "text": "Bot Head of Marketing belum menjadi admin grup."})
                elif not m.get("can_manage_topics"):
                    out.append({"ok": False, "text": "Bot Head of Marketing admin, tetapi belum punya izin 'Manage Topics'."})
                else:
                    out.append({"ok": True, "text": "Bot Head of Marketing admin dengan izin Manage Topics."})
    if not v.get("OWNER_TELEGRAM_IDS"):
        out.append({"ok": False, "text": "ID Owner belum diisi. Pakai tombol Deteksi."})
    return out


def _check_columns(found: list[str], v: dict) -> list[dict]:
    lowered = {c.strip().lower() for c in found}
    out = []
    for key in ("COL_CAMPAIGN", "COL_ZONE", "COL_VISITS", "COL_CONVERSIONS", "COL_COST", "COL_REVENUE"):
        name = v.get(key, "")
        if name.lower() in lowered:
            out.append({"ok": True, "text": f"Kolom '{name}' ditemukan."})
        else:
            out.append({"ok": False, "text": f"Kolom '{name}' ({settings.FIELDS[key]['label']}) tidak ada. "
                                             f"Kolom yang tersedia: {', '.join(found) or '-'}"})
    return out


async def _test_rollerads(v: dict) -> list[dict]:
    token = v.get("ROLLERADS_API_KEY", "")
    if not token:
        return [{"ok": False, "text": "Token API RollerAds wajib diisi."}]
    try:
        me = await rollerads.account(token)
        campaigns = await rollerads.campaigns(token)
    except rollerads.RollerAdsError as e:
        return [{"ok": False, "text": str(e)}]
    out = [{"ok": True, "text": f"Terhubung ke RollerAds: {me['title']}, saldo ${me['balance']:,.2f}."}]
    access, secret = v.get("BEMOB_ACCESS_KEY", ""), v.get("BEMOB_SECRET_KEY", "")
    if access and secret:
        try:
            sources = await bemob.traffic_sources(access, secret)
        except bemob.BeMobError as e:
            out.append({"ok": False, "text": f"BeMob API: {e}"})
        else:
            roller = [s for s in sources if "roller" in s.get("name", "").lower()]
            out.append({"ok": True, "text": "BeMob API terhubung: pendapatan & konversi diambil dari BeMob."})
            out.append({"ok": bool(roller and roller[0].get("postbackUrl")),
                        "text": "Postback BeMob → RollerAds terpasang." if roller and roller[0].get("postbackUrl")
                        else "Traffic source RollerAds di BeMob belum punya Postback URL."})
    else:
        out.append({"ok": False, "text": "BeMob API belum diisi: pendapatan memakai payout per konversi."})
    if campaigns:
        out.append({"ok": True, "text": f"{len(campaigns)} campaign dibaca (archived dilewati): "
                                        + ", ".join(c["title"] for c in campaigns.values())})
    else:
        out.append({"ok": False, "text": "Belum ada campaign active/paused/stopped. Campaign archived tidak dibaca."})
    return out


async def _test_data(v: dict) -> list[dict]:
    source = v.get("DATA_SOURCE", "demo")
    if source == "demo":
        return [{"ok": True, "text": "Memakai data demo (angka contoh). Tidak ada yang perlu dites."}]
    if source == "csv":
        files = sorted(data_source.INBOX_DIR.glob("*.csv"), key=lambda p: p.stat().st_mtime)
        if not files:
            return [{"ok": False, "text": "Belum ada file CSV. Unggah laporan BeMob di menu Bantuan."}]
        with files[-1].open(encoding="utf-8-sig", newline="") as f:
            header = next(csv.reader(f), [])
        return [{"ok": True, "text": f"Membaca file terbaru: {files[-1].name}"}, *_check_columns(header, v)]
    if source == "rollerads":
        return await _test_rollerads(v)
    url, key = v.get("BEMOB_REPORT_URL", ""), v.get("BEMOB_API_KEY", "")
    if not url or not key:
        return [{"ok": False, "text": "URL laporan dan API key BeMob wajib diisi."}]
    today = dt.datetime.now(config.TIMEZONE).date().isoformat()
    try:
        async with httpx.AsyncClient(timeout=30) as http:
            r = await http.get(url.format(date_from=today, date_to=today),
                               headers={v.get("BEMOB_AUTH_HEADER") or "Authorization": key})
        r.raise_for_status()
        payload = r.json()
    except (httpx.HTTPError, ValueError, KeyError, IndexError) as e:
        return [{"ok": False, "text": f"Gagal mengambil data BeMob: {e}"}]
    rows_key = v.get("BEMOB_ROWS_KEY", "")
    records = payload.get(rows_key, []) if isinstance(payload, dict) and rows_key else payload
    if not isinstance(records, list):
        keys = ", ".join(payload) if isinstance(payload, dict) else type(payload).__name__
        return [{"ok": False, "text": f"Daftar baris tidak ditemukan. Kunci JSON yang ada: {keys}"}]
    if not records:
        return [{"ok": True, "text": "Terhubung ke BeMob, tetapi belum ada data hari ini."}]
    return [{"ok": True, "text": f"Terhubung ke BeMob: {len(records)} baris data hari ini."},
            *_check_columns(list(records[0]), v)]


@routes.post("/api/settings/test")
async def settings_test(request: web.Request) -> web.Response:
    body = await body_of(request)
    values = _values_for_test(body.get("values"))
    tests = {"claude": _test_claude, "ai": _test_ai, "telegram": _test_telegram, "data": _test_data}
    what = body.get("what")
    if what not in tests:
        return web.json_response({"error": "Tes tidak dikenal."}, status=400)
    return web.json_response({"results": await tests[what](values)})


@routes.post("/api/settings/detect")
async def settings_detect(request: web.Request) -> web.Response:
    """Cari ID grup & ID Owner dari pesan terakhir yang diterima bot."""
    team = team_of(request)
    if LEADER in team.bots:  # bot sedang berjalan: ia mencatat grup & pengguna yang dilihatnya
        groups, users = storage.get("seen_chats", {}), storage.get("seen_users", {})
    else:
        token = _values_for_test((await body_of(request)).get("values")).get("BOT_TOKEN_HEAD_MARKETING", "")
        if not token:
            return web.json_response({"error": "Isi token bot Head of Marketing dulu."}, status=400)
        async with httpx.AsyncClient(timeout=15) as http:
            updates = await _tg(http, token, "getUpdates")
        if not updates.get("ok"):
            return web.json_response({"error": f"Telegram menolak: {updates.get('description')}"}, status=400)
        groups, users = {}, {}
        for u in updates["result"]:
            msg = u.get("message") or u.get("my_chat_member") or u.get("edited_message") or {}
            chat, user = msg.get("chat", {}), msg.get("from", {})
            if chat.get("type") in ("group", "supergroup"):
                groups[str(chat["id"])] = chat.get("title", "")
            if user and not user.get("is_bot"):
                users[str(user["id"])] = " ".join(filter(None, [user.get("first_name"), user.get("last_name")]))
    return web.json_response({
        "groups": [{"id": k, "name": n} for k, n in groups.items()],
        "users": [{"id": k, "name": n} for k, n in users.items()],
    })


# ---------------------------------------------------------------- middleware & start

LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")
MAX_FAILS = 5              # salah password sebanyak ini dalam FAIL_WINDOW detik -> IP diblokir
FAIL_WINDOW = LOCK_SECONDS = 15 * 60
_fails: dict[str, list[float]] = defaultdict(list)
_locked: dict[str, float] = {}
SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",                       # tidak bisa disisipkan diam-diam di situs lain
    "Content-Security-Policy": "frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",                     # data & pengaturan tidak disimpan di cache browser/proxy
}


PUBLIC_PATHS = {"/login", "/api/login", "/api/auth/state", "/api/auth/setup",
                "/collect"}  # /collect dipanggil browser pengunjung website, dilindungi token sendiri


def _token(request: web.Request) -> str | None:
    return request.cookies.get(auth.COOKIE)


def _via_proxy(request: web.Request) -> bool:
    """Datang lewat reverse proxy (Caddy di VPS)? Proxy selalu menambahkan X-Forwarded-For."""
    return (request.remote or "") in ("127.0.0.1", "::1") and "X-Forwarded-For" in request.headers


def _client_ip(request: web.Request) -> str:
    """IP asli pengunjung. Di balik Caddy semua koneksi datang dari 127.0.0.1, jadi dipakai IP terakhir yang
    ditambahkan Caddy di X-Forwarded-For (bagian kiri bisa dipalsukan pengunjung, jadi tidak dipakai)."""
    if _via_proxy(request) and config.DASHBOARD_DOMAIN:
        return request.headers["X-Forwarded-For"].split(",")[-1].strip() or "?"
    return request.remote or "?"


def _is_local_client(request: web.Request) -> bool:
    """Benar-benar dari komputer tempat program berjalan (bukan pengunjung internet lewat proxy)."""
    return (request.remote or "") in ("127.0.0.1", "::1") and not any(
        h in request.headers for h in ("X-Forwarded-For", "X-Real-IP", "Forwarded"))


def _host_allowed(request: web.Request) -> bool:
    """Dashboard lokal hanya melayani alamat lokal (+ domain VPS jika diisi): menangkal DNS rebinding (situs jahat
    yang menyamar sebagai localhost untuk membaca/mengubah dashboard lewat browser Anda)."""
    if config.DASHBOARD_HOST not in LOCAL_HOSTS:
        return True  # dibuka ke jaringan: password wajib, jadi tetap terlindungi
    host = request.host.rsplit(":", 1)[0].strip("[]").lower() if not request.host.startswith("[") \
        else request.host[1:].split("]")[0]
    return host in LOCAL_HOSTS or (bool(config.DASHBOARD_DOMAIN) and host == config.DASHBOARD_DOMAIN)


def _locked_out(request: web.Request) -> web.Response | None:
    ip, now = _client_ip(request), time.time()
    if _locked.get(ip, 0) > now:
        return web.json_response({"error": f"Terlalu banyak percobaan login salah. Coba lagi dalam "
                                           f"{int((_locked[ip] - now) / 60) + 1} menit."}, status=429)
    return None


async def _login_failed(request: web.Request) -> None:
    ip, now = _client_ip(request), time.time()
    _fails[ip] = [t for t in _fails[ip] if now - t < FAIL_WINDOW] + [now]
    log.warning("Login dashboard salah dari %s (%d kali)", ip, len(_fails[ip]))
    if len(_fails[ip]) >= MAX_FAILS:
        _locked[ip] = now + LOCK_SECONDS
        _fails.pop(ip, None)
        try:
            await team_of(request).send("tracking", "alert", f"🔐 Dashboard: {MAX_FAILS}x password salah dari IP "
                                        f"{ip}. IP diblokir {LOCK_SECONDS // 60} menit. Jika bukan Anda, ganti "
                                        "password dashboard di Pengaturan.")
        except Exception:  # noqa: BLE001 - peringatan gagal tidak boleh membuka akses
            log.exception("Peringatan login gagal dikirim")


def _start_session(request: web.Request, response: web.Response, role: str = "owner") -> None:
    token = auth.create_session(_client_ip(request), request.headers.get("User-Agent", ""), role)
    https = request.secure or (_via_proxy(request) and request.headers.get("X-Forwarded-Proto", "") == "https")
    response.set_cookie(auth.COOKIE, token, max_age=auth.MAX_AGE_SECONDS, httponly=True, samesite="Strict",
                        secure=https, path="/")


@web.middleware
async def guard(request: web.Request, handler):
    if not _host_allowed(request):
        return web.Response(status=421, text="Alamat tidak dikenal. Buka dashboard lewat http://localhost.")
    # Tolak perintah dari situs lain (mis. halaman jahat yang mencoba mengubah pengaturan lewat browser Anda).
    origin = request.headers.get("Origin")
    if request.method != "GET" and ((origin and urlparse(origin).netloc != request.host)
                                    or request.headers.get("Sec-Fetch-Site") == "cross-site"):
        return web.json_response({"error": "Permintaan dari situs lain ditolak."}, status=403)
    role = auth.session_role(_token(request)) if auth.enabled() else None
    if request.path not in PUBLIC_PATHS and role is None:
        if request.path.startswith("/api/"):
            return web.json_response({"error": "Sesi login habis. Silakan login lagi.", "login": True}, status=401)
        raise web.HTTPFound("/login")
    if role == "viewer" and request.method != "GET" and request.path != "/api/logout":
        return web.json_response({"error": "Akun ini hanya bisa melihat, tidak bisa mengubah."}, status=403)
    request["role"] = role or "-"
    response = await _handle(request, handler)
    if request.method != "GET" and request.path not in ("/collect",) and response.status < 400:
        storage.add_audit(request.get("role", "-"), request.get("role", "-"), _client_ip(request),
                          request.path, request.get("audit", ""))
    response.headers.update(SECURITY_HEADERS)
    return response


# ---------------------------------------------------------------- login / logout

@routes.get("/login")
async def login_page(request: web.Request) -> web.StreamResponse:
    if auth.enabled() and auth.check_session(_token(request)):
        raise web.HTTPFound("/")
    return web.FileResponse(WEB_DIR / "login.html")


@routes.get("/api/auth/state")
async def auth_state(request: web.Request) -> web.Response:
    return web.json_response({"setup_needed": not auth.enabled(), "can_setup": _is_local_client(request)})


@routes.post("/api/auth/setup")
async def auth_setup(request: web.Request) -> web.Response:
    """Pertama kali: buat password. Hanya dari komputer ini dan hanya jika password belum ada."""
    if auth.enabled():
        return web.json_response({"error": "Password sudah dibuat. Silakan login."}, status=409)
    if not _is_local_client(request):
        return web.json_response({"error": "Password pertama hanya bisa dibuat dari komputer tempat program "
                                           "berjalan."}, status=403)
    body = await body_of(request)
    password = str(body.get("password") or "")
    if password != str(body.get("confirm") or ""):
        return web.json_response({"error": "Ulangi password tidak sama."}, status=400)
    problem = auth.weakness(password)
    if problem:
        return web.json_response({"error": problem}, status=400)
    settings.save({"DASHBOARD_PASSWORD": password})  # disimpan sebagai hash
    config.DASHBOARD_PASSWORD = settings.current().get("DASHBOARD_PASSWORD", "")
    log.warning("Password dashboard dibuat dari %s", _client_ip(request))
    response = web.json_response({"ok": True})
    _start_session(request, response)
    return response


@routes.post("/api/login")
async def login(request: web.Request) -> web.Response:
    locked = _locked_out(request)
    if locked:
        return locked
    if not auth.enabled():
        return web.json_response({"error": "Password belum dibuat.", "setup": True}, status=409)
    password = str((await body_of(request)).get("password") or "")
    role = auth.role_of_password(password)
    if not role:
        await _login_failed(request)
        storage.add_audit("?", "-", _client_ip(request), "login gagal", "password salah")
        return web.json_response({"error": "Password salah."}, status=401)
    _fails.pop(_client_ip(request), None)
    storage.add_audit(role, role, _client_ip(request), "login berhasil", request.headers.get("User-Agent", "")[:120])
    if role == "viewer":
        response = web.json_response({"ok": True, "role": role})
        _start_session(request, response, role)
        log.info("Login dashboard (lihat saja) dari %s", _client_ip(request))
        return response
    if not auth.is_hashed(config.DASHBOARD_PASSWORD):  # password lama (teks biasa) -> simpan sebagai hash
        try:
            settings.save({"DASHBOARD_PASSWORD": auth.hash_password(password)})
            config.DASHBOARD_PASSWORD = settings.current().get("DASHBOARD_PASSWORD", config.DASHBOARD_PASSWORD)
        except settings.SettingsError:
            log.exception("Password lama tidak bisa diubah menjadi hash")
    log.info("Login dashboard dari %s", _client_ip(request))
    response = web.json_response({"ok": True})
    _start_session(request, response)
    return response


@routes.post("/api/logout")
async def logout(request: web.Request) -> web.Response:
    auth.end_session(_token(request))
    response = web.json_response({"ok": True})
    response.del_cookie(auth.COOKIE, path="/")
    return response


@routes.get("/api/auth/sessions")
async def auth_sessions(request: web.Request) -> web.Response:
    return web.json_response({"sessions": auth.session_list(_token(request)), "role": request.get("role", "-"),
                              "viewer_enabled": bool(config.DASHBOARD_VIEWER_PASSWORD),
                              "audit": storage.list_audit(100)})


@routes.post("/api/auth/logout-others")
async def logout_others(request: web.Request) -> web.Response:
    return web.json_response({"ended": auth.end_all_sessions(keep=_token(request))})


async def _handle(request: web.Request, handler):
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except llm.LLMError as e:
        return web.json_response({"error": str(e)}, status=502)
    except settings.SettingsError as e:
        return web.json_response({"error": str(e)}, status=400)
    except rollerads.RollerAdsError as e:
        return web.json_response({"error": str(e)}, status=502)
    except Exception as e:  # noqa: BLE001 - tampilkan penyebab di dashboard, jangan matikan server
        log.exception("Error dashboard %s", request.path)
        return web.json_response({"error": f"Terjadi kesalahan: {e}"}, status=500)


async def start(team: Team) -> web.AppRunner:
    app = web.Application(middlewares=[guard], client_max_size=20 * 1024 * 1024)
    app["team"] = team
    app.add_routes(routes)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    host = config.DASHBOARD_HOST
    if host not in LOCAL_HOSTS and not auth.enabled():
        # Panel ini bisa mengubah campaign & API key: tanpa password kuat, jangan pernah dibuka ke jaringan.
        log.error("Dashboard TIDAK dibuka ke jaringan karena password belum dibuat. Sementara hanya bisa "
                  "dibuka dari komputer ini; buka dashboard untuk membuat password.")
        host = config.DASHBOARD_HOST = "127.0.0.1"
    await web.TCPSite(runner, host, config.DASHBOARD_PORT).start()
    shown = "localhost" if host in ("127.0.0.1", "0.0.0.0") else host
    log.info("Dashboard siap: http://%s:%d", shown, config.DASHBOARD_PORT)
    if host not in LOCAL_HOSTS:
        log.warning("Dashboard terbuka ke jaringan. Pastikan diakses lewat HTTPS (mis. Cloudflare Tunnel atau "
                    "reverse proxy), karena lewat http biasa password bisa disadap.")
    return runner
