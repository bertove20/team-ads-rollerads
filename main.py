"""Titik masuk: menjalankan tim AI 24 jam di grup Telegram + dashboard web.

Jalankan:  python main.py
"""
import asyncio
import datetime as dt
import functools
import logging
import os
import re
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

import actions
import analysis
import autopause
import autoscale
import breakdown
import checks
import config
import creatives
import dashboard
import data_source
import landing
import llm
import meeting
import memory
import rollerads
import storage
import tracking
import watch
from actions import open_tasks_text
from agents import AGENTS, LEADER
from telegram_team import Team

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(config.DATA_DIR / "team.log", encoding="utf-8"),
    ],
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("main")

HELP_TEXT = """Perintah untuk Owner:
/setup - buat semua topic di grup (sekali saja)
/rapat [agenda] - mulai rapat tim sekarang
/laporan - laporan performa sekarang
/cek - QA & cek landing page sekarang
/kreatif [brief] - minta ide kreatif iklan
/tugas - daftar tugas manual yang belum selesai
/campaign - daftar campaign RollerAds + tombol pause/aktifkan
/campaignbaru [permintaan] - minta Media Buyer menyiapkan campaign baru (butuh persetujuan)
/script [url website] [catatan] - Developer membuat script tracking untuk website, lalu QA mengetesnya
/testracking [domain] - QA mengetes koneksi script tracking (tanpa domain = semua website)
/landingbaru [url website] [ide] - tim AI membuat landing page (file HTML untuk Anda upload)
/lponline [nomor] [alamat] - beri tahu landing page buatan tim sudah online, lalu dicek otomatis
/ingatan - lihat arahan & catatan yang diingat tim
/lupakan [nomor] - hapus satu catatan dari ingatan tim
Tulis "ingat: ..." di grup untuk menyimpan arahan permanen (mis. ingat: jangan naikkan bid di atas $2)
/biaya - estimasi biaya AI hari ini & bulan ini
/pause - hentikan rapat & laporan otomatis (cek LP tetap jalan)
/lanjut - jalankan lagi otomatisasi
/id - lihat ID grup, topic, dan user

Ngobrol dengan agent: mention bot-nya (mis. @nama_bot_analyst ...) atau reply pesannya.
Minta campaign baru ke agent: mis. "@bot_media_buyer buatkan campaign popunder ID bid 1.5 budget 20 ke https://..."
Setiap campaign baru otomatis dibuatkan script tracking untuk website tujuannya (dashboard → 🔌 Script Tracking).
Campaign yang boros/hasilnya jelek di-pause otomatis (atur di dashboard → Pengaturan → Auto-pause)."""

# "buat campaign ...", "bikinin campaign ...", "create campaign ..." -> alur pembuatan campaign
CREATE_CAMPAIGN = re.compile(r"\b(buat|buatkan|bikin|bikinin|create)\b.{0,40}\bcampaign\b", re.I | re.S)
# "ingat: ...", "catat: ..." -> arahan Owner disimpan permanen di ingatan tim
REMEMBER = re.compile(r"^\s*(?:ingat|catat|ingatlah|tolong ingat)\s*:\s*(.{3,})", re.I | re.S)
# "buatkan landing page ...", "bikin LP ..." -> tim AI membuat landing page
CREATE_LANDER = re.compile(r"\b(buat|buatkan|bikin|bikinin|create)\b.{0,40}\b(landing ?page|lander|lp)\b", re.I | re.S)


def team_of(context: ContextTypes.DEFAULT_TYPE) -> Team:
    return context.application.bot_data["team"]


def is_owner(update: Update) -> bool:
    return bool(update.effective_user and update.effective_user.id in config.OWNER_IDS)


def paused() -> bool:
    return bool(storage.get("paused", False))


def owner_only(handler):
    @functools.wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_owner(update):
            await update.effective_message.reply_text("Perintah ini hanya untuk Owner.")
            return
        return await handler(update, context)

    return wrapper


def thread_of(message: Message) -> int | None:
    return message.message_thread_id if message.is_topic_message else None


def topic_key_of(thread_id: int | None) -> str | None:
    for key, value in storage.get("topics", {}).items():
        if value == thread_id:
            return key
    return None


# ---------------------------------------------------------------- jobs

async def job_health(context: ContextTypes.DEFAULT_TYPE) -> None:
    await checks.health_check(team_of(context))
    try:
        await tracking.monitor(team_of(context))
    except Exception:  # noqa: BLE001 - pemantauan script tidak boleh menghentikan cek landing page
        log.exception("Pemantauan script tracking gagal")
    await watch.run(team_of(context))       # biaya AI, saldo, moderasi campaign, selisih konversi
    await autoscale.run(team_of(context))   # usulan menaikkan budget/bid campaign yang untung


async def job_analyst(context: ContextTypes.DEFAULT_TYPE, force: bool = False) -> None:
    if paused() and not force:
        return
    team = team_of(context)
    try:
        snap = await analysis.latest(max_age=0)
    except data_source.DataSourceError as e:
        if force or storage.get("last_data_error") != str(e):
            await team.send("analyst", "alert", f"⚠️ Data tidak bisa diambil: {e}")
        storage.put("last_data_error", str(e))
        return
    storage.put("last_data_error", None)
    storage.put("last_facts", analysis.facts_text(snap))
    try:  # rincian per negara/device/jam (1 panggilan API, tanpa AI)
        await breakdown.collect()
        await creatives.collect()
    except Exception:  # noqa: BLE001 - rincian gagal tidak boleh menghentikan laporan
        log.exception("Rincian negara/device/jam gagal diambil")
    await team.send("analyst", "laporan", analysis.hourly_report(snap))

    today = dt.datetime.now(config.TIMEZONE).date().isoformat()
    if snap.over_budget and storage.get("budget_alert_day") != today:
        storage.put("budget_alert_day", today)
        await team.send(
            LEADER,
            "alert",
            f"🚨 Spend hari ini ${snap.total.cost:.2f} sudah melewati batas "
            f"${config.MAX_DAILY_SPEND_USD:g}. Owner, mohon cek dan pause campaign bila perlu.",
        )

    # Rapat darurat jika banyak zone boros baru
    proposed = storage.recently_proposed_zones()
    new_bad = [z for z in snap.bad_zones if z.zone not in proposed]
    waste = sum(z.cost for z in new_bad)
    gap_ok = time.time() - storage.get("last_meeting", 0) >= config.MEETING_MIN_GAP_MINUTES * 60
    if config.GROUP_ID and not force and new_bad and waste >= config.ZONE_WASTE_USD * 3 and gap_ok:
        await meeting.run_meeting(
            team, f"Rapat darurat: {len(new_bad)} zone boros baru menghabiskan ${waste:.2f}"
        )


async def job_autopause(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await autopause.run(team_of(context))
    except rollerads.RollerAdsError as e:
        log.error("Auto-pause gagal memeriksa campaign: %s", e)


async def job_meeting(context: ContextTypes.DEFAULT_TYPE) -> None:
    if config.GROUP_ID and not paused():
        await meeting.run_meeting(team_of(context), "Rapat rutin terjadwal")


async def job_daily_report(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not config.GROUP_ID or paused():
        return
    team = team_of(context)
    try:
        report = await actions.daily_report()
    except llm.LLMError as e:
        await team.send(LEADER, "alert", f"Laporan harian gagal dibuat: {e}")
        return
    await team.send(LEADER, "laporan", f"🗓️ Laporan harian\n\n{report}")
    try:  # usulan dari rincian negara/device/jam (sekali sehari, tetap butuh Setuju Owner)
        await breakdown.run(team)
    except Exception:  # noqa: BLE001
        log.exception("Usulan dari rincian gagal dibuat")
    try:  # usulan dari performa creative
        await creatives.run(team)
    except Exception:  # noqa: BLE001
        log.exception("Usulan creative gagal dibuat")
    try:  # rangkum obrolan & laporan hari ini ke ingatan tim
        await memory.consolidate_chat()
    except llm.LLMError as e:
        log.warning("Ingatan tim tidak diperbarui: %s", e)


async def job_qa(context: ContextTypes.DEFAULT_TYPE) -> None:
    await checks.qa_report(team_of(context))


async def job_daily_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    if config.GROUP_ID:
        await team_of(context).send(LEADER, "reminder", f"⏰ {context.job.data}")


async def job_task_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not config.GROUP_ID:
        return
    team = team_of(context)
    for task in storage.open_tasks():
        if time.time() - task["last_reminded"] >= config.TASK_REMIND_HOURS * 3600:
            await team.send(
                "media_buyer",
                "reminder",
                f"⏰ Pengingat tugas #{task['id']} belum dikerjakan:\n{task['text']}",
                reply_markup=meeting.task_keyboard(task["id"]),
                via_leader=True,
            )
            storage.mark_task_reminded(task["id"])


# ---------------------------------------------------------------- commands

async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    await msg.reply_text(
        f"ID grup: {msg.chat_id}\nID topic: {thread_of(msg)}\nID Anda: {update.effective_user.id}"
    )


@owner_only
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(HELP_TEXT)


@owner_only
async def cmd_setup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.id != config.GROUP_ID:
        await update.effective_message.reply_text(
            f"Isi TELEGRAM_GROUP_ID={update.effective_chat.id} di .env, restart, lalu /setup lagi."
        )
        return
    try:
        created = await team_of(context).setup_topics()
    except Exception as e:  # noqa: BLE001 - tampilkan penyebab ke Owner
        await update.effective_message.reply_text(
            f"Gagal membuat topic: {e}\nPastikan grup memakai Topics dan bot ini admin dengan izin "
            "'Manage Topics'."
        )
        return
    await update.effective_message.reply_text(
        "Topic dibuat: " + ", ".join(created) if created else "Semua topic sudah ada."
    )


@owner_only
async def cmd_rapat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    agenda = " ".join(context.args) or "Rapat atas permintaan Owner"
    await update.effective_message.reply_text("Siap, rapat dimulai di topic Diskusi Strategi.")
    context.application.create_task(meeting.run_meeting(team_of(context), agenda))


@owner_only
async def cmd_laporan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await job_analyst(context, force=True)


@owner_only
async def cmd_cek(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text("Memeriksa landing page...")
    await checks.qa_report(team_of(context))
    await checks.health_check(team_of(context))


@owner_only
async def cmd_kreatif(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    brief = " ".join(context.args) or "Buat variasi kreatif untuk campaign yang sedang berjalan."
    team = team_of(context)
    try:
        ideas = await actions.creative_ideas(brief)
    except llm.LLMError as e:
        await update.effective_message.reply_text(f"Gagal: {e}")
        return
    await team.send("creative", "kreatif", ideas)


@owner_only
async def cmd_tugas(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(open_tasks_text())


def campaign_keyboard(campaigns: dict[int, dict]) -> InlineKeyboardMarkup | None:
    rows = [
        [InlineKeyboardButton(
            f"{'⏸️ Pause' if c['status'] == 'active' else '▶️ Aktifkan'} {c['title'][:30]}",
            callback_data=f"rc:{cid}:{'off' if c['status'] == 'active' else 'on'}",
        )]
        for cid, c in campaigns.items() if c["status"] in ("active", "paused")
    ]
    return InlineKeyboardMarkup(rows) if rows else None


@owner_only
async def cmd_campaign(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        campaigns = await rollerads.campaigns()
    except rollerads.RollerAdsError as e:
        await update.effective_message.reply_text(f"Gagal membaca campaign: {e}")
        return
    if not campaigns:
        await update.effective_message.reply_text("Belum ada campaign aktif/paused (campaign archived tidak dibaca).")
        return
    icon = {"active": "🟢", "paused": "⏸️", "stopped": "⏹️"}
    lines = [f"{icon.get(c['status'], '')} {c['title']} (#{cid}) - {c['status']}" for cid, c in campaigns.items()]
    await update.effective_message.reply_text(
        "Campaign RollerAds:\n" + "\n".join(lines), reply_markup=campaign_keyboard(campaigns)
    )


async def create_campaign_flow(team: Team, message: Message, brief: str) -> None:
    """Media Buyer menyusun campaign dari permintaan Owner, lalu dikirim sebagai usulan bertombol."""
    await message.reply_text("🎯 Media Buyer sedang menyiapkan campaign...")
    try:
        spec = await actions.draft_campaign(brief)
        proposal_id, errors, clean = await actions.propose_campaign(team, spec)
    except (llm.LLMError, rollerads.RollerAdsError) as e:
        await message.reply_text(f"Gagal menyiapkan campaign: {e}")
        return
    if errors:
        await team.send("media_buyer", topic_key_of(thread_of(message)),
                        f"Campaign belum bisa diusulkan:\n- " + "\n- ".join(errors)
                        + f"\n\nDraf saat ini:\n{rollerads.describe_spec(clean)}\n\n"
                        "Lengkapi info di atas lalu minta lagi, atau buat dari dashboard (menu RollerAds).",
                        reply_to=message.message_id)
        return
    await message.reply_text(f"Usulan #{proposal_id} dikirim ke topic Campaign & Approval. Tekan Setuju untuk membuat.")


@owner_only
async def cmd_campaignbaru(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    brief = " ".join(context.args)
    if not brief:
        await update.effective_message.reply_text(
            "Tulis permintaannya, mis.:\n/campaignbaru popunder Indonesia bid 1.5 CPM budget harian 20 "
            "ke https://domain-anda.com/"
        )
        return
    await create_campaign_flow(team_of(context), update.effective_message, brief)


@owner_only
async def cmd_script(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    url = context.args[0] if context.args else (config.LANDING_PAGE_URLS[0] if config.LANDING_PAGE_URLS else "")
    if not url.startswith(("http://", "https://")):
        await update.effective_message.reply_text(
            "Tulis alamat websitenya, mis.:\n/script https://domain-anda.com/ (boleh juga link campaign BeMob)")
        return
    host, site_url, lander_url = await tracking.locate(url)
    if not host:
        await update.effective_message.reply_text("Website tujuan link itu tidak bisa ditemukan.")
        return
    tracking.register(url, site_url=site_url, hints=" ".join(context.args[1:]), lander_url=lander_url)
    if not tracking.start_build(team_of(context), host):
        await update.effective_message.reply_text(f"Script untuk {host} sedang dibuat, tunggu sebentar.")
        return
    await update.effective_message.reply_text(
        f"🛠️ Developer mulai membuat script tracking untuk {host}. Hasilnya dikirim ke topic Creative & LP, "
        "lalu QA mengetes koneksinya.")


async def create_lander_flow(team: Team, message: Message, text: str) -> None:
    """Website diambil dari URL di pesan, atau satu-satunya website yang terdaftar."""
    url = re.search(r"https?://\S+", text)
    known = sorted(tracking.sites())
    host = tracking.host_of(url.group(0)) if url else (known[0] if len(known) == 1 else "")
    if not host:
        await message.reply_text("Landing page ini untuk website mana? Tulis alamatnya, mis.:\n"
                                 "/landingbaru https://domain-anda.com/ bonus member baru, warna merah"
                                 + (f"\n\nWebsite terdaftar: {', '.join(known)}" if known else ""))
        return
    if not tracking.site(host):
        tracking.register("", site_url=url.group(0))
    brief = (text.replace(url.group(0), "") if url else text).strip()
    record = landing.request(team, host, brief, "Owner (Telegram)")
    await message.reply_text(f"🎨 Landing page #{record['id']} untuk {host} mulai dibuat. File HTML-nya dikirim ke "
                             "topic Creative & LP; setelah Anda upload, kirim /lponline "
                             f"{record['id']} <alamatnya>.")


@owner_only
async def cmd_landingbaru(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await create_lander_flow(team_of(context), update.effective_message, " ".join(context.args))


@owner_only
async def cmd_lponline(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if len(args) < 2 or not landing.get(args[0].lstrip("#")) or not args[1].startswith(("http://", "https://")):
        await update.effective_message.reply_text("Tulis nomor landing page dan alamatnya, mis.:\n"
                                                  "/lponline 3 https://promo.domain-anda.com/")
        return
    await update.effective_message.reply_text("🧪 QA mengecek landing page-nya…")
    await landing.mark_online(team_of(context), args[0].lstrip("#"), args[1])


@owner_only
async def cmd_ingatan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = memory.notes_text(3800, ids=True)
    await update.effective_message.reply_text(
        (text or "Ingatan tim masih kosong.") + "\n\nTambah arahan: tulis \"ingat: ...\" di grup. Hapus: /lupakan <nomor>.")


@owner_only
async def cmd_lupakan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    arg = (context.args[0] if context.args else "").lstrip("#")
    if not arg.isdigit():
        await update.effective_message.reply_text("Tulis nomor catatannya, mis. /lupakan 3")
        return
    await update.effective_message.reply_text("🧠 Dihapus dari ingatan tim." if memory.forget(int(arg))
                                              else "Nomor itu tidak ada.")


@owner_only
async def cmd_testracking(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    wanted = (context.args[0] if context.args else "").lower()
    hosts = [h for h in tracking.sites() if not wanted or wanted in h]
    if not hosts:
        await update.effective_message.reply_text("Belum ada website dengan script tracking. Buat dengan /script <url>.")
        return
    await update.effective_message.reply_text(f"🧪 QA mengetes {', '.join(hosts)}…")
    for host in hosts:
        await tracking.test(team_of(context), host)


@owner_only
async def cmd_biaya(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    today, _ = llm.cost_since(llm.start_of_day_ts())
    week, _ = llm.cost_since(time.time() - 7 * 86400)
    month, lines = llm.cost_since(llm.start_of_month_ts())
    await update.effective_message.reply_text(
        f"💰 Pemakaian AI\nHari ini: ${today:.2f}\n7 hari: ${week:.2f} (perkiraan sebulan ${week / 7 * 30:.2f})\n"
        f"Bulan ini: ${month:.2f}\n\nPer model bulan ini:\n" + "\n".join(lines)
        + f"\n\n=== 7 HARI TERAKHIR ===\n{llm.usage_report(time.time() - 7 * 86400)}"
        + "\n\nRincian lengkap ada di dashboard → 💰 Biaya AI."
    )


@owner_only
async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    storage.put("paused", True)
    await update.effective_message.reply_text(
        "⏸️ Rapat, laporan, dan AI otomatis dihentikan. Cek landing page tetap berjalan. /lanjut untuk "
        "menyalakan lagi.\nAuto-pause campaign RollerAds tetap berjalan. Untuk pause campaign: /campaign"
    )


@owner_only
async def cmd_lanjut(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    storage.put("paused", False)
    await update.effective_message.reply_text("▶️ Otomatisasi berjalan lagi.")


# ---------------------------------------------------------------- tombol & obrolan

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not is_owner(update):
        await query.answer("Hanya Owner yang bisa memutuskan.", show_alert=True)
        return
    team = team_of(context)
    kind, item_id, decision = query.data.split(":")
    item_id = int(item_id)
    user = update.effective_user

    if kind == "ap":
        approved = decision == "yes"
        if not await actions.decide_proposal(
            team, item_id, approved, user.first_name, user.id, reply_to=query.message.message_id
        ):
            await query.answer("Usulan ini sudah diputuskan sebelumnya.")
            await query.edit_message_reply_markup(None)
            return
        await query.answer("Dicatat.")
        await query.edit_message_reply_markup(None)
    elif kind == "rc":
        status = "active" if decision == "on" else "paused"
        try:
            await actions.set_campaign_status(team, item_id, status, user.first_name)
        except rollerads.RollerAdsError as e:
            await query.answer(f"Gagal: {e}"[:190], show_alert=True)
            return
        await query.answer("Campaign diaktifkan." if status == "active" else "Campaign di-pause.")
        await query.edit_message_reply_markup(None)
    elif kind == "task":
        if not await actions.finish_task(team, item_id, reply_to=query.message.message_id):
            await query.answer("Tugas ini sudah selesai.")
            await query.edit_message_reply_markup(None)
            return
        await query.answer("Mantap, tercatat selesai.")
        await query.edit_message_reply_markup(None)


def detect_agent(message: Message, team: Team) -> str | None:
    """Agent yang diajak bicara: lewat @mention, reply ke pesannya, atau nama di awal pesan."""
    for entity, value in message.parse_entities(["mention"]).items():
        key = team.agent_by_username.get(value.lstrip("@").lower())
        if key:
            return key
    replied = message.reply_to_message
    if replied and replied.from_user and replied.from_user.is_bot:
        for key, agent in AGENTS.items():
            if (replied.text or "").startswith(f"{agent.label}:"):
                return key
        return team.agent_by_username.get((replied.from_user.username or "").lower())
    lowered = message.text.lower()
    for key, agent in AGENTS.items():
        if lowered.startswith(agent.name.lower()):
            return key
    return None


async def on_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if not message or not message.text or not user or user.is_bot:
        return
    if message.chat_id != config.GROUP_ID:
        return
    thread_id = thread_of(message)
    storage.log_chat(thread_id, user.first_name, message.text, topic_key_of(thread_id))
    if user.id not in config.OWNER_IDS:
        return  # hanya Owner yang boleh memakai kredit AI
    team = team_of(context)
    remember = REMEMBER.match(message.text)
    if remember:
        note = memory.remember(remember.group(1), source=f"Owner ({user.first_name}) di Telegram")
        await message.reply_text(f"🧠 Dicatat sebagai arahan #{note['id']}. Semua agent akan mengikutinya."
                                 if note else "🧠 Arahan itu sudah tercatat sebelumnya.")
        return
    agent_key = detect_agent(message, team)
    if not agent_key:
        return
    if CREATE_LANDER.search(message.text) and not CREATE_CAMPAIGN.search(message.text):
        await create_lander_flow(team, message, message.text)
        return
    if CREATE_CAMPAIGN.search(message.text) and config.ROLLERADS_API_KEY:
        await create_campaign_flow(team, message, message.text)
        return
    try:
        reply = await actions.ask_agent(agent_key, user.first_name, thread_id)
    except llm.LLMError as e:
        await message.reply_text(f"Maaf, {AGENTS[agent_key].name} sedang tidak bisa menjawab: {e}")
        return
    await team.send(agent_key, topic_key_of(thread_id), reply, reply_to=message.message_id)


async def remember_seen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Catat grup & pengguna yang terlihat bot, untuk tombol Deteksi ID di dashboard."""
    chat, user = update.effective_chat, update.effective_user
    if chat and chat.type in ("group", "supergroup"):
        chats = storage.get("seen_chats", {})
        if chats.get(str(chat.id)) != chat.title:
            chats[str(chat.id)] = chat.title
            storage.put("seen_chats", chats)
    if user and not user.is_bot:
        users = storage.get("seen_users", {})
        if users.get(str(user.id)) != user.full_name:
            users[str(user.id)] = user.full_name
            storage.put("seen_users", users)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Error tak terduga", exc_info=context.error)


# ---------------------------------------------------------------- startup

async def post_init(app: Application) -> None:
    team = Team(app.bot)
    await team.start()
    app.bot_data["team"] = team
    app.bot_data["dashboard"] = await dashboard.start(team)

    jq = app.job_queue
    jq.run_repeating(job_health, interval=config.HEALTHCHECK_MINUTES * 60, first=30)
    jq.run_repeating(job_analyst, interval=config.ANALYST_MINUTES * 60, first=60)
    jq.run_repeating(job_task_reminder, interval=15 * 60, first=120)
    jq.run_repeating(job_autopause, interval=config.AUTOPAUSE_MINUTES * 60, first=45)
    for t in config.MEETING_TIMES:
        jq.run_daily(job_meeting, time=t)
    jq.run_daily(job_daily_report, time=config.DAILY_REPORT_TIME)
    jq.run_daily(job_qa, time=config.QA_TIME)
    for t, text in config.DAILY_REMINDERS:
        jq.run_daily(job_daily_reminder, time=t, data=text)

    if not config.GROUP_ID:
        log.warning("TELEGRAM_GROUP_ID kosong. Tambahkan bot ke grup, ketik /id, lalu isi .env.")
    if not config.OWNER_IDS:
        log.warning("OWNER_TELEGRAM_IDS kosong. Ketik /id di grup untuk melihat ID Anda.")
    log.info("Tim AI siap. Sumber data: %s. AI per agent: %s", config.DATA_SOURCE,
             ", ".join(llm.label(a) for a in AGENTS.values()))


async def post_shutdown(app: Application) -> None:
    runner = app.bot_data.get("dashboard")
    if runner:
        await runner.cleanup()
    team: Team | None = app.bot_data.get("team")
    if team:
        await team.stop()


async def autopause_loop(team: Team) -> None:
    while True:
        await asyncio.sleep(config.AUTOPAUSE_MINUTES * 60)
        try:
            await autopause.run(team)
        except Exception:  # noqa: BLE001 - jangan sampai dashboard ikut mati
            log.exception("Auto-pause gagal")


async def run_dashboard_only() -> None:
    """Tanpa token Telegram: dashboard tetap jalan, cek landing page & auto-pause tetap otomatis."""
    team = Team(None)
    await dashboard.start(team)
    asyncio.get_running_loop().create_task(autopause_loop(team))
    while True:
        try:
            await checks.health_check(team)
            await tracking.monitor(team)
            await watch.run(team)
            await breakdown.collect()
        except Exception:  # noqa: BLE001 - jangan sampai dashboard ikut mati
            log.exception("Cek landing page gagal")
        await asyncio.sleep(config.HEALTHCHECK_MINUTES * 60)


def main() -> None:
    token = os.getenv("BOT_TOKEN_HEAD_MARKETING", "").strip()
    if not token:
        log.warning("BOT_TOKEN_HEAD_MARKETING belum diisi: berjalan dalam mode DASHBOARD SAJA "
                    "(tanpa Telegram & tanpa jadwal otomatis).")
        try:
            asyncio.run(run_dashboard_only())
        except KeyboardInterrupt:
            pass
        return

    app = (
        Application.builder()
        .token(token)
        .concurrent_updates(True)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.add_handler(TypeHandler(Update, remember_seen), group=-1)
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler(["start", "bantuan", "help"], cmd_help))
    app.add_handler(CommandHandler("setup", cmd_setup))
    app.add_handler(CommandHandler("rapat", cmd_rapat))
    app.add_handler(CommandHandler("laporan", cmd_laporan))
    app.add_handler(CommandHandler("cek", cmd_cek))
    app.add_handler(CommandHandler("kreatif", cmd_kreatif))
    app.add_handler(CommandHandler("tugas", cmd_tugas))
    app.add_handler(CommandHandler("campaign", cmd_campaign))
    app.add_handler(CommandHandler("campaignbaru", cmd_campaignbaru))
    app.add_handler(CommandHandler("script", cmd_script))
    app.add_handler(CommandHandler("testracking", cmd_testracking))
    app.add_handler(CommandHandler("landingbaru", cmd_landingbaru))
    app.add_handler(CommandHandler("lponline", cmd_lponline))
    app.add_handler(CommandHandler("ingatan", cmd_ingatan))
    app.add_handler(CommandHandler("lupakan", cmd_lupakan))
    app.add_handler(CommandHandler("biaya", cmd_biaya))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("lanjut", cmd_lanjut))
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^(ap|task|rc):\d+:\w+$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_group_message))
    app.add_error_handler(on_error)

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
