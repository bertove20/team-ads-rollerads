"""Klien BeMob REST API (baca saja): pendapatan & konversi per campaign/zone RollerAds, dan cek postback.

Traffic source RollerAds di BeMob mengirim campaign_id ke custom1 dan zone_id ke custom3.
Dokumentasi: https://api.bemob.com/docs/
"""
import re

import httpx

import config

API = "https://api.bemob.com/v1"
CAMPAIGN_PARAM = "custom1"  # {campaignId} RollerAds
ZONE_PARAM = "custom3"      # {zoneId} RollerAds


class BeMobError(Exception):
    pass


def enabled() -> bool:
    return bool(config.BEMOB_ACCESS_KEY and config.BEMOB_SECRET_KEY)


async def call(path: str, params=None, access: str = "", secret: str = ""):
    headers = {"X-ACCESS-KEY": access or config.BEMOB_ACCESS_KEY, "X-SECRET-KEY": secret or config.BEMOB_SECRET_KEY}
    try:
        async with httpx.AsyncClient(timeout=60) as http:
            response = await http.get(f"{API}{path}", params=params, headers=headers)
        payload = response.json()
    except (httpx.HTTPError, ValueError) as e:
        raise BeMobError(f"Gagal menghubungi BeMob API: {e}") from e
    if response.status_code >= 400 or not isinstance(payload, dict) or not payload.get("success"):
        message = payload.get("message") if isinstance(payload, dict) else payload
        raise BeMobError(f"BeMob menolak ({response.status_code}): {message or payload}")
    return payload["payload"]


async def traffic_sources(access: str = "", secret: str = "") -> list[dict]:
    return await call("/traffic-sources", access=access, secret=secret)


async def _report(day: str, group: list[str]) -> list[dict]:
    params = [
        ("date", "customDateTime"), ("from", f"{day} 00:00:00"), ("to", f"{day} 23:59:59"),
        ("timezone", str(config.TIMEZONE)), ("status", "all"), ("limit", "10000"),
        *[("groupBy", g) for g in group],
        *[("columns", f"reports-{g}") for g in group],
        ("columns", "reports-visits"), ("columns", "reports-conversions"),
        ("columns", "reports-revenue"), ("columns", "reports-cost"),
    ]
    return (await call("/report", params)).get("data", [])


def _id(value) -> int | None:
    value = str(value or "").strip()
    return int(value) if re.fullmatch(r"\d+", value) else None


async def by_campaign_zone(day: str, campaign_ids) -> dict[tuple[int, str], dict]:
    """(campaign RollerAds, zone) -> konversi & pendapatan. Hanya campaign yang diberikan (tanpa archived)."""
    allowed = {int(c) for c in campaign_ids}
    out: dict[tuple[int, str], dict] = {}
    for r in await _report(day, [CAMPAIGN_PARAM, ZONE_PARAM]):
        cid, zone = _id(r.get(f"reports-{CAMPAIGN_PARAM}")), _id(r.get(f"reports-{ZONE_PARAM}"))
        if cid not in allowed or zone is None:
            continue
        out[(cid, str(zone))] = {"conversions": int(r.get("reports-conversions") or 0),
                                 "revenue": float(r.get("reports-revenue") or 0)}
    return out


async def by_campaign(day: str, campaign_ids) -> dict[int, dict]:
    """Campaign RollerAds -> konversi & pendapatan hari itu. Hanya campaign yang diberikan (tanpa archived)."""
    allowed = {int(c) for c in campaign_ids}
    out: dict[int, dict] = {}
    for r in await _report(day, [CAMPAIGN_PARAM]):
        cid = _id(r.get(f"reports-{CAMPAIGN_PARAM}"))
        if cid in allowed:
            out[cid] = {"conversions": int(r.get("reports-conversions") or 0),
                        "revenue": float(r.get("reports-revenue") or 0)}
    return out


async def rollerads_postback_url() -> str | None:
    """Postback URL di traffic source RollerAds (None jika traffic source-nya tidak ada)."""
    for ts in await traffic_sources():
        if "roller" in ts.get("name", "").lower() and ts.get("status") == "active":
            return ts.get("postbackUrl") or ""
    return None
