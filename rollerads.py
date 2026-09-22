"""Klien RollerAds API: membaca campaign & statistik, pause/aktifkan, dan membuat campaign.

Campaign berstatus archived adalah campaign salah: tidak pernah dibaca maupun diubah.
Dokumentasi: https://my.rollerads.com/api/docs
"""
import datetime as dt
import json
import time

import httpx

import config

API = "https://api.rollerads.com/v2"
READABLE_STATUSES = ("active", "paused", "stopped")

FORMATS = {2: "OnClick / Popunder", 1: "Push notification", 3: "In-page push", 7: "Native"}
BID_MODELS = {
    1: "CPC", 2: "CPM", 3: "Model 3", 5: "Smart CPC", 6: "Smart CPM",
    7: "CPA Goal CPC", 8: "CPA Goal CPM", 9: "Model 9", 10: "Model 10", 11: "Model 11",
}
# Kombinasi yang diterima API (dicek langsung ke RollerAds)
FORMAT_BID_MODELS = {2: [2, 6, 8, 3], 1: [1, 5, 2, 7, 3], 3: [1, 5], 7: [9, 10, 11, 3]}
CREATIVE_FORMATS = {1, 3, 7}  # format yang wajib punya creative (judul, deskripsi, gambar)


class RollerAdsError(Exception):
    pass


async def call(method: str, path: str, *, params=None, fields: dict | None = None, body: dict | None = None,
               token: str = "") -> dict:
    """Panggil API. `fields` dikirim sebagai multipart/form-data (nilai dict/list diubah ke JSON),
    `body` sebagai JSON."""
    token = token or config.ROLLERADS_API_KEY
    if not token:
        raise RollerAdsError("Token API RollerAds belum diisi (ROLLERADS_API_KEY).")
    files = None
    if fields is not None:
        files = {k: (None, v if isinstance(v, str) else json.dumps(v)) for k, v in fields.items()}
    try:
        async with httpx.AsyncClient(timeout=60) as http:
            response = await http.request(method, f"{API}{path}", params=params, files=files, json=body,
                                          headers={"X-ACCESS-TOKEN": token})
        payload = response.json()
    except (httpx.HTTPError, ValueError) as e:
        raise RollerAdsError(f"Gagal menghubungi RollerAds API: {e}") from e
    if response.status_code >= 400 or not isinstance(payload, dict) or "error" in payload:
        error = payload.get("error") if isinstance(payload, dict) else None
        message = error.get("message") if isinstance(error, dict) else error
        details = error.get("elements") if isinstance(error, dict) else None
        if details:
            message = f"{message}: " + "; ".join(f"{k}: {', '.join(v)}" for k, v in details.items())
        raise RollerAdsError(f"RollerAds menolak ({response.status_code}): {message or payload}")
    return payload


async def account(token: str = "") -> dict:
    payload = await call("GET", "/whoami", token=token)
    info = (payload.get("response") or {}).get("account") or {}
    return {"title": info.get("account_title", "-"), "balance": float(payload.get("balance_current") or 0)}


async def campaigns(token: str = "") -> dict[int, dict]:
    """Campaign yang boleh dibaca, id -> info. Campaign archived tidak pernah ikut."""
    payload = await call("GET", "/campaigns/list", params=[("statuses[]", s) for s in READABLE_STATUSES],
                         token=token)
    return {
        int(c["campaign_id"]): {
            "id": int(c["campaign_id"]),
            "title": c["campaign_title"],
            "status": c["campaign_status"],
            "format_id": c.get("format_id"),
            "bid_model_id": c.get("bid_model_id"),
        }
        for c in payload.get("response", [])
        if c.get("campaign_status") in READABLE_STATUSES
    }


def today() -> str:
    return dt.datetime.now(config.TIMEZONE).date().isoformat()


async def stats(group: str, campaign_ids, day: str | None = None) -> list[dict]:
    """Statistik hari `day` (default hari ini) hanya untuk campaign yang diberikan."""
    ids = [str(i) for i in campaign_ids]
    if not ids:
        return []
    day = day or today()
    rows, page = [], 1
    while True:
        payload = await call("GET", "/statistics/adv", params={
            "group": group,
            "filters[campaign_id]": "in|" + ",".join(ids),
            "filters[ts]": f"bw|{day} 00:00:00;{day} 23:59:59",
            "limit": 10000,
            "page": page,
        })
        result = payload.get("response") or {}
        rows += [r for r in result.get("rows", []) if str(r.get("campaign_id")) in ids]
        if page >= int((result.get("page") or {}).get("max") or 1):
            return rows
        page += 1


async def detail(campaign_id: int) -> dict:
    """Detail campaign. Menolak campaign archived/deleted (tidak boleh dibaca atau diubah)."""
    info = (await call("GET", f"/campaigns/{campaign_id}")).get("response") or {}
    if info.get("campaign_status") not in READABLE_STATUSES:
        raise RollerAdsError(f"Campaign #{campaign_id} berstatus {info.get('campaign_status')}, tidak boleh diubah.")
    return info


async def update(campaign_id: int, **changes) -> dict:
    """Ubah field campaign (mis. campaign_status, campaign_bid, campaign_spent_day). Mengembalikan detail SEBELUM
    diubah. Judul, URL, dan bid wajib ikut dikirim API, jadi diambil dari detail saat ini."""
    info = await detail(campaign_id)
    await call("POST", f"/campaigns/{campaign_id}", fields={"campaign": {
        "campaign_title": info["campaign_title"],
        "campaign_url_target": info["campaign_url_target"],
        "campaign_bid": info["campaign_bid"],
        **changes,
    }})
    return info


async def set_status(campaign_id: int, status: str) -> dict:
    """Pause (`paused`) atau aktifkan (`active`) campaign. Menolak campaign archived/deleted."""
    if status not in ("active", "paused"):
        raise RollerAdsError(f"Status tidak dikenal: {status}")
    return await update(campaign_id, campaign_status=status)


async def blacklist_zones(campaign_id: int, zones: list[str]) -> list[int]:
    """Tambahkan zone ke blacklist (deny) campaign. Mengembalikan zone yang benar-benar ada di blacklist sesudahnya."""
    ids = sorted({int(z) for z in zones if str(z).isdigit()})
    if not ids:
        raise RollerAdsError("Tidak ada ID zone yang valid (harus angka ID zone RollerAds).")
    await detail(campaign_id)  # pastikan bukan campaign archived
    payload = await call("POST", f"/campaigns/{campaign_id}/zone", body={"allow": [], "deny": ids})
    result = payload.get("response") if isinstance(payload.get("response"), dict) else payload
    denied = {int(z) for z in ((result.get("target") or {}).get("deny") or []) if str(z).isdigit()}
    return [z for z in ids if z in denied] if denied else ids


_countries: dict[str, int] = {}
_countries_ts = 0.0


async def countries() -> dict[str, int]:
    """Kode negara ISO2 (mis. ID) -> ID negara RollerAds. Di-cache 1 hari."""
    global _countries, _countries_ts
    if not _countries or time.time() - _countries_ts > 86400:
        payload = await call("GET", "/misc/countries")
        _countries = {c["country_iso2"].upper(): int(c["country_id"]) for c in payload.get("response", [])
                      if c.get("country_iso2")}
        _countries_ts = time.time()
    return _countries


# ---------------------------------------------------------------- pembuatan campaign

# Bentuk "spec" campaign yang dipakai dashboard, agent AI, dan usulan.
SPEC_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "format_id": {"type": "integer", "enum": list(FORMATS)},
        "bid_model_id": {"type": "integer", "enum": list(BID_MODELS)},
        "bid": {"type": "number"},
        "url": {"type": "string"},
        "countries": {"type": "array", "items": {"type": "string"}},
        "daily_budget": {"type": "number"},
        "total_budget": {"type": "number"},
        "status": {"type": "string", "enum": ["paused", "active"]},
        "creative_title": {"type": "string"},
        "creative_descr": {"type": "string"},
        "image_360": {"type": "string"},
        "image_192": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": ["title", "format_id", "bid_model_id", "bid", "url", "countries", "daily_budget",
                 "total_budget", "status", "creative_title", "creative_descr", "image_360", "image_192", "reason"],
    "additionalProperties": False,
}


def _num(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


async def validate_spec(spec: dict) -> tuple[dict, list[str]]:
    """Bersihkan & cek spec terhadap aturan API dan batas pengaman. Mengembalikan (spec, daftar kesalahan)."""
    s = {
        "title": str(spec.get("title") or "").strip(),
        "format_id": int(_num(spec.get("format_id"))),
        "bid_model_id": int(_num(spec.get("bid_model_id"))),
        "bid": round(_num(spec.get("bid")), 4),
        "url": str(spec.get("url") or "").strip(),
        "countries": [c.strip().upper() for c in (spec.get("countries") or []) if str(c).strip()],
        "daily_budget": round(_num(spec.get("daily_budget")), 2),
        "total_budget": round(_num(spec.get("total_budget")), 2),
        "status": spec.get("status") if spec.get("status") in ("active", "paused") else "paused",
        "creative_title": str(spec.get("creative_title") or "").strip(),
        "creative_descr": str(spec.get("creative_descr") or "").strip(),
        "image_360": str(spec.get("image_360") or "").strip(),
        "image_192": str(spec.get("image_192") or "").strip(),
        "reason": str(spec.get("reason") or "").strip(),
    }
    errors = []
    if not 3 <= len(s["title"]) <= 128:
        errors.append("Nama campaign harus 3-128 karakter.")
    if not s["url"].startswith(("http://", "https://")) or len(s["url"]) > 2048:
        errors.append("URL tujuan harus diawali http:// atau https://.")
    if s["format_id"] not in FORMATS:
        errors.append("Format iklan tidak dikenal.")
    elif s["bid_model_id"] not in FORMAT_BID_MODELS[s["format_id"]]:
        allowed = ", ".join(BID_MODELS[b] for b in FORMAT_BID_MODELS[s["format_id"]])
        errors.append(f"Model bid tidak tersedia untuk {FORMATS[s['format_id']]}. Pilihan: {allowed}.")
    if not 0 < s["bid"] <= config.CAMPAIGN_MAX_BID_USD:
        errors.append(f"Bid harus lebih dari $0 dan maksimal ${config.CAMPAIGN_MAX_BID_USD:g} (batas pengaman).")
    if not 0 < s["daily_budget"] <= config.MAX_CAMPAIGN_DAILY_BUDGET_USD:
        errors.append(f"Budget harian wajib diisi, maksimal ${config.MAX_CAMPAIGN_DAILY_BUDGET_USD:g} (batas pengaman).")
    if s["total_budget"] < 0:
        errors.append("Budget total tidak boleh negatif (0 = tanpa batas total).")
    if not s["countries"]:
        errors.append("Pilih minimal satu negara (kode 2 huruf, mis. ID).")
    else:
        known = await countries()
        unknown = [c for c in s["countries"] if c not in known]
        if unknown:
            errors.append(f"Kode negara tidak dikenal: {', '.join(unknown)}. Pakai kode 2 huruf, mis. ID, MY.")
    if s["format_id"] in CREATIVE_FORMATS:
        max_descr = 90 if s["format_id"] == 7 else 128
        if not 3 <= len(s["creative_title"]) <= 64:
            errors.append("Judul iklan (creative) harus 3-64 karakter.")
        if len(s["creative_descr"]) > max_descr:
            errors.append(f"Deskripsi iklan maksimal {max_descr} karakter.")
        if not s["image_360"].startswith(("http://", "https://")):
            errors.append("URL gambar besar (image_360) wajib untuk format ini.")
        if s["format_id"] != 7 and not s["image_192"].startswith(("http://", "https://")):
            errors.append("URL ikon (image_192) wajib untuk format ini.")
    return s, errors


def describe_spec(s: dict) -> str:
    lines = [
        f"Buat campaign baru: {s['title']}",
        f"Format: {FORMATS.get(s['format_id'], s['format_id'])} · Model bid: {BID_MODELS.get(s['bid_model_id'], s['bid_model_id'])}"
        f" · Bid: ${s['bid']:g}",
        f"Negara: {', '.join(s['countries'])} · Budget harian: ${s['daily_budget']:g}"
        + (f" · Budget total: ${s['total_budget']:g}" if s["total_budget"] else ""),
        f"URL: {s['url']}",
        f"Status awal: {'aktif langsung' if s['status'] == 'active' else 'paused (aktifkan setelah dicek)'}",
    ]
    if s["format_id"] in CREATIVE_FORMATS:
        lines.append(f"Iklan: \"{s['creative_title']}\" - {s['creative_descr']}")
    return "\n".join(lines)


async def create(spec: dict) -> int:
    """Buat campaign dari spec yang sudah lolos validate_spec. Mengembalikan ID campaign."""
    s, errors = await validate_spec(spec)
    if errors:
        raise RollerAdsError(" ".join(errors))
    known = await countries()
    fields = {
        "campaign": {
            "format_id": s["format_id"],
            "bid_model_id": s["bid_model_id"],
            "campaign_title": s["title"],
            "campaign_url_target": s["url"],
            "campaign_bid": s["bid"],
            "campaign_spent_day": s["daily_budget"],
            "campaign_spent_total": s["total_budget"],
            "campaign_status": s["status"],
        },
        "targetings": {"geo": {"targeting_type": "geo", "targeting_value": {
            "country": [known[c] for c in s["countries"]], "countryType": True,
            "region": [], "regionType": True, "city": [], "cityType": True,
        }}},
    }
    if s["format_id"] in CREATIVE_FORMATS:
        creative = {"creative_title": s["creative_title"], "creative_descr": s["creative_descr"],
                    "image_360": s["image_360"]}
        if s["image_192"]:
            creative["image_192"] = s["image_192"]
        fields["creatives"] = [creative]
    result = (await call("POST", "/campaigns", fields=fields)).get("response")
    campaign_id = result.get("campaign_id") if isinstance(result, dict) else None
    if not campaign_id and isinstance(result, dict):
        campaign_id = (result.get("campaign") or {}).get("campaign_id")
    return int(campaign_id or 0)
