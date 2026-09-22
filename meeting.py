"""Rapat tim: para agent berdiskusi di topic Diskusi, lalu usulan final dikirim ke Owner untuk disetujui."""
import asyncio
import logging
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import analysis
import config
import data_source
import landing
import llm
import memory
import rollerads
import storage
import tracking
from agents import AGENTS, LEADER
from telegram_team import Team

log = logging.getLogger(__name__)

_lock = asyncio.Lock()

# Urutan bicara dalam rapat
STEPS = [
    ("analyst", "Paparkan temuan terpenting dari data: kondisi umum, campaign terbaik/terburuk, "
                "dan zone boros atau bagus yang paling berpengaruh. Bandingkan dengan catatan tim & dampak "
                "tindakan sebelumnya: apa yang membaik/memburuk sejak keputusan terakhir."),
    ("media_buyer", "Berdasarkan paparan Analyst, usulkan tindakan konkret (blacklist zone, ubah bid, "
                    "pause/scale campaign). Sebutkan angka dan alasannya. Jika CR sebuah website rendah atau "
                    "perlu tes A/B, boleh usulkan landing page baru buatan tim (sebut website dan idenya). Patuhi arahan "
                    "Owner, lanjutkan rencana/tes yang masih berjalan, dan jangan ulangi usulan yang ditolak Owner "
                    "tanpa alasan baru."),
    ("ads_manager", "Kritisi usulan Media Buyer. Mana yang disetujui, mana yang perlu diubah atau "
                    "ditunda? Pastikan tidak melanggar batas pengaman."),
    (LEADER, "Tutup rapat: simpulkan keputusan dan prioritasnya dalam beberapa poin, lalu sampaikan "
             "bahwa usulan final akan dikirim ke Owner untuk persetujuan."),
]

ACTION_TYPES = ["blacklist_zones", "change_bid", "pause_campaign", "change_daily_budget", "create_lander", "other"]

PROPOSAL_SCHEMA = {
    "type": "object",
    "properties": {
        "actions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": ACTION_TYPES},
                    "campaign": {"type": "string"},
                    "zones": {"type": "array", "items": {"type": "string"}},
                    "current_value": {"type": "number"},
                    "new_value": {"type": "number"},
                    "reason": {"type": "string"},
                    "website": {"type": "string"},
                    "brief": {"type": "string"},
                },
                "required": ["type", "campaign", "zones", "current_value", "new_value", "reason", "website",
                             "brief"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["actions"],
    "additionalProperties": False,
}

MAX_ACTIONS = 5


def validate(actions: list[dict], snap: analysis.Snapshot) -> tuple[list[dict], list[str]]:
    """Guardrail berbasis kode: usulan yang melanggar batas atau memakai data karangan ditolak."""
    accepted, rejected = [], []
    already = storage.recently_proposed_zones()
    for action in actions[:MAX_ACTIONS]:
        kind = action["type"]
        if kind == "blacklist_zones":
            zones = [z for z in action["zones"] if z in snap.all_zones and z not in already]
            if not zones:
                rejected.append("Blacklist ditolak: zone tidak ada di data atau sudah diusulkan.")
                continue
            action = {**action, "zones": zones}
        elif kind == "change_bid":
            old, new = action["current_value"], action["new_value"]
            if old <= 0 or new <= 0:
                rejected.append(f"Ubah bid {action['campaign']} ditolak: nilai bid tidak valid.")
                continue
            change = abs(new - old) / old * 100
            if change > config.MAX_BID_CHANGE_PCT:
                rejected.append(
                    f"Ubah bid {action['campaign']} ditolak: perubahan {change:.0f}% > batas "
                    f"{config.MAX_BID_CHANGE_PCT:g}%."
                )
                continue
        elif kind == "change_daily_budget":
            if action["new_value"] > config.MAX_CAMPAIGN_DAILY_BUDGET_USD:
                rejected.append(
                    f"Budget {action['campaign']} ditolak: ${action['new_value']:g} > batas "
                    f"${config.MAX_CAMPAIGN_DAILY_BUDGET_USD:g}."
                )
                continue
        elif kind == "create_lander":
            wanted = (action.get("website") or "").lower()
            host = next((h for h in tracking.sites() if wanted and (wanted == h or wanted in h or h in wanted)), None)
            if not host:
                rejected.append(f"Usulan landing page ditolak: website '{action.get('website')}' belum terdaftar "
                                "di Script Tracking.")
                continue
            accepted.append({**action, "website": host})
            continue
        if kind != "other" and kind != "blacklist_zones" and action["campaign"] not in snap.campaigns:
            rejected.append(f"Usulan ditolak: campaign '{action['campaign']}' tidak ada di data.")
            continue
        accepted.append(action)
    return accepted, rejected


def describe_action(action: dict) -> str:
    kind = action["type"]
    if kind == "blacklist_zones":
        what = f"Blacklist {len(action['zones'])} zone di {action['campaign']}:\n" + ", ".join(action["zones"])
    elif kind == "change_bid":
        what = f"Ubah bid {action['campaign']}: ${action['current_value']:g} -> ${action['new_value']:g}"
    elif kind == "pause_campaign":
        what = f"Pause campaign {action['campaign']}"
    elif kind == "create_lander":
        what = f"Buat landing page baru (tim AI) untuk {action['website']}: {action.get('brief') or '-'}"
    elif kind == "create_campaign":
        what = rollerads.describe_spec(action["spec"])
    elif kind == "change_daily_budget":
        what = (
            f"Ubah budget harian {action['campaign']}: ${action['current_value']:g} -> "
            f"${action['new_value']:g}"
        )
    else:
        what = f"Lainnya ({action['campaign'] or '-'})"
    return f"{what}\nAlasan: {action['reason']}"


def approval_keyboard(proposal_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Setuju", callback_data=f"ap:{proposal_id}:yes"),
            InlineKeyboardButton("❌ Tolak", callback_data=f"ap:{proposal_id}:no"),
        ]]
    )


def task_keyboard(task_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("✔️ Sudah dikerjakan", callback_data=f"task:{task_id}:done")]]
    )


def is_running() -> bool:
    return _lock.locked()


async def run_meeting(team: Team, reason: str) -> None:
    if _lock.locked():
        log.info("Rapat sedang berjalan, permintaan baru dilewati.")
        return
    async with _lock:
        storage.put("last_meeting", time.time())
        try:
            snap = await analysis.latest(max_age=0)
        except data_source.DataSourceError as e:
            await team.send("analyst", "alert", f"Rapat batal, data tidak bisa diambil: {e}")
            return
        facts = "\n\n".join(filter(None, [memory.context(), analysis.facts_text(snap), tracking.status_text(),
                                            landing.status_text()]))
        await team.send(LEADER, "diskusi", f"📣 Rapat tim dimulai.\nAgenda: {reason}")

        transcript: list[str] = []
        try:
            for agent_key, instruction in STEPS:
                agent = AGENTS[agent_key]
                prompt = (
                    f"{facts}\n\nTRANSKRIP RAPAT SEJAUH INI:\n"
                    + ("\n\n".join(transcript) or "(belum ada)")
                    + f"\n\nGILIRAN ANDA ({agent.name}). {instruction}"
                )
                reply = await llm.ask(agent, prompt)
                transcript.append(f"{agent.name}: {reply}")
                await team.send(agent_key, "diskusi", reply)

            result = await llm.ask(
                AGENTS["ads_manager"],
                f"{facts}\n\nTRANSKRIP RAPAT:\n" + "\n\n".join(transcript)
                + "\n\nUbah keputusan final rapat menjadi daftar tindakan terstruktur (maks "
                f"{MAX_ACTIONS}). Hanya tindakan yang disetujui Head of Marketing. Gunakan ID zone dan "
                "nama campaign persis seperti di data. Untuk create_lander isi website (host dari Status tracking) "
                "dan brief (ide landing page). Isi field yang tidak relevan dengan string "
                "kosong, list kosong, atau 0. Jika tidak ada tindakan, kembalikan list kosong.",
                schema=PROPOSAL_SCHEMA,
            )
        except llm.LLMError as e:
            await team.send(LEADER, "alert", f"Rapat terhenti karena masalah AI: {e}")
            return

        accepted, rejected = validate(result["actions"], snap)
        if rejected:
            await team.send("ads_manager", "approval", "Usulan yang otomatis ditolak sistem pengaman:\n- "
                            + "\n- ".join(rejected))
        sent = []
        for action in accepted:
            proposal_id = storage.add_proposal(action)
            sent.append(f"#{proposal_id} {describe_action(action).splitlines()[0]}")
            await team.send(
                LEADER,
                "approval",
                f"Usulan #{proposal_id} menunggu persetujuan Owner\n\n{describe_action(action)}",
                reply_markup=approval_keyboard(proposal_id),
                via_leader=True,
            )
        if not accepted:
            await team.send(LEADER, "approval", "Rapat selesai. Tidak ada tindakan yang perlu disetujui.")
        log.info("Rapat selesai: %d usulan menunggu persetujuan", len(accepted))

        # Catat hasil rapat ke ingatan tim supaya rapat berikutnya melanjutkan, bukan mulai dari nol.
        try:
            await memory.consolidate(
                f"Agenda: {reason}\n\n" + "\n\n".join(transcript)
                + "\n\nUsulan dikirim ke Owner:\n- " + ("\n- ".join(sent) or "(tidak ada)")
                + ("\n\nDitolak sistem pengaman:\n- " + "\n- ".join(rejected) if rejected else ""),
                f"rapat {time.strftime('%d-%m %H:%M')}")
        except llm.LLMError as e:
            log.warning("Ingatan tim tidak diperbarui setelah rapat: %s", e)
