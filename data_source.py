"""Mengambil data performa per zone: demo, file CSV export BeMob, BeMob API, atau RollerAds API."""
import csv
import datetime as dt
import logging
import random
import time
from dataclasses import dataclass

import httpx

import bemob
import config
import rollerads

log = logging.getLogger(__name__)

INBOX_DIR = config.DATA_DIR / "inbox"
INBOX_DIR.mkdir(exist_ok=True)


@dataclass
class Row:
    campaign: str
    zone: str
    visits: int
    conversions: int
    cost: float
    revenue: float

    @property
    def profit(self) -> float:
        return self.revenue - self.cost

    @property
    def roi(self) -> float | None:
        return (self.profit / self.cost * 100) if self.cost else None


class DataSourceError(Exception):
    pass


def _number(value) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = str(value).replace("$", "").replace(",", "").replace("%", "").strip()
    return float(cleaned) if cleaned else 0.0


def _row_from_mapping(record: dict) -> Row:
    lowered = {str(k).strip().lower(): v for k, v in record.items()}

    def pick(field: str):
        return lowered.get(config.COLUMNS[field].lower())

    return Row(
        campaign=str(pick("campaign") or "-"),
        zone=str(pick("zone") or "-"),
        visits=int(_number(pick("visits"))),
        conversions=int(_number(pick("conversions"))),
        cost=_number(pick("cost")),
        revenue=_number(pick("revenue")),
    )


def _demo_rows() -> list[Row]:
    """Data contoh yang berubah tiap jam, untuk menguji sistem tanpa akun sungguhan."""
    rng = random.Random(int(time.time() // 3600))
    rows = []
    for campaign, bid_quality in [("ID-Popunder-01", 1.0), ("ID-InPagePush-02", 0.7)]:
        for _ in range(12):
            zone = str(rng.randint(100000, 999999))
            visits = rng.randint(200, 3000)
            cost = round(visits * rng.uniform(0.0008, 0.003), 2)
            quality = rng.random() * bid_quality
            conversions = 0 if quality < 0.35 else rng.randint(1, max(1, visits // 400))
            revenue = round(conversions * rng.uniform(1.5, 4.0), 2)
            rows.append(Row(campaign, zone, visits, conversions, cost, revenue))
    return rows


def _csv_rows() -> list[Row]:
    files = sorted(INBOX_DIR.glob("*.csv"), key=lambda p: p.stat().st_mtime)
    if not files:
        raise DataSourceError(
            f"Belum ada file CSV di {INBOX_DIR}. Export laporan BeMob (group by zone) ke folder itu."
        )
    with files[-1].open(encoding="utf-8-sig", newline="") as f:
        return [_row_from_mapping(record) for record in csv.DictReader(f)]


async def _bemob_rows() -> list[Row]:
    if not config.BEMOB_REPORT_URL or not config.BEMOB_API_KEY:
        raise DataSourceError("BEMOB_REPORT_URL / BEMOB_API_KEY belum diisi di .env.")
    today = dt.datetime.now(config.TIMEZONE).date()
    url = config.BEMOB_REPORT_URL.format(date_from=today.isoformat(), date_to=today.isoformat())
    async with httpx.AsyncClient(timeout=30) as http:
        try:
            response = await http.get(url, headers={config.BEMOB_AUTH_HEADER: config.BEMOB_API_KEY})
            response.raise_for_status()
        except httpx.HTTPError as e:
            raise DataSourceError(f"Gagal mengambil data BeMob: {e}") from e
    payload = response.json()
    records = payload.get(config.BEMOB_ROWS_KEY, []) if isinstance(payload, dict) else payload
    return [_row_from_mapping(record) for record in records]


async def _rollerads_rows() -> list[Row]:
    try:
        campaigns = await rollerads.campaigns()  # tanpa campaign archived
        records = await rollerads.stats("campaign-zone", campaigns)
    except rollerads.RollerAdsError as e:
        raise DataSourceError(str(e)) from e
    tracked = None  # pendapatan & konversi asli dari BeMob, per (campaign, zone)
    if bemob.enabled():
        try:
            tracked = await bemob.by_campaign_zone(rollerads.today(), campaigns)
        except bemob.BeMobError as e:
            log.warning("Data BeMob tidak bisa diambil, memakai konversi RollerAds: %s", e)
    rows = []
    for record in records:
        campaign = campaigns.get(int(record.get("campaign_id") or 0))
        if not campaign:  # pengaman ganda: jangan pernah memakai campaign archived
            continue
        zone = str(record.get("zone_id") or "-")
        conversions = int(_number(record.get("cnt_conversion")))
        revenue = conversions * config.ROLLERADS_PAYOUT_USD
        if tracked is not None:
            t = tracked.get((campaign["id"], zone), {"conversions": 0, "revenue": 0.0})
            conversions, revenue = t["conversions"], t["revenue"]
        rows.append(Row(
            campaign=campaign["title"],
            zone=zone,
            visits=int(_number(record.get("cnt_click") or record.get("cnt_impression"))),
            conversions=conversions,
            cost=_number(record.get("amt_imoney")),
            revenue=revenue,
        ))
    return rows


async def fetch_rows() -> list[Row]:
    """Data hari ini, per campaign x zone."""
    if config.DATA_SOURCE == "demo":
        return _demo_rows()
    if config.DATA_SOURCE == "csv":
        return _csv_rows()
    if config.DATA_SOURCE == "bemob":
        return await _bemob_rows()
    if config.DATA_SOURCE == "rollerads":
        return await _rollerads_rows()
    raise DataSourceError(f"DATA_SOURCE tidak dikenal: {config.DATA_SOURCE}")
