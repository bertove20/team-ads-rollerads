"""Landing page buatan tim AI (Owner yang meng-upload ke hosting-nya sendiri).

Alur: Creative menulis isi -> Developer membuat satu file HTML (semua tombol ke Click URL BeMob) -> dicek otomatis
(tombol, domain luar, tampilan HP) -> QA me-review. Jika AI Developer gagal/ditolak, AI cadangan membantu; jika semua
gagal, dipakai template standar. Hasilnya dikirim sebagai file. Setelah Owner meng-upload dan memberi tahu alamatnya,
landing page didaftarkan ke website tujuan dan tombolnya dicek otomatis (lihat tracking.check_lander).

Dipicu Owner (dashboard / Telegram) atau usulan rapat tim yang disetujui Owner.
"""
import html as htmlmod
import logging
import re
import time

import llm
import memory
import storage
import tracking
from agents import AGENTS
from telegram_team import Team

log = logging.getLogger(__name__)

KEY = "ai_landers"
MARKER = "AIADS-LP"
MAX_MAKERS = 3
BUILDING: set[str] = set()  # id yang sedang dibuat

COPY_SCHEMA = {
    "type": "object",
    "properties": {
        "brand": {"type": "string"},
        "headline": {"type": "string"},
        "subheadline": {"type": "string"},
        "bullets": {"type": "array", "items": {"type": "string"}},
        "cta": {"type": "string"},
        "urgency": {"type": "string"},
        "trust": {"type": "string"},
        "disclaimer": {"type": "string"},
        "primary_color": {"type": "string"},
        "background_color": {"type": "string"},
        "angle": {"type": "string"},
    },
    "required": ["brand", "headline", "subheadline", "bullets", "cta", "urgency", "trust", "disclaimer",
                 "primary_color", "background_color", "angle"],
    "additionalProperties": False,
}

HTML_SCHEMA = {
    "type": "object",
    "properties": {"html": {"type": "string"}, "notes": {"type": "string"}},
    "required": ["html", "notes"],
    "additionalProperties": False,
}

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "issues": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
    },
    "required": ["ok", "issues", "summary"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------- data

def all_landers() -> dict[str, dict]:
    data = storage.get(KEY, {})
    return data if isinstance(data, dict) else {}


def get(lp_id: str) -> dict | None:
    return all_landers().get(str(lp_id))


def _update(lp_id: str, **fields) -> dict:
    data = all_landers()
    data[str(lp_id)].update(fields)
    storage.put(KEY, data)
    return data[str(lp_id)]


def _log(lp_id: str, text: str) -> None:
    record = get(lp_id)
    if record:
        _update(lp_id, log=(record.get("log") or [])[-40:] + [{"ts": time.time(), "text": text}])
    log.info("Landing page #%s: %s", lp_id, text)


def create(host: str, brief: str, source: str) -> dict:
    data = all_landers()
    lp_id = str(max([int(k) for k in data] or [0]) + 1)
    record = tracking.site(host) or {}
    data[lp_id] = {"id": lp_id, "host": host, "site_url": record.get("url") or f"https://{host}/", "brief": brief,
                   "source": source, "status": "building", "created": time.time(), "log": [], "copy": None,
                   "html": "", "version": "", "passed": None, "issues": [], "summary": "", "writer": "",
                   "maker": "", "checker": "", "click_url": "", "live_url": "", "notes": ""}
    storage.put(KEY, data)
    return data[lp_id]


# ---------------------------------------------------------------- pemeriksaan otomatis

def problems(page: str, click: str) -> list[str]:
    """Masalah pasti tanpa AI. Halaman harus mandiri (satu file) dan semua tombolnya ke Click URL BeMob."""
    if not page.strip():
        return ["HTML kosong."]
    found = []
    if len(page) > 150_000:
        found.append("File terlalu besar (lebih dari 150 KB). Buang gambar/kode yang tidak perlu.")
    if not re.search(r"<meta[^>]+name=[\"']?viewport", page, re.I):
        found.append("Tidak ada <meta name=\"viewport\">, tampilan di HP akan kecil.")
    if not re.search(r"<title[^>]*>\s*\S", page, re.I):
        found.append("Tidak ada <title>.")
    if re.search(r"<script\b", page, re.I):
        found.append("Jangan memakai <script>. Landing page cukup HTML + CSS (lebih cepat dan aman).")
    if re.search(r"<(iframe|form|object|embed)\b", page, re.I):
        found.append("Jangan memakai iframe, form, object, atau embed.")
    links = tracking.page_links(page, "https://landing.invalid/")
    to_click = [l for l in links if l["url"].split("?")[0].rstrip("/") == click.rstrip("/")]
    other = [l["url"] for l in links if l not in to_click and tracking.host_of(l["url"]) != "landing.invalid"]
    if not to_click:
        found.append(f"Tidak ada tombol yang menuju Click URL BeMob ({click}).")
    if other:
        found.append(f"Ada link selain Click URL BeMob: {', '.join(other[:3])}. Semua tombol wajib ke {click}.")
    outside = sorted({h.lower() for h in re.findall(r"https?://([A-Za-z0-9.-]+)", page)}
                     - {tracking.host_of(click), "www.w3.org"})  # w3.org = penanda SVG, bukan file luar
    if outside:
        found.append(f"Memuat file dari domain lain ({', '.join(outside[:3])}). Pakai CSS, emoji, atau SVG di dalam "
                     "file saja supaya cukup satu file.")
    return found


def clean_html(page: str) -> str:
    page = (page or "").strip()
    page = re.sub(r"^```\w*\s*|\s*```$", "", page)
    return page.strip()


def stamp(page: str, version: str, host: str) -> str:
    """Tandai versi supaya sistem tahu file yang di-upload Owner adalah versi ini."""
    page = re.sub(r"<!--\s*" + MARKER + r".*?-->\s*", "", page)
    mark = f"<!-- {MARKER} {version} · untuk {host} · dibuat Tim AI -->\n"
    return re.sub(r"(<head[^>]*>)", r"\1\n" + mark, page, count=1, flags=re.I) if re.search(r"<head", page, re.I) \
        else mark + page


def version_in(page: str) -> str | None:
    match = re.search(MARKER + r"\s+([0-9a-f]{8})", page or "")
    return match.group(1) if match else None


def template_html(copy: dict, click: str) -> str:
    """Template standar yang aman (dipakai jika semua AI Developer gagal)."""
    e = lambda key: htmlmod.escape(str(copy.get(key) or ""))
    color = lambda key, default: copy.get(key) if re.fullmatch(r"#[0-9a-fA-F]{3,8}", str(copy.get(key) or "")) \
        else default
    primary, background = color("primary_color", "#e11d48"), color("background_color", "#0f172a")
    bullets = "".join(f"<li>{htmlmod.escape(str(b))}</li>" for b in (copy.get("bullets") or [])[:5])
    link = htmlmod.escape(click)
    return f"""<!DOCTYPE html>
<html lang="id">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e('brand')} · {e('headline')}</title>
<style>
  *{{box-sizing:border-box}}
  body{{margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:{background};color:#fff;
    min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px 16px}}
  main{{width:100%;max-width:440px;text-align:center}}
  .brand{{font-weight:800;letter-spacing:.08em;text-transform:uppercase;opacity:.85;font-size:14px}}
  h1{{font-size:30px;line-height:1.15;margin:14px 0 10px}}
  p.sub{{font-size:17px;opacity:.9;margin:0 0 20px}}
  ul{{list-style:none;padding:0;margin:0 0 24px;text-align:left}}
  li{{background:rgba(255,255,255,.08);border-radius:12px;padding:12px 14px;margin:0 0 8px;font-size:16px}}
  li::before{{content:"✔";color:{primary};font-weight:800;margin-right:10px}}
  .cta{{display:block;background:{primary};color:#fff;text-decoration:none;font-weight:800;font-size:20px;
    padding:18px;border-radius:14px;box-shadow:0 8px 24px rgba(0,0,0,.35)}}
  .urgency{{margin:14px 0 0;font-weight:700;color:#fde68a}}
  .trust{{margin:18px 0 0;font-size:14px;opacity:.8}}
  .disclaimer{{margin:24px 0 0;font-size:12px;opacity:.6}}
</style>
</head>
<body>
<main>
  <div class="brand">{e('brand')}</div>
  <h1>{e('headline')}</h1>
  <p class="sub">{e('subheadline')}</p>
  <ul>{bullets}</ul>
  <a class="cta" href="{link}">{e('cta')}</a>
  <p class="urgency">{e('urgency')}</p>
  <p class="trust">{e('trust')}</p>
  <p class="disclaimer">{e('disclaimer')}</p>
  <a class="cta" href="{link}" style="margin-top:20px">{e('cta')}</a>
</main>
</body>
</html>
"""


# ---------------------------------------------------------------- prompt

RULES = """Aturan isi (wajib):
- Bahasa Indonesia yang mudah dibaca di HP. Menarik, tetapi TIDAK menipu: jangan menjanjikan pasti menang/untung,
  jangan mengarang angka, testimoni, penghargaan, atau lisensi. Patuhi aturan iklan RollerAds.
- Pakai nama/merek dan penawaran yang benar-benar ada di website tujuan atau brief Owner. Jika tidak ada, tulis umum.
- Sertakan disclaimer singkat (mis. khusus 18+, bermain secara bertanggung jawab) jika penawarannya permainan/taruhan."""


def _copy_prompt(record: dict, digest: str, feedback: str) -> str:
    return (f"TUGAS: Tulis isi landing page yang mengarahkan pengunjung iklan (popunder/push, kebanyakan dari HP) "
            f"ke website {record['site_url']}.\n\n{memory.notes_text(3000)}\n\n"
            f"Brief Owner / alasan tim: {record['brief'] or '-'}\n"
            + (f"Catatan revisi dari Owner: {feedback}\n" if feedback else "")
            + f"\nRingkasan HTML website tujuan (untuk nama merek & penawaran):\n{digest or '(tidak bisa diambil)'}\n\n"
            f"{RULES}\n\nIsi field: brand, headline (maks 60 karakter), subheadline (maks 120), bullets (3-4 poin "
            "keunggulan singkat), cta (teks tombol, maks 24), urgency (1 kalimat ajakan segera, tanpa hitung mundur "
            "palsu), trust (1 kalimat), disclaimer, primary_color & background_color (hex, mis. #e11d48), angle "
            "(1 kalimat: sudut pandang/ide yang dites, untuk A/B test).")


def _dev_prompt(record: dict, copy: dict, click: str, previous: str | None, feedback: list[str]) -> str:
    prompt = (f"TUGAS: Buat landing page satu file HTML untuk isi berikut (dari Creative):\n{copy}\n\n"
              f"Syarat wajib:\n"
              f"1. SEMUA tombol/link ajakan memakai href persis \"{click}\" (Click URL BeMob). Tidak boleh ada link "
              f"lain, termasuk langsung ke {record['host']}. Minimal 2 tombol (atas & bawah).\n"
              "2. Satu file mandiri: HTML + <style> saja. DILARANG <script>, iframe, form, font/gambar dari internet. "
              "Hiasan pakai CSS, emoji, atau SVG di dalam file.\n"
              "3. Mobile-first: <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">, <title>, "
              "lang=\"id\", teks besar dan tombol mudah ditekan, cepat dimuat (di bawah 60 KB).\n"
              "4. Pakai isi dari Creative apa adanya (boleh merapikan tata letak, jangan menambah klaim baru).\n\n")
    if previous:
        prompt += "REVISI. Versi sebelumnya:\n" + previous[:20000] + "\n\nMasalah yang WAJIB diperbaiki:\n- " \
                  + "\n- ".join(feedback) + "\n\n"
    return prompt + "Isi field: html (dokumen HTML lengkap), notes (catatan singkat untuk Owner)."


def _review_prompt(record: dict, copy: dict, click: str, page: str) -> str:
    return (f"TUGAS: Periksa landing page buatan Developer sebelum di-upload Owner. Website tujuan {record['site_url']}, "
            f"semua tombol wajib ke Click URL BeMob {click}.\n\n{RULES}\n\nIsi dari Creative:\n{copy}\n\n"
            f"HTML:\n{page[:40000]}\n\n"
            "Nilai ok=true hanya jika tidak ada masalah yang membuat pengunjung tidak bisa lanjut ke website, tampilan "
            "rusak di HP, atau isinya menipu/melanggar aturan iklan. Selera desain jangan dijadikan alasan menolak. "
            "issues: masalah konkret beserta cara memperbaikinya (kosong jika ok). summary: 1-2 kalimat untuk Owner.")


# ---------------------------------------------------------------- membuat

async def _develop(lp_id: str, record: dict, copy: dict, click: str) -> dict:
    """Developer membuat HTML, dicek otomatis, lalu QA. Mengembalikan percobaan terbaik."""
    dev, qa = AGENTS["developer"], AGENTS["qa"]
    previous, feedback, best = None, [], None
    for provider in llm.chain(dev)[:MAX_MAKERS]:
        _log(lp_id, f"{llm.label(dev, provider)} {'memperbaiki' if previous else 'membuat'} HTML…")
        for _ in range(2):
            try:
                draft = await llm.ask(dev, _dev_prompt(record, copy, click, previous, feedback), schema=HTML_SCHEMA,
                                      provider=provider, fallback=False, task="landing")
            except llm.LLMError as e:
                _log(lp_id, f"{llm.provider_name(provider)} gagal: {str(e)[:200]}")
                break
            page = clean_html(draft.get("html", ""))
            found = problems(page, click)
            attempt = {"html": page, "maker": llm.label(dev, provider), "notes": draft.get("notes", "")}
            if found:
                _log(lp_id, "Cek otomatis menemukan masalah: " + "; ".join(found))
                previous, feedback = page, found
                continue
            _log(lp_id, f"Lolos cek otomatis. {llm.label(qa)} memeriksa…")
            try:
                review, checker = await llm.ask_ex(qa, _review_prompt(record, copy, click, page),
                                                   schema=REVIEW_SCHEMA, task="landing")
            except llm.LLMError as e:
                return {**attempt, "passed": None, "issues": [f"QA tidak bisa memeriksa: {str(e)[:200]}"],
                        "summary": "Belum diperiksa QA.", "checker": ""}
            attempt.update(passed=bool(review.get("ok")), issues=review.get("issues") or [],
                           summary=review.get("summary", ""), checker=llm.label(qa, checker))
            if review.get("ok"):
                _log(lp_id, f"✓ Disetujui {attempt['checker']}.")
                return attempt
            _log(lp_id, f"Ditolak {attempt['checker']}: " + "; ".join(attempt["issues"]))
            best, previous, feedback = attempt, page, attempt["issues"] or ["QA menolak tanpa alasan jelas."]
    if best:
        return best
    _log(lp_id, "Semua AI Developer gagal. Memakai template standar.")
    page = template_html(copy, click)
    return {"html": page, "maker": "Template standar", "notes": "Template standar karena AI Developer gagal.",
            "passed": None if not problems(page, click) else False, "issues": problems(page, click),
            "summary": "Template standar (belum diperiksa QA).", "checker": ""}


async def build(team: Team, lp_id: str, feedback: str = "") -> None:
    if lp_id in BUILDING:
        return
    BUILDING.add(lp_id)
    try:
        record = _update(lp_id, status="building", log=[])
        site = tracking.site(record["host"]) or {"host": record["host"], "tracking_urls": []}
        click = tracking.click_url(site)
        if not click:
            _update(lp_id, status="failed", summary="Click URL BeMob belum diketahui.")
            await team.send("developer", "kreatif", f"⚠️ Landing page #{lp_id} belum bisa dibuat: Click URL BeMob belum "
                            "diketahui. Isi URL postback BeMob di Pengaturan, atau daftarkan link campaign BeMob.")
            return
        creative = AGENTS["creative"]
        await team.send("creative", "kreatif", f"🎨 Menyiapkan landing page #{lp_id} untuk {record['host']}. "
                        f"Isi ditulis {llm.label(creative)}, HTML dibuat {llm.label(AGENTS['developer'])}, "
                        f"diperiksa {llm.label(AGENTS['qa'])}.")
        _log(lp_id, f"Membuka {record['site_url']}…")
        page = await tracking.fetch(record["site_url"])
        digest = tracking.digest(page.get("html", ""), 8000) if page["ok"] else ""
        _log(lp_id, f"{llm.label(creative)} menulis isi…")
        try:
            copy, writer = await llm.ask_ex(creative, _copy_prompt(record, digest, feedback),
                                            schema=COPY_SCHEMA, task="landing")
        except llm.LLMError as e:
            _update(lp_id, status="failed", summary=f"Creative gagal: {str(e)[:200]}")
            await team.send("creative", "kreatif", f"⚠️ Landing page #{lp_id} gagal: Creative tidak bisa menulis isi ({e}).")
            return
        result = await _develop(lp_id, record, copy, click)
        version = tracking.version_of(result["html"])
        record = _update(lp_id, status="ready", copy=copy, html=stamp(result["html"], version, record["host"]),
                         version=version, click_url=click, writer=llm.label(creative, writer), **{
                             k: result[k] for k in ("maker", "checker", "passed", "issues", "summary", "notes")})
        status = ("✅ lolos pemeriksaan QA" if record["passed"] else
                  "⚠️ BELUM lolos QA, baca catatannya" if record["passed"] is False else "⚠️ belum diperiksa QA")
        await team.send("developer", "kreatif",
                        f"📦 Landing page #{lp_id} untuk {record['host']} siap (versi {version}), {status}.\n"
                        f"Ide: {copy.get('angle', '-')}\nJudul: {copy.get('headline', '')}\nTombol: {copy.get('cta', '')} "
                        f"→ {click}\nIsi: {record['writer']} · HTML: {record['maker']}"
                        + (f" · Dicek: {record['checker']}" if record["checker"] else "")
                        + "\n\nLangkah Anda:\n1. Buka file terlampir di HP/browser untuk melihat tampilannya.\n"
                        "2. Upload ke hosting Anda sebagai halaman baru (mis. promo.domain-anda.com). Cukup satu file ini.\n"
                        "3. Di BeMob: menu Landers → tambah lander dengan alamat tersebut, lalu di campaign pilih alur "
                        "Landing → Offer.\n"
                        f"4. Beri tahu alamatnya: dashboard → 🔌 Script Tracking → Sudah online, atau /lponline {lp_id} <alamat>.\n"
                        "Setelah itu sistem mengecek tombolnya dan memantaunya otomatis.")
        await team.send_file("developer", "kreatif", f"landing-{record['host']}-{lp_id}.html", record["html"],
                             f"Landing page #{lp_id} v{version}. Upload ke hosting Anda.")
        if record["issues"]:
            await team.send("qa", "qa", f"Catatan QA untuk landing page #{lp_id}:\n- " + "\n- ".join(record["issues"]))
    except Exception as e:  # noqa: BLE001 - laporkan ke Owner, jangan diam-diam gagal
        log.exception("Landing page #%s gagal dibuat", lp_id)
        if get(lp_id):
            _update(lp_id, status="failed", summary=str(e)[:300])
        await team.send("developer", "kreatif", f"⚠️ Landing page #{lp_id} gagal dibuat: {e}")
    finally:
        BUILDING.discard(lp_id)


def request(team: Team, host: str, brief: str, source: str) -> dict:
    """Catat permintaan lalu buat di latar belakang."""
    record = create(host, brief, source)
    tracking._spawn(build(team, record["id"]))
    return record


def revise(team: Team, lp_id: str, feedback: str) -> bool:
    if lp_id in BUILDING or not get(lp_id):
        return False
    record = get(lp_id)
    _update(lp_id, brief=(record["brief"] + f"\nRevisi Owner: {feedback}").strip() if feedback else record["brief"])
    tracking._spawn(build(team, lp_id, feedback))
    return True


async def mark_online(team: Team, lp_id: str, url: str) -> dict:
    """Owner sudah meng-upload: cek versinya, daftarkan ke website tujuan, dan cek tombolnya."""
    record = get(lp_id)
    page = await tracking.fetch(url)
    found = version_in(page.get("html", "")) if page["ok"] else None
    same = found == record.get("version")
    tracking.register("", site_url=record["site_url"], lander_url=url, click=record.get("click_url", ""))
    check = await tracking.check_lander(None, record["host"], url) or {"ok": False, "results": []}
    _update(lp_id, status="online", live_url=url)
    note = ("versi yang online sama dengan buatan tim ✓" if same else
            f"yang online versi {found} (terbaru {record.get('version')}), upload ulang file terbaru" if found else
            "halaman tidak bisa dibuka" if not page["ok"] else
            "penanda versi tidak ditemukan (mungkin isinya diubah hosting). Tidak apa-apa selama tombolnya benar")
    lines = "\n".join(f"{'✅' if r['ok'] else '❌' if r['ok'] is False else 'ℹ️'} {r['text']}" for r in check["results"])
    await team.send("qa", "qa", f"🧪 Landing page #{lp_id} online di {url}: {note}.\n{lines}\n\n"
                                + ("Tombol sudah lewat BeMob. Pastikan di BeMob campaign-nya memakai alur Landing → Offer "
                                   "dengan lander ini; QA ikut menelusuri jalur lengkapnya saat Tes koneksi."
                                   if check["ok"] else "Tombolnya belum benar. Lihat dashboard → 🔌 Script Tracking."))
    return {"ok": check["ok"], "same_version": same, "found": found, "note": note, "results": check["results"]}


def status_text() -> str:
    items = [r for r in all_landers().values() if r["status"] != "failed"]
    if not items:
        return ""
    names = {"building": "sedang dibuat", "ready": "siap, MENUNGGU di-upload Owner", "online": "online"}
    return "Landing page buatan tim AI:\n" + "\n".join(
        f"- #{r['id']} untuk {r['host']}: {names.get(r['status'], r['status'])}"
        + (f" di {r['live_url']}" if r.get("live_url") else "") + (f" · ide: {r['copy'].get('angle')}" if r.get("copy") else "")
        for r in sorted(items, key=lambda r: int(r["id"]))[-10:])
