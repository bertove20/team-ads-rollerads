"""Pemeriksaan otomatis landing page & link tracking (tanpa AI, jadi gratis)."""
import re
import time

import httpx

import config
import storage
from telegram_team import Team

SLOW_MS = 5000
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 13; SM-A536E) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36"
}


async def probe(http: httpx.AsyncClient, url: str) -> dict:
    start = time.perf_counter()
    try:
        response = await http.get(url)
    except httpx.HTTPError as e:
        return {"url": url, "ok": False, "error": type(e).__name__, "ms": None}
    ms = int((time.perf_counter() - start) * 1000)
    return {
        "url": url,
        "ok": response.status_code < 400,
        "status": response.status_code,
        "ms": ms,
        "kb": len(response.content) / 1024,
        "html": response.text if "html" in response.headers.get("content-type", "") else "",
        "error": None,
    }


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=20, follow_redirects=True, headers=HEADERS)


async def health_check(team: Team) -> None:
    """Hanya mengirim alert saat status berubah (mati -> hidup atau sebaliknya) agar grup tidak spam."""
    urls = config.LANDING_PAGE_URLS + config.TRACKING_URLS
    if not urls:
        return
    state = storage.get("health", {})
    details = {}
    async with _client() as http:
        for url in urls:
            result = await probe(http, url)
            details[url] = {
                "kind": "Landing page" if url in config.LANDING_PAGE_URLS else "Link tracking",
                "ok": result["ok"],
                "status": result.get("status"),
                "ms": result["ms"],
                "error": result["error"],
                "ts": time.time(),
            }
            was_ok = state.get(url, True)
            if not result["ok"] and was_ok:
                detail = result["error"] or f"HTTP {result['status']}"
                await team.send("tracking", "alert", f"🔴 DOWN: {url}\nPenyebab: {detail}\nTraffic berbayar "
                                "bisa terbuang. Owner, pertimbangkan pause campaign terkait.")
            elif result["ok"] and not was_ok:
                await team.send("tracking", "alert", f"🟢 Pulih: {url} ({result['ms']} ms)")
            elif result["ok"] and result["ms"] > SLOW_MS and not state.get(f"{url}#slow"):
                await team.send("tracking", "alert", f"🟡 Lambat: {url} butuh {result['ms']} ms untuk dimuat.")
            state[url] = result["ok"]
            state[f"{url}#slow"] = bool(result["ok"] and result["ms"] > SLOW_MS)
    storage.put("health", state)
    storage.put("health_detail", details)


async def qa_report(team: Team) -> None:
    if not config.LANDING_PAGE_URLS:
        await team.send("qa", "qa", "Belum ada LANDING_PAGE_URLS di .env, tidak ada yang diperiksa.")
        return
    lines = ["Hasil QA landing page (dicek sebagai HP Android):"]
    results = []
    async with _client() as http:
        for url in config.LANDING_PAGE_URLS:
            r = await probe(http, url)
            lines.append("")
            lines.append(url)
            if not r["ok"]:
                error = f"Tidak bisa dibuka: {r['error'] or 'HTTP ' + str(r['status'])}"
                lines.append(f"- ❌ {error}")
                results.append({"url": url, "checks": [{"ok": False, "text": error}]})
                continue
            html = r["html"]
            title = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
            checks = [
                (r["ms"] <= 3000, f"Waktu muat {r['ms']} ms (target <= 3000 ms)"),
                (r["kb"] <= 500, f"Ukuran HTML {r['kb']:.0f} KB"),
                (url.startswith("https://"), "Memakai HTTPS"),
                (bool(re.search(r'<meta[^>]+name=["\']viewport', html, re.I)), "Ada meta viewport (mobile)"),
                (bool(title and title.group(1).strip()), "Ada judul halaman"),
            ]
            lines += [f"- {'✅' if ok else '⚠️'} {text}" for ok, text in checks]
            results.append({"url": url, "checks": [{"ok": ok, "text": text} for ok, text in checks]})
    storage.put("qa_last", {"ts": time.time(), "results": results})
    await team.send("qa", "qa", "\n".join(lines))
