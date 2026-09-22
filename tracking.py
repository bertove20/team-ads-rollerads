"""Script tracking konversi untuk website tujuan campaign.

Alur: Developer membuat script -> dicek otomatis (sintaks, URL postback, domain luar) -> QA me-review kodenya.
Jika ditolak, Developer merevisi. Jika AI Developer gagal/ditolak dua kali, AI cadangan berikutnya membantu.
Jika semua AI gagal, dipakai template standar. Setelah itu QA mengetes koneksinya: script terpasang di website,
link BeMob meneruskan click_id, dan postback BeMob -> RollerAds terpasang.

Mode landing page (Iklan -> BeMob -> landing page -> tombol -> Click URL BeMob -> website?click_id=): script konversi
tetap untuk website tujuan. Landing page hanya dicek tombolnya (harus ke Click URL BeMob); jika salah, Developer
memberi kode perbaikan. QA menelusuri jalur lengkapnya.

AI yang dipakai Developer & QA diatur Owner (AI_DEVELOPER, AI_QA, AI_FALLBACK di .env).
"""
import asyncio
import datetime as dt
import hashlib
import logging
import os
import re
import shutil
import tempfile
import time
from urllib.parse import parse_qs, urljoin, urlparse

import bemob
import checks
import config
import llm
import storage
from agents import AGENTS
from telegram_team import Team

log = logging.getLogger(__name__)

SITES_KEY = "tracking_sites"
MARKER = "AIADS-TRACKING"
CLICK_PARAMS = ("click_id", "clickid", "cid", "subid")
TEMPLATE_PATH = config.BASE_DIR / "snippets" / "bemob-footer.html"
MAX_MAKERS = 3  # berapa AI yang boleh mencoba membuat script

JOBS: dict[str, dict] = {}  # host -> progres pembuatan script (untuk dashboard)
_tasks: set[asyncio.Task] = set()

KIT_SCHEMA = {
    "type": "object",
    "properties": {
        "code": {"type": "string"},
        "placement": {"type": "string"},
        "events": {"type": "array", "items": {"type": "string"}},
        "owner_steps": {"type": "array", "items": {"type": "string"}},
        "notes": {"type": "string"},
    },
    "required": ["code", "placement", "events", "owner_steps", "notes"],
    "additionalProperties": False,
}

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "issues": {"type": "array", "items": {"type": "string"}},
        "test_steps": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
    },
    "required": ["ok", "issues", "test_steps", "summary"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------- data website

def sites() -> dict[str, dict]:
    data = storage.get(SITES_KEY, {})
    return data if isinstance(data, dict) else {}


def site(host: str) -> dict | None:
    return sites().get(host)


def _update(host: str, **fields) -> dict:
    """Baca-ubah-simpan tanpa await di tengahnya, supaya tidak menimpa perubahan tugas lain."""
    data = sites()
    record = data.setdefault(host, {"host": host, "url": f"https://{host}/", "hints": "", "campaigns": [],
                                    "tracking_urls": [], "landers": {}, "kit": None, "test": None,
                                    "installed": None})
    record.update(fields)
    storage.put(SITES_KEY, data)
    return record


def host_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def _root(host: str) -> str:
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) > 2 else host


def is_tracker(host: str) -> bool:
    return "bemob" in host or host == host_of(config.BEMOB_POSTBACK_URL)


def postback_url(record: dict) -> str | None:
    """URL postback BeMob: dari Pengaturan, atau ditebak dari domain link campaign BeMob."""
    if config.BEMOB_POSTBACK_URL:
        return config.BEMOB_POSTBACK_URL.split("?")[0]
    for url in record.get("tracking_urls", []):
        if is_tracker(host_of(url)):
            return f"https://{host_of(url)}/postback"
    return None


def click_url(record: dict, lander: dict | None = None) -> str | None:
    """Click URL BeMob untuk tombol landing page: dari Owner, atau ditebak dari domain link campaign/postback."""
    if lander and lander.get("click_url"):
        return lander["click_url"]
    for url in record.get("tracking_urls", []):
        if is_tracker(host_of(url)):
            return f"https://{host_of(url)}/click"
    postback = postback_url(record)
    return f"https://{host_of(postback)}/click" if postback else None


def _log(host: str, text: str) -> None:
    job = JOBS.setdefault(host, {"running": True, "log": [], "started": time.time()})
    job["log"].append({"ts": time.time(), "text": text})
    log.info("Tracking %s: %s", host, text)


def _spawn(coro) -> None:
    task = asyncio.get_running_loop().create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


# ---------------------------------------------------------------- mengambil halaman

async def fetch(url: str) -> dict:
    """Buka halaman seperti HP Android. Ikuti redirect HTTP dan meta refresh (dipakai sebagian link BeMob)."""
    async with checks._client() as http:
        result = await checks.probe(http, url)
        for _ in range(2):
            meta = re.search(r'<meta[^>]+http-equiv=["\']?refresh[^>]+url=([^"\'>]+)', result.get("html") or "", re.I)
            if not result["ok"] or not meta:
                break
            url = urljoin(url, meta.group(1).strip())
            result = await checks.probe(http, url)
    return result


async def _follow(http, url: str) -> tuple[str, str]:
    """(URL akhir, HTML) setelah redirect HTTP dan meta refresh. ("", "") jika tidak bisa dibuka.
    Pakai `http` yang sama untuk langkah berikutnya supaya cookie BeMob ikut (dibutuhkan Click URL)."""
    try:
        response = await http.get(url)
        for _ in range(2):
            meta = re.search(r'<meta[^>]+http-equiv=["\']?refresh[^>]+url=([^"\'>]+)', response.text or "", re.I)
            if not meta:
                break
            response = await http.get(urljoin(str(response.url), meta.group(1).strip()))
    except Exception:  # noqa: BLE001 - dilaporkan sebagai tidak bisa dibuka
        return "", ""
    return str(response.url), response.text or ""


async def resolve(url: str) -> str:
    """URL akhir setelah semua redirect (mis. link BeMob -> website?click_id=...)."""
    async with checks._client() as http:
        return (await _follow(http, url))[0]


def page_links(html: str, base: str) -> list[dict]:
    """Tujuan tombol/link di halaman: <a href>, <form action>, dan location.href / window.open di JavaScript."""
    found: dict[str, str] = {}

    def add(target: str, text: str) -> None:
        target = (target or "").strip().replace("&amp;", "&")
        if not target or target.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
            return
        url = urljoin(base, target)
        if url.startswith(("http://", "https://")) and url not in found:
            found[url] = text

    for m in re.finditer(r"<a\b([^>]*)>(.*?)</a>", html or "", re.I | re.S):
        href = re.search(r'\bhref\s*=\s*["\']?([^"\'\s>]+)', m.group(1), re.I)
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(2))).strip()
        add(href.group(1) if href else "", text[:60] or "(tanpa tulisan)")
    for m in re.finditer(r'<form\b[^>]*\baction\s*=\s*["\']?([^"\'\s>]+)', html or "", re.I):
        add(m.group(1), "(form)")
    for m in re.finditer(r'(?:location(?:\.href)?\s*=|location\.(?:assign|replace)\(|window\.open\()\s*["\']([^"\']+)',
                         html or ""):
        add(m.group(1), "(JavaScript)")
    return [{"url": u, "text": t} for u, t in found.items()]


def _on_site(url: str, host: str) -> bool:
    h = host_of(url)
    return h == host or h == _root(host) or h.endswith("." + _root(host))


def digest(html: str, limit: int = 24000) -> str:
    """Ringkas HTML untuk AI: buang script, style, svg, dan komentar; sisakan struktur & atribut."""
    html = re.sub(r"<(script|style|svg|noscript)\b.*?</\1>", "", html or "", flags=re.I | re.S)
    html = re.sub(r"<!--.*?-->", "", html, flags=re.S)
    html = re.sub(r"\s+", " ", html)
    return html[:limit] + (" …(dipotong)" if len(html) > limit else "")


def installed_version(html: str) -> str | None:
    match = re.search(MARKER + r"\s+([0-9a-f]{8})", html or "") or \
        re.search(r'AIADS_TRACKING\s*=\s*["\']([0-9a-f]{8})', html or "")
    return match.group(1) if match else None


def installed_code(html: str) -> str:
    match = re.search(r"<!--\s*" + MARKER + r".*?<!--\s*/" + MARKER + r"\s*-->", html or "", re.S)
    return match.group(0) if match else ""


# ---------------------------------------------------------------- pemeriksaan kode otomatis

def clean_code(code: str) -> str:
    code = (code or "").strip()
    code = re.sub(r"^```\w*\s*|\s*```$", "", code)
    code = re.sub(r"</?script[^>]*>", "", code, flags=re.I)
    return code.strip()


async def syntax_check(code: str) -> tuple[bool | None, str]:
    """Cek sintaks dengan Node.js jika terpasang. None = tidak bisa dicek di komputer ini."""
    node = shutil.which("node")
    if not node:
        return None, "Node.js tidak terpasang, sintaks dicek QA dan browser dashboard."
    fd, path = tempfile.mkstemp(suffix=".js")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(code)
        proc = await asyncio.create_subprocess_exec(node, "--check", path, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.PIPE)
        _, err = await asyncio.wait_for(proc.communicate(), 30)
    except (OSError, NotImplementedError, asyncio.TimeoutError) as e:
        return None, f"Sintaks tidak bisa dicek ({type(e).__name__})."
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    if proc.returncode == 0:
        return True, "Sintaks JavaScript valid (node --check)."
    lines = [ln for ln in err.decode("utf-8", "replace").splitlines() if ln.strip()]
    return False, "Sintaks JavaScript error: " + " / ".join(lines[1:4] or lines[:3])


async def static_problems(code: str, host: str, postback: str) -> tuple[list[str], str]:
    """Masalah pasti (tanpa AI) + catatan hasil cek sintaks."""
    problems = []
    postback_host = host_of(postback)
    if not code:
        return ["Script kosong."], ""
    if len(code) > 40000:
        problems.append("Script terlalu panjang (lebih dari 40.000 karakter).")
    if postback_host not in code:
        problems.append(f"URL postback BeMob ({postback}) tidak ada di script.")
    if "click_id" not in code:
        problems.append("Script tidak membaca parameter click_id dari URL.")
    if "txid" not in code:
        problems.append("Script tidak mengirim txid, konversi bisa tercatat dobel.")
    if re.search(r"\beval\s*\(|new\s+Function\s*\(|document\.write\s*\(", code):
        problems.append("Dilarang memakai eval, new Function, atau document.write.")
    allowed = {postback_host, host, _root(host)}
    outside = sorted({h.lower() for h in re.findall(r"https?://([A-Za-z0-9.-]+)", code)
                      if h.lower() not in allowed and not h.lower().endswith("." + _root(host))})
    if outside:
        problems.append(f"Script menghubungi domain lain: {', '.join(outside)}. Hanya boleh {postback_host} "
                        "dan website itu sendiri.")
    ok, note = await syntax_check(code)
    if ok is False:
        problems.append(note)
    return problems, note


def version_of(code: str) -> str:
    return hashlib.sha1(code.encode("utf-8")).hexdigest()[:8]


def wrap(code: str, version: str, host: str) -> str:
    day = dt.datetime.now(config.TIMEZONE).strftime("%d-%m-%Y")
    return (f"<!-- {MARKER} {version} · {host} · dibuat Tim AI {day}. Tempel di FOOTER semua halaman. -->\n"
            f"<script>\nwindow.AIADS_TRACKING = \"{version}\";\n{code}\n</script>\n<!-- /{MARKER} -->\n")


def template_code(postback: str) -> str:
    """Template standar yang sudah teruji (dipakai jika semua AI gagal), diisi dengan pengaturan Owner."""
    html = TEMPLATE_PATH.read_text(encoding="utf-8")
    code = re.search(r"<script>(.*?)</script>", html, re.S).group(1).strip()
    for name, value in (("POSTBACK", f"'{postback}'"), ("PAYOUT_DAFTAR", f"{config.TRACK_REG_PAYOUT_USD:g}"),
                        ("KURS_USD", f"{config.TRACK_KURS_USD:g}"), ("BAGIAN_DEPOSIT", f"{config.TRACK_DEPOSIT_SHARE:g}")):
        code = re.sub(rf"(var {name}\s*=\s*)[^;]+;", lambda m, v=value: m.group(1) + v + ";", code, count=1)
    return code


# ---------------------------------------------------------------- prompt

def _requirements(host: str, postback: str) -> str:
    return f"""Cara kerja tracking:
- Pengunjung dari iklan RollerAds lewat BeMob tiba di website dengan parameter URL click_id
  (mis. https://{host}/?click_id=AbC123). Terima juga clickid, cid, dan subid sebagai cadangan.
- Setiap konversi dikirim dari browser ke URL postback BeMob:
  {postback}?cid=CLICK_ID&payout=NILAI_USD&txid=ID_UNIK  (pakai new Image().src).
- BeMob meneruskan konversi ke RollerAds sendiri. Script tidak perlu menghubungi RollerAds.

Nilai konversi:
- Daftar (registrasi): payout {config.TRACK_REG_PAYOUT_USD:g} USD, txid "REG-" + click_id, sekali per click_id.
- Deposit: HANYA deposit yang berhasil/disetujui (bukan saat tombol kirim diklik, bukan pending/ditolak).
  payout = nominal dalam rupiah / {config.TRACK_KURS_USD:g} x {config.TRACK_DEPOSIT_SHARE:g}.
  txid = nomor referensi transaksi dari website. Perhatikan skala nominal di website
  (mis. tulisan "IDR 1 = 1,000" berarti angka 50 = Rp50.000).

Syarat wajib kode:
1. JavaScript murni tanpa tag <script> dan tanpa library, dibungkus IIFE. Setiap bagian dibungkus try/catch
   supaya website TIDAK PERNAH rusak walau terjadi error.
2. Simpan click_id di localStorage DAN cookie first-party di root domain (.{_root(host)}) selama 30 hari,
   supaya tetap ada saat pengunjung pindah halaman atau subdomain.
3. Anti-dobel: setiap txid dikirim maksimal sekali (simpan daftar txid terkirim). Abaikan deposit yang terjadi
   sebelum click_id pertama kali tersimpan.
4. Hanya boleh menghubungi {host_of(postback)} dan website itu sendiri (same-origin).
   Dilarang eval, new Function, dan document.write.
5. Selector elemen harus benar-benar ada di HTML website / contoh HTML dari Owner. Halaman bisa berubah
   tanpa reload (AJAX), jadi pakai event delegation dan/atau MutationObserver.
6. Pengaturan (URL postback, payout, kurs, porsi deposit, DEBUG) ditaruh sebagai variabel di baris atas dengan
   komentar bahasa Indonesia. DEBUG=false; jika true, tulis log console berawalan [BeMob].
7. Kode ditempel di footer semua halaman, jadi harus aman dijalankan sebelum maupun sesudah DOM siap."""


def _context(record: dict, page_digest: str) -> str:
    return (f"HTML website (ringkasan otomatis dari {record['url']}):\n{page_digest or '(tidak bisa diambil)'}\n\n"
            f"Contoh HTML dari Owner (tombol daftar, form deposit, riwayat transaksi, dll.):\n"
            f"{record.get('hints') or '(tidak ada)'}")


def _maker_prompt(record: dict, page_digest: str, postback: str, previous: str | None, feedback: list[str]) -> str:
    try:
        reference = template_code(postback)
    except (OSError, AttributeError):
        reference = "(tidak ada)"
    prompt = (f"TUGAS: Buat script tracking konversi untuk website {record['url']} (host {record['host']}).\n\n"
              f"{_requirements(record['host'], postback)}\n\n{_context(record, page_digest)}\n\n"
              "Contoh script yang sudah terbukti untuk website sejenis. Sesuaikan selector dan logikanya dengan "
              f"website ini; jangan menyalin mentah jika strukturnya berbeda:\n{reference}\n\n")
    if previous:
        prompt += ("REVISI. Versi sebelumnya:\n" + previous + "\n\nMasalah yang WAJIB diperbaiki:\n- "
                   + "\n- ".join(feedback) + "\n\n")
    return prompt + ("Isi field: code (JavaScript saja), placement (di mana Owner menempel, bahasa sederhana), "
                     "events (daftar konversi yang dilacak beserta pemicunya), owner_steps (langkah pemasangan untuk "
                     "Owner yang awam), notes (asumsi dan keterbatasan, jujur).")


def _review_prompt(record: dict, page_digest: str, postback: str, code: str, syntax_note: str) -> str:
    return (f"TUGAS: Periksa script tracking buatan Developer untuk website {record['url']} sebelum ditempel Owner.\n\n"
            f"Syarat yang harus dipenuhi:\n{_requirements(record['host'], postback)}\n\n"
            f"{_context(record, page_digest)}\n\nHasil cek otomatis: {syntax_note or '-'}\n\n"
            f"SCRIPT:\n{code}\n\n"
            "Nilai ok=true hanya jika tidak ada masalah yang membuat konversi salah hitung, tidak terkirim, terkirim "
            "dobel, atau merusak website. Masalah gaya penulisan jangan dijadikan alasan menolak. issues: masalah "
            "konkret beserta cara memperbaikinya (kosong jika ok). test_steps: langkah tes manual untuk Owner (klik "
            "link campaign sendiri, daftar, deposit kecil, cek BeMob → Conversions, cara memakai DEBUG). "
            "summary: 1-2 kalimat.")


# ---------------------------------------------------------------- membuat script

async def _make(team: Team, record: dict, page_digest: str, postback: str) -> dict:
    """Developer membuat, QA memeriksa. Mengembalikan percobaan terbaik."""
    host = record["host"]
    dev, qa = AGENTS["developer"], AGENTS["qa"]
    makers = llm.chain(dev)[:MAX_MAKERS]
    best = None  # percobaan yang lolos cek otomatis tetapi ditolak QA
    # AI cadangan melanjutkan draf terakhir + catatan masalahnya, tidak mulai dari nol.
    previous, feedback, reason = None, [], ""
    for i, provider in enumerate(makers):
        if i:
            await team.send("developer", "kreatif", f"🤝 {llm.label(dev, makers[i - 1])} belum berhasil ({reason}). "
                                                    f"Minta bantuan {llm.provider_name(provider)}.")
        _log(host, f"{llm.label(dev, provider)} {'memperbaiki' if previous else 'membuat'} script…")
        for _ in range(2):
            try:
                draft = await llm.ask(dev, _maker_prompt(record, page_digest, postback, previous, feedback),
                                      schema=KIT_SCHEMA, provider=provider, fallback=False, task="script")
            except llm.LLMError as e:
                reason = str(e)[:200]
                _log(host, f"{llm.provider_name(provider)} gagal: {reason}")
                break
            code = clean_code(draft.get("code", ""))
            problems, syntax_note = await static_problems(code, host, postback)
            attempt = {**draft, "code": code, "maker": provider, "syntax": syntax_note}
            if problems:
                reason = problems[0]
                _log(host, "Cek otomatis menemukan masalah: " + "; ".join(problems))
                previous, feedback = code, problems
                continue
            _log(host, f"Lolos cek otomatis. {llm.label(qa)} memeriksa kode…")
            try:
                review, checker = await llm.ask_ex(qa, _review_prompt(record, page_digest, postback, code, syntax_note),
                                                   schema=REVIEW_SCHEMA, task="script")
            except llm.LLMError as e:
                _log(host, f"QA tidak bisa memeriksa: {e}")
                return {**attempt, "review": {"ok": None, "issues": [f"QA tidak bisa memeriksa: {str(e)[:200]}"],
                                              "test_steps": [], "summary": "Belum diperiksa QA."}, "checker": ""}
            attempt.update(review=review, checker=checker)
            if review.get("ok"):
                _log(host, f"✓ Disetujui {llm.label(qa, checker)}.")
                return attempt
            issues = review.get("issues") or ["QA menolak tanpa alasan jelas."]
            reason = "ditolak QA: " + issues[0][:160]
            _log(host, f"Ditolak {llm.label(qa, checker)}: " + "; ".join(issues))
            best, previous, feedback = attempt, code, issues
    if best:
        return best

    # Semua AI gagal: pakai template standar yang sudah teruji.
    _log(host, "Semua AI gagal membuat script. Memakai template standar.")
    code = template_code(postback)
    problems, syntax_note = await static_problems(code, host, postback)
    return {
        "code": code, "maker": "template", "syntax": syntax_note,
        "placement": "Tempel di bagian footer (sebelum </body>) semua halaman website.",
        "events": ["Daftar: saat tombol daftar diklik dan form sudah lengkap",
                   "Deposit: saat riwayat transaksi menampilkan deposit berstatus sukses"],
        "owner_steps": ["Salin seluruh script", "Tempel di pengaturan footer / custom HTML website", "Simpan",
                        "Klik Tes koneksi di dashboard"],
        "notes": "Template standar (tidak disesuaikan AI) karena semua AI gagal. Selector tombol daftar dan riwayat "
                 "deposit mungkin perlu disesuaikan dengan website Anda.",
        "review": {"ok": None if not problems else False, "issues": problems or
                   ["Belum diperiksa QA karena AI tidak tersedia."], "test_steps": [], "summary": "Template standar."},
        "checker": "",
    }


async def build(team: Team, host: str) -> dict | None:
    """Buat (ulang) script tracking untuk website `host`. Hasil disimpan dan diumumkan."""
    if JOBS.get(host, {}).get("task_active"):
        return None  # sedang dibuat oleh tugas lain
    JOBS[host] = {"running": True, "task_active": True, "log": [], "started": time.time()}
    try:
        record = site(host)
        if not record:
            return None
        postback = postback_url(record)
        if not postback:
            _log(host, "URL postback BeMob belum diketahui.")
            await team.send("developer", "kreatif", f"⚠️ Script tracking untuk {host} belum bisa dibuat: URL postback "
                            "BeMob belum diisi. Isi di dashboard → Pengaturan → Script tracking (mis. "
                            "https://xxxxx.bemobtrcks.com/postback).")
            return None
        dev, qa = AGENTS["developer"], AGENTS["qa"]
        await team.send("developer", "kreatif", f"🛠️ Menyiapkan script tracking untuk {host}. Dibuat oleh "
                        f"{llm.label(dev)}, diperiksa {llm.label(qa)}.")
        _log(host, f"Membuka {record['url']}…")
        page = await fetch(record["url"])
        page_digest = digest(page.get("html", "")) if page["ok"] else ""
        if not page["ok"]:
            _log(host, f"Website tidak bisa dibuka ({page.get('error') or page.get('status')}). Lanjut memakai contoh HTML dari Owner.")

        attempt = await _make(team, record, page_digest, postback)
        code = attempt["code"]
        version = version_of(code)
        review = attempt.get("review") or {}
        kit = {
            "version": version,
            "snippet": wrap(code, version, host),
            "code": code,
            "placement": attempt.get("placement", ""),
            "events": attempt.get("events", []),
            "owner_steps": attempt.get("owner_steps", []),
            "notes": attempt.get("notes", ""),
            "test_steps": review.get("test_steps", []),
            "issues": review.get("issues", []),
            "summary": review.get("summary", ""),
            "passed": review.get("ok"),
            "maker": "Template standar" if attempt["maker"] == "template" else llm.label(dev, attempt["maker"]),
            "checker": llm.label(qa, attempt["checker"]) if attempt.get("checker") else "",
            "syntax": attempt.get("syntax", ""),
            "postback": postback,
            "created": time.time(),
            "log": JOBS[host]["log"][-30:],
        }
        _update(host, kit=kit, installed=None)
        status = ("✅ lolos pemeriksaan QA" if kit["passed"] else
                  "⚠️ BELUM lolos pemeriksaan QA, baca catatannya" if kit["passed"] is False else "⚠️ belum diperiksa QA")
        steps = "\n".join(f"{i}. {s}" for i, s in enumerate(kit["owner_steps"], 1))
        await team.send("developer", "kreatif",
                        f"📦 Script tracking {host} siap (versi {version}), {status}.\nDibuat: {kit['maker']}"
                        + (f" · Dicek: {kit['checker']}" if kit["checker"] else "")
                        + f"\n\nDi mana: {kit['placement']}\n\nYang dilacak:\n- " + "\n- ".join(kit["events"] or ["-"])
                        + (f"\n\nLangkah:\n{steps}" if steps else "")
                        + "\n\nScript lengkap ada di file terlampir dan di dashboard → 🔌 Script Tracking (tombol Salin).")
        await team.send_file("developer", "kreatif", f"tracking-{host}.html", kit["snippet"],
                             f"Script tracking {host} v{version}. Tempel di footer semua halaman.")
        if kit["issues"]:
            await team.send("qa", "qa", f"Catatan QA untuk script {host}:\n- " + "\n- ".join(kit["issues"]))
        _log(host, f"Selesai: versi {version}.")
        return kit
    except Exception as e:  # noqa: BLE001 - laporkan ke Owner, jangan diam-diam gagal
        log.exception("Pembuatan script tracking %s gagal", host)
        _log(host, f"Gagal: {e}")
        await team.send("developer", "kreatif", f"⚠️ Script tracking {host} gagal dibuat: {e}")
        return None
    finally:
        JOBS[host]["running"] = False
        JOBS[host]["task_active"] = False


# ---------------------------------------------------------------- landing page (lander BeMob)

LANDER_MARKER = "AIADS-LANDER"


def lander_fix(click: str, host: str) -> str:
    """Script cadangan untuk footer landing page: semua tombol yang langsung ke website dibelokkan ke Click URL BeMob.
    Tanpa AI supaya selalu benar; lebih baik lagi jika href tombol diganti langsung."""
    return (f"<!-- {LANDER_MARKER} · tombol ke {_root(host)} dilewatkan BeMob. Tempel di FOOTER landing page. -->\n"
            "<script>\n(function () {\n"
            f"  var CLICK_URL = \"{click}\"; // Click URL BeMob (menu Landers / tracking domain)\n"
            f"  var WEBSITE = \"{_root(host)}\"; // website tujuan (tempat daftar/deposit)\n"
            "  function keWebsite(a) {\n"
            "    var h = (a.hostname || \"\").toLowerCase();\n"
            "    return h === WEBSITE || h.slice(-WEBSITE.length - 1) === \".\" + WEBSITE;\n"
            "  }\n"
            "  function perbaiki() {\n"
            "    try {\n"
            "      var links = document.querySelectorAll(\"a[href]\");\n"
            "      for (var i = 0; i < links.length; i++) if (keWebsite(links[i])) links[i].href = CLICK_URL;\n"
            "    } catch (e) {}\n"
            "  }\n"
            "  // Tombol yang muncul belakangan (pop-up, AJAX) ditangkap saat diklik.\n"
            "  document.addEventListener(\"click\", function (e) {\n"
            "    try {\n"
            "      var a = e.target && e.target.closest ? e.target.closest(\"a[href]\") : null;\n"
            "      if (a && keWebsite(a)) a.href = CLICK_URL;\n"
            "    } catch (err) {}\n"
            "  }, true);\n"
            "  if (document.readyState === \"loading\") document.addEventListener(\"DOMContentLoaded\", perbaiki);\n"
            "  else perbaiki();\n"
            "})();\n</script>\n"
            f"<!-- /{LANDER_MARKER} -->\n")


async def check_lander(team: Team | None, host: str, lander_url: str) -> dict | None:
    """Developer mengecek tombol landing page: harus ke Click URL BeMob, bukan langsung ke website."""
    record = site(host)
    lander = (record or {}).get("landers", {}).get(lander_url)
    if not lander:
        return None
    click = click_url(record, lander)
    results: list[dict] = []
    page = await fetch(lander_url)
    links = page_links(page.get("html", ""), page.get("url") or lander_url) if page["ok"] else []
    via = [l for l in links if is_tracker(host_of(l["url"]))]
    own = host_of(page.get("url") or lander_url)  # link internal landing page (bisa satu domain dengan website)
    direct = [l for l in links if _on_site(l["url"], host) and host_of(l["url"]) != own]
    label = lambda items: ", ".join(f"\"{l['text']}\"" for l in items[:4]) + (" …" if len(items) > 4 else "")
    if not page["ok"]:
        results.append({"ok": False, "text": f"Landing page {lander_url} tidak bisa dibuka "
                                             f"({page.get('error') or 'HTTP ' + str(page.get('status'))})."})
    else:
        if via:
            results.append({"ok": True, "text": f"{len(via)} tombol lewat BeMob: {label(via)}."})
            odd = [l for l in via if "/click" not in urlparse(l["url"]).path]
            if odd:
                results.append({"ok": None, "text": f"Tombol {label(odd)} ke BeMob tetapi bukan Click URL "
                                                    f"({odd[0]['url']}). Seharusnya {click or 'Click URL BeMob'}."})
        if direct:
            results.append({"ok": False, "text": f"{len(direct)} tombol langsung ke website tanpa BeMob: "
                                                 f"{label(direct)}. click_id hilang, konversinya tidak tercatat."})
        if not via and not direct:
            results.append({"ok": False, "text": "Tidak ada tombol yang mengarah ke BeMob. Kalau tombolnya memakai "
                                                 "JavaScript yang rumit, cek manual: klik tombolnya, alamat yang terbuka "
                                                 "harus lewat domain BeMob lalu sampai di website dengan ?click_id=."})
        if click and via and all(host_of(l["url"]) != host_of(click) for l in via):
            results.append({"ok": None, "text": f"Domain BeMob di tombol ({host_of(via[0]['url'])}) berbeda dari "
                                                f"{host_of(click)}. Pastikan milik akun BeMob yang sama."})
    ok = page["ok"] and bool(via) and not direct
    fix = lander_fix(click, host) if page["ok"] and click and not ok else ""
    check = {"ts": time.time(), "ok": ok, "results": results, "click_url": click}
    landers = dict(site(host).get("landers") or {})  # dibaca ulang setelah await
    if lander_url in landers:
        landers[lander_url] = {**landers[lander_url], "check": check, "fix": fix}
        _update(host, landers=landers)
    if team:
        lines = "\n".join(f"{'✅' if r['ok'] else '❌' if r['ok'] is False else 'ℹ️'} {r['text']}" for r in results)
        text = f"🔎 Cek tombol landing page {lander_url} (tujuan {host}): {'BENAR' if ok else 'PERLU DIPERBAIKI'}\n\n{lines}"
        if fix:
            text += (f"\n\nCara memperbaiki (pilih salah satu):\n1. Paling baik: ubah alamat semua tombol ajakan "
                     f"(Daftar, Main, dll.) menjadi {click}\n2. Atau tempel script terlampir di footer landing page. "
                     "Script membelokkan tombol yang langsung ke website supaya lewat BeMob.")
        elif not click and not ok:
            text += "\n\nClick URL BeMob belum diketahui. Isi di dashboard → 🔌 Script Tracking → Landing page."
        await team.send("developer", "kreatif", text)
        if fix:
            await team.send_file("developer", "kreatif", f"perbaikan-lander-{host_of(lander_url)}.html", fix,
                                 "Tempel di footer landing page (bukan di website tujuan).")
    return check


# ---------------------------------------------------------------- tes koneksi

def _arrival(final: str, host: str, via: str) -> dict:
    """Nilai halaman akhir: harus di website `host` dengan click_id."""
    params = parse_qs(urlparse(final).query)
    found = next((p for p in CLICK_PARAMS if params.get(p, [""])[0]), None)
    if not _on_site(final, host):
        return {"ok": False, "text": f"{via} mengarah ke {host_of(final)}, bukan ke {host}. Cek URL offer di BeMob."}
    if not found:
        return {"ok": False, "text": f"{via} sampai ke website, tetapi tanpa click_id ({final}). "
                                     "Tambahkan ?click_id={clickId} pada URL offer di BeMob."}
    return {"ok": True, "text": f"{via} meneruskan {found} ke website (1 kunjungan tes tercatat di BeMob)."}


async def trace(url: str, record: dict) -> list[dict]:
    """QA menelusuri jalur lengkap: link campaign → (landing page → tombol →) website?click_id=."""
    host = record["host"]
    async with checks._client() as http:
        final, html = await _follow(http, url)
        if not final:
            return [{"ok": False, "text": f"Link BeMob {url} tidak bisa dibuka."}]
        landers = {host_of(u) for u in record.get("landers") or {}} - {host}
        if _on_site(final, host) and host_of(final) not in landers:
            results = [_arrival(final, host, "Link BeMob")]
            if record.get("landers"):
                results.append({"ok": None, "text": "Link BeMob langsung ke website, landing page dilewati. Jika memang "
                                                    "ingin lewat landing page, atur alur campaign Landing → Offer di BeMob."})
            return results
        if is_tracker(host_of(final)):
            return [{"ok": False, "text": f"Link BeMob berhenti di {host_of(final)} (tidak meneruskan ke mana pun). "
                                          "Cek alur campaign di BeMob."}]
        lander = host_of(final)
        results = [{"ok": True, "text": f"Link BeMob membuka landing page {lander}."}]
        buttons = [l for l in page_links(html, final) if is_tracker(host_of(l["url"]))]
        if not buttons:
            direct = [l for l in page_links(html, final) if _on_site(l["url"], host) and host_of(l["url"]) != lander]
            results.append({"ok": False, "text": f"Tidak ada tombol di landing page {lander} yang lewat BeMob"
                                                 + (f"; tombol \"{direct[0]['text']}\" langsung ke website sehingga "
                                                    "click_id hilang" if direct else "")
                                                 + ". Lihat perbaikan dari Developer."})
            return results
        button = buttons[0]
        final2, _ = await _follow(http, button["url"])
        if not final2:
            results.append({"ok": False, "text": f"Tombol \"{button['text']}\" ({button['url']}) tidak bisa dibuka."})
        else:
            results.append(_arrival(final2, host, f"Tombol \"{button['text']}\" di landing page"))
    return results


async def test(team: Team, host: str, *, ai: bool = True, announce: bool = True) -> dict | None:
    """QA mengetes: script terpasang, click_id sampai ke website, postback BeMob -> RollerAds."""
    record = site(host)
    if not record:
        return None
    kit = record.get("kit") or {}
    results: list[dict] = []  # ok: True = lolos, False = gagal, None = info

    page = await fetch(record["url"])
    html = page.get("html", "") if page["ok"] else ""
    if not page["ok"]:
        results.append({"ok": False, "text": f"Website {record['url']} tidak bisa dibuka "
                                             f"({page.get('error') or 'HTTP ' + str(page.get('status'))})."})
    installed = installed_version(html) if html else None
    if page["ok"]:
        if not kit:
            results.append({"ok": False, "text": "Belum ada script untuk website ini. Klik Buatkan script."})
        elif installed == kit["version"]:
            results.append({"ok": True, "text": f"Script tracking versi terbaru ({installed}) terpasang di website."})
        elif installed:
            results.append({"ok": False, "text": f"Yang terpasang versi lama ({installed}), terbaru {kit['version']}. "
                                                 "Ganti dengan script terbaru."})
        else:
            results.append({"ok": False, "text": "Script tracking TIDAK ditemukan di halaman website. Pastikan sudah "
                                                 "ditempel di footer dan disimpan (hapus cache website jika ada)."})

    trackers = [u for u in record.get("tracking_urls", []) if u]
    if not trackers:
        results.append({"ok": None, "text": "Belum ada link campaign BeMob untuk website ini, jadi penerusan "
                                            "click_id belum bisa dites."})
    for url in trackers[:3]:
        if host_of(url) == host:
            results.append({"ok": False, "text": f"Link campaign langsung ke website tanpa BeMob ({url}). Konversi "
                                                 "tidak bisa dilacak; pakai link campaign BeMob."})
            continue
        results += await trace(url, record)

    for lander_url in record.get("landers") or {}:
        check = await check_lander(None, host, lander_url)
        if check:
            results.append({"ok": check["ok"], "text": f"Tombol landing page {lander_url}: " + (
                "semua lewat BeMob." if check["ok"] else
                next((r["text"] for r in check["results"] if r["ok"] is False), "perlu diperbaiki.")
                + " Kode perbaikannya ada di bagian Landing page.")})

    postback = postback_url(record)
    if not postback:
        results.append({"ok": False, "text": "URL postback BeMob belum diisi (Pengaturan → Script tracking)."})
    elif trackers and not any(host_of(u) == host_of(postback) for u in trackers):
        results.append({"ok": None, "text": f"Domain postback ({host_of(postback)}) berbeda dari domain link "
                                            "campaign. Pastikan keduanya milik akun BeMob yang sama."})
    if bemob.enabled():
        try:
            rollerads_pb = await bemob.rollerads_postback_url()
        except bemob.BeMobError as e:
            results.append({"ok": None, "text": f"Postback BeMob → RollerAds tidak bisa dicek: {e}"})
        else:
            results.append({"ok": bool(rollerads_pb), "text": "Postback BeMob → RollerAds terpasang."
                            if rollerads_pb else "Traffic source RollerAds di BeMob belum punya Postback URL."})
        ids = [c["id"] for c in record.get("campaigns", []) if c.get("id")]
        if ids:
            try:
                day = dt.datetime.now(config.TIMEZONE).date().isoformat()
                conv = sum(v["conversions"] for v in (await bemob.by_campaign(day, ids)).values())
                results.append({"ok": None, "text": f"Konversi tercatat di BeMob hari ini dari campaign website ini: {conv}."})
            except bemob.BeMobError as e:
                results.append({"ok": None, "text": f"Konversi BeMob tidak bisa dibaca: {e}"})
    else:
        results.append({"ok": None, "text": "BeMob API belum diisi, jadi postback BeMob → RollerAds tidak dicek otomatis."})

    connected = all(r["ok"] is not False for r in results)
    summary, checker = "", ""
    if ai and llm.any_configured():
        qa = AGENTS["qa"]
        lines = "\n".join(f"- {'✓' if r['ok'] else '✗' if r['ok'] is False else 'i'} {r['text']}" for r in results)
        extra = ""
        if installed and kit and installed != kit.get("version"):
            extra = "\n\nPotongan script yang terpasang di website:\n" + installed_code(html)[:6000]
        try:
            summary, provider = await llm.ask_ex(
                qa, f"Hasil tes koneksi tracking untuk {host}:\n{lines}{extra}\n\n"
                    f"Langkah tes manual dari review kode: {'; '.join(kit.get('test_steps', [])) or '-'}\n\n"
                    "Tulis kesimpulan untuk Owner: sudah terhubung atau belum, apa yang salah, dan langkah berikutnya "
                    "(termasuk tes manual daftar/deposit jika semua cek otomatis lolos).", max_tokens=2000, task="script")
            checker = llm.label(qa, provider)
        except llm.LLMError as e:
            summary = f"QA tidak bisa membuat kesimpulan: {e}"
    result = {"ts": time.time(), "results": results, "connected": connected, "summary": summary,
              "checker": checker, "installed": installed}
    _update(host, test=result, installed=bool(kit) and installed == kit.get("version"))
    if announce:
        lines = "\n".join(f"{'✅' if r['ok'] else '❌' if r['ok'] is False else 'ℹ️'} {r['text']}" for r in results)
        await team.send("qa", "qa", f"🧪 Tes koneksi tracking {host}: {'TERHUBUNG' if connected else 'BELUM TERHUBUNG'}\n\n"
                                    f"{lines}" + (f"\n\n{summary}" if summary else ""))
    return result


# ---------------------------------------------------------------- dipicu campaign baru & pemantauan

def register(url: str, *, site_url: str = "", hints: str = "", campaign: dict | None = None,
             lander_url: str = "", click: str = "") -> str:
    """Catat website (tanpa jaringan). `url` boleh link BeMob atau website. Mengembalikan host website.
    `lander_url`/`click`: landing page di depan website dan Click URL BeMob untuk tombolnya."""
    host = host_of(site_url) if site_url else ""
    tracker = url if url and is_tracker(host_of(url)) else ""
    if not host and url and not tracker:
        host = host_of(url)
    if not host:
        return ""
    record = site(host) or {}
    fields = {"url": site_url or record.get("url") or f"https://{host}/"}
    if hints:
        fields["hints"] = hints
    if tracker and tracker not in record.get("tracking_urls", []):
        fields["tracking_urls"] = (record.get("tracking_urls", []) + [tracker])[-5:]
    if campaign and campaign.get("id") and all(c.get("id") != campaign["id"] for c in record.get("campaigns", [])):
        fields["campaigns"] = record.get("campaigns", []) + [campaign]
    if lander_url:
        landers = dict(record.get("landers") or {})
        entry = landers.get(lander_url) or {"url": lander_url, "click_url": "", "check": None, "fix": ""}
        landers[lander_url] = {**entry, "click_url": click or entry.get("click_url", "")}
        fields["landers"] = landers
    _update(host, **fields)
    return host


def _base(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}/"


async def locate(url: str, site_url: str = "", lander_url: str = "") -> tuple[str, str, str]:
    """(host website, url website, url landing page). Link BeMob diikuti sampai ke website. Jika yang terbuka
    ternyata landing page (ada tombol ke BeMob), tombolnya ikut diklik supaya script dibuat untuk website tujuan."""
    if site_url:
        return host_of(site_url), site_url, lander_url
    if url and not is_tracker(host_of(url)):
        return host_of(url), url, lander_url
    async with checks._client() as http:
        final, html = await _follow(http, url) if url else ("", "")
        if final and not is_tracker(host_of(final)):
            buttons = [l for l in page_links(html, final) if is_tracker(host_of(l["url"]))]
            if buttons:
                final2, _ = await _follow(http, buttons[0]["url"])
                if final2 and not is_tracker(host_of(final2)) and host_of(final2) != host_of(final):
                    parsed = urlparse(final)
                    return host_of(final2), _base(final2), lander_url or f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
            return host_of(final), _base(final), lander_url
    if config.LANDING_PAGE_URLS:
        return host_of(config.LANDING_PAGE_URLS[0]), config.LANDING_PAGE_URLS[0], lander_url
    return "", "", lander_url


async def for_campaign(team: Team, campaign_id: int, spec: dict, site_url: str = "", hints: str = "",
                       lander_url: str = "", click: str = "") -> None:
    """Dipanggil setelah campaign dibuat: cek tombol landing page (jika ada), siapkan script website (jika belum
    ada), lalu tes koneksi jalur lengkapnya."""
    try:
        host, url, lander_url = await locate(spec.get("url", ""), site_url, lander_url)
        if not host:
            await team.send("developer", "kreatif", f"⚠️ Campaign {spec.get('title')}: website tujuannya tidak "
                            "diketahui, jadi script tracking belum dibuat. Buat dari dashboard → 🔌 Script Tracking.")
            return
        register(spec.get("url", ""), site_url=url, hints=hints,
                 campaign={"id": campaign_id, "title": spec.get("title", "")}, lander_url=lander_url, click=click)
        if lander_url:
            await team.send("developer", "kreatif", f"🧭 Campaign {spec.get('title')} lewat landing page {lander_url}. "
                            f"Script konversi dibuat untuk website tujuan {host}; landing page cukup dicek tombolnya.")
            await check_lander(team, host, lander_url)
        record = site(host)
        if record.get("kit"):
            await team.send("developer", "kreatif", f"ℹ️ Campaign {spec.get('title')} memakai website {host} yang sudah "
                            f"punya script tracking (versi {record['kit']['version']}). Tidak perlu tempel ulang.")
        elif not await build(team, host):
            return
        await test(team, host)
    except Exception:  # noqa: BLE001 - tugas latar belakang tidak boleh mematikan program
        log.exception("Script tracking untuk campaign #%s gagal", campaign_id)


def start_for_campaign(team: Team, campaign_id: int, spec: dict, site_url: str = "", hints: str = "",
                       lander_url: str = "", click: str = "") -> None:
    _spawn(for_campaign(team, campaign_id, spec, site_url, hints, lander_url, click))


def start_build(team: Team, host: str, then_test: bool = True) -> bool:
    """Mulai membuat script di latar belakang. False jika sedang dibuat."""
    if JOBS.get(host, {}).get("running"):
        return False

    async def run():
        if await build(team, host) and then_test:
            await test(team, host)
    JOBS[host] = {"running": True, "task_active": False, "log": [], "started": time.time()}
    _spawn(run())
    return True


def status_text() -> str:
    """Ringkasan kondisi tracking & landing page untuk laporan dan obrolan tim (tanpa jaringan)."""
    lines = []
    for host, r in sorted(sites().items()):
        kit, test_ = r.get("kit"), r.get("test")
        state = ("belum ada script" if not kit else
                 f"script v{kit['version']} " + ("terpasang" if r.get("installed") else "BELUM terpasang"))
        if test_:
            state += f", tes terakhir {'TERHUBUNG' if test_['connected'] else 'BELUM TERHUBUNG'}"
        lines.append(f"- {host}: {state}")
        for url, lander in (r.get("landers") or {}).items():
            ok = (lander.get("check") or {}).get("ok")
            lines.append(f"  - landing page {url}: " + ("tombol lewat BeMob" if ok else "belum dicek" if ok is None
                                                          else "tombol BERMASALAH, ada perbaikan dari Developer"))
    return "Status tracking website:\n" + ("\n".join(lines) if lines else "- belum ada website terdaftar")


async def monitor(team: Team) -> None:
    """Tiap cek landing page: apakah script masih terpasang dan tombol landing page masih lewat BeMob?
    Alert hanya saat berubah (gratis, tanpa AI)."""
    for host, record in sites().items():
        if JOBS.get(host, {}).get("running"):
            continue
        for lander_url, lander in (record.get("landers") or {}).items():
            was = (lander.get("check") or {}).get("ok")
            check = await check_lander(None, host, lander_url)
            if not check or was is None or check["ok"] == was:
                continue
            if check["ok"]:
                await team.send("tracking", "alert", f"🟢 Tombol landing page {lander_url} sudah lewat BeMob lagi. "
                                                     f"Konversi ke {host} kembali terlacak.")
            else:
                problem = next((r["text"] for r in check["results"] if r["ok"] is False), "")
                await team.send("tracking", "alert", f"🔴 Landing page {lander_url} bermasalah: {problem}\nKonversi "
                                                     f"ke {host} dari landing page ini tidak tercatat. Perbaikannya ada "
                                                     "di dashboard → 🔌 Script Tracking → Landing page.")
        kit = record.get("kit")
        if not kit or JOBS.get(host, {}).get("running"):
            continue
        page = await fetch(record["url"])
        if not page["ok"]:
            continue  # website mati sudah dilaporkan oleh cek landing page
        ok = installed_version(page.get("html", "")) == kit["version"]
        was = record.get("installed")
        _update(host, installed=ok)
        if ok and not was:
            await team.send("tracking", "alert", f"🟢 Script tracking {host} (versi {kit['version']}) terdeteksi "
                                                 "terpasang. QA menjalankan tes koneksi.")
            _spawn(test(team, host))
        elif was and not ok:
            await team.send("tracking", "alert", f"🔴 Script tracking di {host} HILANG atau berubah. Konversi dari "
                                                 "website ini tidak tercatat. Tempel ulang dari dashboard → 🔌 Script Tracking.")
