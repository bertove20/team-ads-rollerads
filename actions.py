"""Tindakan yang bisa dipicu dari Telegram maupun dashboard, supaya hasilnya selalu sama."""
import datetime as dt
import logging
import time

import autopause
import config
import landing
import llm
import meeting
import memory
import rollerads
import storage
import tracking
from agents import AGENTS, LEADER
from telegram_team import Team

log = logging.getLogger(__name__)


def _line(t: dict) -> str:
    roi = f"{t['roi']:.0f}%" if t.get("roi") is not None else "-"
    cpa = f"${t['cpa']:.2f}" if t.get("cpa") is not None else "-"
    return (f"spend ${t.get('cost', 0):.2f} | revenue ${t.get('revenue', 0):.2f} | profit ${t.get('profit', 0):.2f} | "
            f"ROI {roi} | konv {t.get('conversions', 0)} | CPA {cpa}")


def results_text() -> str:
    """Hasil akhir kemarin per campaign, tren 7 hari, dan tindakan tim 24 jam terakhir (tanpa AI)."""
    days = storage.daily_snapshots(8)
    yesterday = (dt.datetime.now(config.TIMEZONE).date() - dt.timedelta(days=1)).isoformat()
    parts = []
    y = next((d for d in days if d["day"] == yesterday), None)
    if y:
        rows = sorted(y["campaigns"].items(), key=lambda kv: -kv[1].get("cost", 0))
        parts.append(f"HASIL AKHIR KEMARIN ({yesterday}):\nTotal: {_line(y['total'])}\n"
                     + "\n".join(f"- {name}: {_line(t)}" for name, t in rows[:15]))
    else:
        parts.append(f"HASIL AKHIR KEMARIN ({yesterday}): tidak ada data tersimpan (program mungkin mati kemarin).")
    past = [d for d in days if d["day"] <= yesterday][-7:]
    if len(past) > 1:
        parts.append("TREN 7 HARI:\n" + "\n".join(f"- {d['day']}: {_line(d['total'])}" for d in past))
    since = time.time() - 86400
    done = [e for e in storage.get(autopause.LOG_KEY, []) if e.get("ts", 0) >= since]
    if done:
        parts.append("TINDAKAN 24 JAM TERAKHIR:\n" + "\n".join(
            f"- {e.get('title')}: {e.get('action')} {e.get('reason') or ''} (oleh {e.get('by')})" for e in done[-20:]))
    pending = [p for p in storage.list_proposals(50) if p["status"] == "pending"]
    if pending:
        parts.append("USULAN MENUNGGU KEPUTUSAN OWNER:\n" + "\n".join(
            f"- #{p['id']}: {meeting.describe_action(p['action']).splitlines()[0]}" for p in pending[:10]))
    return "\n\n".join(parts)


def progress_text() -> str:
    """Status tracking & landing page, supaya tim bisa membahas progresnya."""
    return "\n\n".join(filter(None, [tracking.status_text(), landing.status_text()]))


def open_tasks_text() -> str:
    tasks = storage.open_tasks()
    if not tasks:
        return "Tidak ada tugas manual yang tertunda."
    return "Tugas manual tertunda:\n" + "\n\n".join(f"#{t['id']}: {t['text']}" for t in tasks)


async def decide_proposal(
    team: Team, proposal_id: int, approved: bool, who: str, user_id: int, reply_to: int | None = None
) -> bool:
    """Catat keputusan Owner. False jika usulan sudah diputuskan sebelumnya."""
    if not storage.decide_proposal(proposal_id, "approved" if approved else "rejected", user_id):
        return False
    await team.send(
        LEADER,
        "approval",
        f"Usulan #{proposal_id} {'DISETUJUI ✅' if approved else 'DITOLAK ❌'} oleh {who}.",
        reply_to=reply_to,
    )
    if approved:
        action = storage.get_proposal(proposal_id)["action"]
        if await _execute(team, proposal_id, action):
            return True
        task_text = (
            f"Kerjakan di dashboard RollerAds (usulan #{proposal_id}):\n{meeting.describe_action(action)}"
        )
        task_id = storage.add_task(task_text, proposal_id)
        await team.send(
            "media_buyer",
            "reminder",
            f"📝 Tugas manual #{task_id} untuk Owner\n{task_text}",
            reply_markup=meeting.task_keyboard(task_id),
            via_leader=True,
        )
    return True


async def _execute(team: Team, proposal_id: int, action: dict) -> bool:
    """Jalankan usulan lewat RollerAds API bila bisa. False = tetap jadi tugas manual."""
    if action["type"] == "create_lander":
        record = landing.request(team, action["website"], action.get("brief") or action.get("reason", ""),
                                 f"usulan #{proposal_id}")
        await team.send("creative", "approval", f"🎨 Usulan #{proposal_id}: tim mulai membuat landing page "
                        f"#{record['id']} untuk {action['website']}. File-nya dikirim ke topic Creative & LP.")
        return True
    if not config.ROLLERADS_API_KEY:
        return False
    if action["type"] == "create_campaign":
        try:
            campaign_id = await rollerads.create(action["spec"])
        except rollerads.RollerAdsError as e:
            await team.send(LEADER, "approval", f"⚠️ Usulan #{proposal_id}: campaign GAGAL dibuat. {e}\n"
                            "Perbaiki lalu buat ulang dari panel (menu RollerAds).")
            return True
        autopause.record({"campaign_id": campaign_id, "title": action["spec"]["title"], "action": "created",
                          "reason": f"Usulan #{proposal_id} disetujui", "by": "Tim AI"})
        await team.send("media_buyer", "approval",
                        f"🚀 Campaign {action['spec']['title']} dibuat di RollerAds (#{campaign_id}), "
                        f"status {action['spec']['status']}. Menunggu moderasi RollerAds.\n"
                        "Developer menyiapkan script tracking untuk website tujuannya, lalu QA mengetes koneksinya.")
        tracking.start_for_campaign(team, campaign_id, action["spec"])
        return True
    if action["type"] == "pause_campaign":
        try:
            match = [c for c in (await rollerads.campaigns()).values()
                     if c["title"] == action["campaign"] and c["status"] == "active"]
            if len(match) != 1:
                return False
            await set_campaign_status(team, match[0]["id"], "paused", f"usulan #{proposal_id}")
        except rollerads.RollerAdsError as e:
            log.error("Pause dari usulan #%s gagal: %s", proposal_id, e)
            return False
        return True
    if action["type"] in ("blacklist_zones", "change_bid", "change_daily_budget"):
        try:
            return await _apply_change(team, proposal_id, action)
        except rollerads.RollerAdsError as e:
            log.error("Usulan #%s gagal dijalankan: %s", proposal_id, e)
            await team.send("media_buyer", "approval", f"⚠️ Usulan #{proposal_id} tidak bisa dijalankan otomatis: {e}\n"
                            "Dijadikan tugas manual.")
            return False
    return False


async def _apply_change(team: Team, proposal_id: int, action: dict) -> bool:
    """Blacklist zone / ubah bid / ubah budget harian lewat API. False = jadikan tugas manual."""
    kind, name = action["type"], action["campaign"]
    match = [c for c in (await rollerads.campaigns()).values() if c["title"] == name]
    if len(match) != 1:
        raise rollerads.RollerAdsError(f"campaign '{name}' tidak ditemukan (atau namanya dobel) di RollerAds")
    cid = match[0]["id"]
    who = f"Tim AI (usulan #{proposal_id} disetujui Owner)"

    if kind == "blacklist_zones":
        done = await rollerads.blacklist_zones(cid, action["zones"])
        missing = sorted(set(action["zones"]) - {str(z) for z in done})
        autopause.record({"campaign_id": cid, "title": name, "action": "blacklist",
                          "reason": f"{len(done)} zone: {', '.join(map(str, done))}", "by": who})
        await team.send("media_buyer", "approval", f"🚫 {len(done)} zone di-blacklist di {name} (#{cid}): "
                        + ", ".join(map(str, done))
                        + (f"\n⚠️ Belum masuk blacklist: {', '.join(missing)}. Cek manual di RollerAds." if missing else ""))
        return True

    current = await rollerads.detail(cid)
    if kind == "change_bid":
        old, new = float(current.get("campaign_bid") or 0), float(action["new_value"])
        # Bid di RollerAds bisa sudah berubah sejak rapat: batas perubahan dihitung ulang dari bid sekarang.
        change = abs(new - old) / old * 100 if old else 100
        if new <= 0 or new > config.CAMPAIGN_MAX_BID_USD or change > config.MAX_BID_CHANGE_PCT:
            raise rollerads.RollerAdsError(
                f"bid sekarang ${old:g}, usulan ${new:g} ({change:.0f}%) melewati batas pengaman "
                f"(maks {config.MAX_BID_CHANGE_PCT:g}% per usulan, maks ${config.CAMPAIGN_MAX_BID_USD:g})")
        await rollerads.update(cid, campaign_bid=new)
        field, label = "bid", "Bid"
    else:
        old, new = float(current.get("campaign_spent_day") or 0), float(action["new_value"])
        if new <= 0 or new > config.MAX_CAMPAIGN_DAILY_BUDGET_USD:
            raise rollerads.RollerAdsError(f"budget ${new:g} di luar batas (maks "
                                           f"${config.MAX_CAMPAIGN_DAILY_BUDGET_USD:g})")
        await rollerads.update(cid, campaign_spent_day=new)
        field, label = "budget", "Budget harian"
    autopause.record({"campaign_id": cid, "title": name, "action": field, "reason": f"${old:g} → ${new:g}", "by": who})
    await team.send("media_buyer", "approval", f"✅ {label} {name} (#{cid}) diubah ${old:g} → ${new:g}.")
    return True


async def set_campaign_status(team: Team, campaign_id: int, status: str, who: str) -> str:
    """Pause/aktifkan campaign RollerAds dan umumkan ke grup. Mengembalikan nama campaign."""
    detail = await rollerads.set_status(campaign_id, status)
    title = detail.get("campaign_title", str(campaign_id))
    word = "di-PAUSE ⏸️" if status == "paused" else "DIAKTIFKAN ▶️"
    autopause.record({"campaign_id": campaign_id, "title": title, "action": status, "reason": "", "by": who})
    await team.send(LEADER, "alert", f"Campaign {title} (#{campaign_id}) {word} oleh {who}.")
    return title


async def draft_campaign(brief: str) -> dict:
    """Media Buyer menyusun spec campaign dari permintaan bebas Owner."""
    formats = "\n".join(
        f"- format_id {f} = {name}; bid_model_id yang boleh: "
        + ", ".join(f"{b} ({rollerads.BID_MODELS[b]})" for b in rollerads.FORMAT_BID_MODELS[f])
        for f, name in rollerads.FORMATS.items()
    )
    return await llm.ask(
        AGENTS["media_buyer"],
        f"{storage.get('last_facts') or 'Belum ada data performa.'}\n\n"
        f"{memory.notes_text()}\n\n"
        f"Link campaign BeMob: {', '.join(config.TRACKING_URLS) or '-'}\n"
        f"Landing page yang dipantau: {', '.join(config.LANDING_PAGE_URLS) or '-'}\n\n"
        f"PERMINTAAN OWNER: {brief}\n\n"
        "Susun spesifikasi campaign RollerAds baru untuk permintaan ini.\n"
        f"Format & model bid yang tersedia:\n{formats}\n"
        f"Aturan: bid maks ${config.CAMPAIGN_MAX_BID_USD:g}; budget harian wajib > 0 dan maks "
        f"${config.MAX_CAMPAIGN_DAILY_BUDGET_USD:g}; negara pakai kode ISO 2 huruf (mis. ID); status 'paused' "
        "kecuali Owner jelas meminta langsung aktif; total_budget 0 jika tidak disebut. Untuk format 1, 3, 7 "
        "isi creative_title (maks 64), creative_descr (maks 128, native maks 90), image_360 dan image_192 berupa "
        "URL gambar dari Owner; jika Owner tidak memberi URL gambar, biarkan kosong. Untuk OnClick/Popunder "
        "kosongkan semua field creative. Pakai URL dari Owner; jika tidak ada, pakai link campaign BeMob, baru "
        "landing page yang dipantau jika link BeMob juga tidak ada (URL campaign sebaiknya link BeMob supaya "
        "konversi terlacak). "
        "Jangan mengarang URL. Tulis alasan singkat di field reason.",
        schema=rollerads.SPEC_SCHEMA,
    )


async def propose_campaign(team: Team, spec: dict) -> tuple[int | None, list[str], dict]:
    """Cek spec lalu kirim sebagai usulan bertombol ke Owner. Mengembalikan (id usulan, kesalahan, spec)."""
    clean, errors = await rollerads.validate_spec(spec)
    if errors:
        return None, errors, clean
    action = {"type": "create_campaign", "campaign": clean["title"], "zones": [], "spec": clean,
              "current_value": 0, "new_value": clean["bid"], "reason": clean["reason"] or "Permintaan Owner"}
    proposal_id = storage.add_proposal(action)
    await team.send(
        LEADER,
        "approval",
        f"Usulan #{proposal_id} menunggu persetujuan Owner\n\n{meeting.describe_action(action)}",
        reply_markup=meeting.approval_keyboard(proposal_id),
        via_leader=True,
    )
    return proposal_id, [], clean


async def finish_task(team: Team, task_id: int, reply_to: int | None = None) -> bool:
    """False jika tugas sudah selesai sebelumnya."""
    if not storage.finish_task(task_id):
        return False
    await team.send("media_buyer", "reminder", f"✔️ Tugas #{task_id} selesai dikerjakan.", reply_to=reply_to)
    return True


async def ask_agent(agent_key: str, who: str, thread_id: int | None = None) -> str:
    """Jawaban agent atas pesan terakhir Owner di percakapan (thread) tersebut."""
    agent = AGENTS[agent_key]
    chat = "\n".join(f"{c['speaker']}: {c['text']}" for c in storage.recent_chat(thread_id, limit=30))
    prompt = (
        f"INGATAN TIM:\n{memory.context() or '(belum ada)'}\n\n"
        f"{storage.get('last_facts') or 'Belum ada data performa terbaru.'}\n\n{open_tasks_text()}\n\n"
        f"{progress_text()}\n\n{results_text()}\n\n"
        f"PERCAKAPAN TERAKHIR DI TOPIC INI:\n{chat}\n\n"
        f"Jawab pesan terakhir dari Owner ({who}) sebagai {agent.name}. Sambungkan dengan pembahasan, keputusan, "
        "dan arahan Owner sebelumnya bila relevan (jangan bertanya ulang hal yang sudah pernah dijawab Owner)."
    )
    return await llm.ask(agent, prompt)


async def creative_ideas(brief: str) -> str:
    return await llm.ask(
        AGENTS["creative"],
        f"{storage.get('last_facts') or ''}\n\nBrief dari Owner: {brief}\n\n"
        "Buat 5 variasi iklan push/in-page push (judul maks 30 karakter, deskripsi maks 45 "
        "karakter, ide ikon/gambar), lalu 1 ide A/B test untuk landing page.",
    )


async def daily_report() -> str:
    ai_cost, _ = llm.cost_since(time.time() - 86400)
    return await llm.ask(
        AGENTS[LEADER],
        f"INGATAN TIM:\n{memory.context() or '(belum ada)'}\n\n"
        f"{storage.get('last_facts') or 'Belum ada data.'}\n\n{open_tasks_text()}\n\n"
        f"Biaya AI 24 jam terakhir: ${ai_cost:.2f}\n\n{results_text()}\n\n{progress_text()}\n\n"
        "Tulis laporan harian untuk Owner dengan urutan: (1) HASIL AKHIR KEMARIN: total profit/ROI dan campaign "
        "terbaik & terburuk, (2) dibanding tren 7 hari: membaik atau memburuk, (3) tindakan yang sudah dijalankan "
        "tim dan dampaknya jika terlihat, (4) risiko, (5) rencana hari ini, (6) progres tracking/landing page "
        "(yang belum terpasang, belum terhubung, atau menunggu di-upload Owner), (7) hal yang perlu keputusan "
        "atau tindakan Owner (usulan menunggu, script untuk ditempel, tugas manual). Data 'hari ini' baru "
        "sebagian hari, jangan dijadikan kesimpulan.",
    )
