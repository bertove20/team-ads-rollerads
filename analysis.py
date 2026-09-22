"""Perhitungan angka dilakukan oleh kode (bukan AI) supaya akurat. AI hanya membaca hasilnya."""
import datetime as dt
import time
from collections import defaultdict
from dataclasses import dataclass, field

import config
import data_source
import storage
from data_source import Row

SNAPSHOT_EVERY_SECONDS = 10 * 60  # riwayat untuk grafik tren disimpan maks tiap 10 menit


@dataclass
class Totals:
    visits: int = 0
    conversions: int = 0
    cost: float = 0.0
    revenue: float = 0.0

    def add(self, row: Row) -> None:
        self.visits += row.visits
        self.conversions += row.conversions
        self.cost += row.cost
        self.revenue += row.revenue

    @property
    def profit(self) -> float:
        return self.revenue - self.cost

    @property
    def roi(self) -> float | None:
        return (self.profit / self.cost * 100) if self.cost else None

    @property
    def cpa(self) -> float | None:
        return (self.cost / self.conversions) if self.conversions else None

    def as_dict(self) -> dict:
        return {
            "visits": self.visits,
            "conversions": self.conversions,
            "cost": round(self.cost, 2),
            "revenue": round(self.revenue, 2),
            "profit": round(self.profit, 2),
            "roi": None if self.roi is None else round(self.roi, 1),
            "cpa": None if self.cpa is None else round(self.cpa, 2),
        }

    def describe(self) -> str:
        roi = f"{self.roi:.0f}%" if self.roi is not None else "-"
        cpa = f"${self.cpa:.2f}" if self.cpa is not None else "-"
        return (
            f"visits {self.visits:,} | konv {self.conversions} | spend ${self.cost:.2f} | "
            f"revenue ${self.revenue:.2f} | profit ${self.profit:.2f} | ROI {roi} | CPA {cpa}"
        )


@dataclass
class Snapshot:
    total: Totals
    campaigns: dict[str, Totals]
    all_zones: set[str] = field(default_factory=set)
    bad_zones: list[Row] = field(default_factory=list)
    good_zones: list[Row] = field(default_factory=list)
    rows: list[Row] = field(default_factory=list)
    fetched_at: float = 0.0

    @property
    def over_budget(self) -> bool:
        return self.total.cost >= config.MAX_DAILY_SPEND_USD


def analyze(rows: list[Row]) -> Snapshot:
    total = Totals()
    campaigns: dict[str, Totals] = defaultdict(Totals)
    for row in rows:
        total.add(row)
        campaigns[row.campaign].add(row)

    bad = [
        r
        for r in rows
        if r.cost >= config.ZONE_WASTE_USD
        and (r.conversions == 0 or (r.roi is not None and r.roi < config.ZONE_MIN_ROI_PCT))
    ]
    good = [
        r
        for r in rows
        if r.conversions >= 2 and r.roi is not None and r.roi >= config.ZONE_GOOD_ROI_PCT
    ]
    bad.sort(key=lambda r: r.profit)
    good.sort(key=lambda r: r.profit, reverse=True)
    return Snapshot(total, dict(campaigns), {r.zone for r in rows}, bad, good, rows, time.time())


_latest: Snapshot | None = None


async def latest(max_age: float = 60) -> Snapshot:
    """Ambil data terbaru (di-cache `max_age` detik) dan simpan riwayatnya untuk grafik tren."""
    global _latest
    if _latest and time.time() - _latest.fetched_at < max_age:
        return _latest
    _latest = analyze(await data_source.fetch_rows())
    storage.put("last_facts", facts_text(_latest))
    if time.time() - storage.get("last_snapshot_ts", 0) >= SNAPSHOT_EVERY_SECONDS:
        storage.put("last_snapshot_ts", time.time())
        storage.save_snapshot(
            dt.datetime.now(config.TIMEZONE).date().isoformat(),
            _latest.total.as_dict(),
            {name: t.as_dict() for name, t in _latest.campaigns.items()},
        )
    return _latest


def zone_status(row: Row, snap: Snapshot) -> str:
    if row in snap.bad_zones:
        return "boros"
    if row in snap.good_zones:
        return "bagus"
    return "normal"


def insights(snap: Snapshot) -> list[dict]:
    """Evaluasi otomatis dalam bahasa sederhana. level: good | warning | critical | info."""
    out: list[dict] = []
    t = snap.total

    def add(level: str, title: str, text: str) -> None:
        out.append({"level": level, "title": title, "text": text})

    if t.cost == 0:
        add("info", "Belum ada spend hari ini", "Belum ada data biaya iklan untuk dievaluasi.")
        return out

    if t.profit >= 0:
        add("good", f"Hari ini untung ${t.profit:.2f}",
            f"Dari spend ${t.cost:.2f} kembali ${t.revenue:.2f} (ROI {t.roi:.0f}%).")
    else:
        add("critical", f"Hari ini rugi ${-t.profit:.2f}",
            f"Spend ${t.cost:.2f} baru menghasilkan ${t.revenue:.2f} (ROI {t.roi:.0f}%). "
            "Fokus memotong zone boros di bawah.")

    used = t.cost / config.MAX_DAILY_SPEND_USD * 100 if config.MAX_DAILY_SPEND_USD else 0
    if used >= 100:
        add("critical", "Batas spend harian terlewati",
            f"Spend ${t.cost:.2f} melewati batas ${config.MAX_DAILY_SPEND_USD:g}. "
            "Cek campaign dan pause bila perlu.")
    elif used >= 80:
        add("warning", f"Spend sudah {used:.0f}% dari batas harian",
            f"Sisa ${config.MAX_DAILY_SPEND_USD - t.cost:.2f} sebelum batas ${config.MAX_DAILY_SPEND_USD:g}.")

    if t.conversions == 0 and t.cost >= config.ZONE_WASTE_USD * 3:
        add("critical", "Tidak ada konversi sama sekali",
            f"Spend ${t.cost:.2f} tanpa satu pun konversi. Kemungkinan tracking/postback bermasalah "
            "atau landing page tidak berfungsi. Cek menu Landing Page.")

    for name, c in sorted(snap.campaigns.items(), key=lambda kv: kv[1].profit):
        if c.roi is None or c.cost < config.ZONE_WASTE_USD:
            continue
        if c.roi < 0:
            add("warning", f"Campaign {name} rugi ${-c.profit:.2f}",
                f"ROI {c.roi:.0f}% dari spend ${c.cost:.2f}. Periksa zone boros-nya, atau turunkan bid.")
        elif c.roi >= config.ZONE_GOOD_ROI_PCT:
            add("good", f"Campaign {name} bagus (ROI {c.roi:.0f}%)",
                f"Profit ${c.profit:.2f}. Kandidat untuk dinaikkan budget-nya secara bertahap.")

    if snap.bad_zones:
        waste = sum(r.cost for r in snap.bad_zones)
        add("warning", f"{len(snap.bad_zones)} zone boros menghabiskan ${waste:.2f}",
            "Zone ini spend tinggi tanpa hasil. Pertimbangkan blacklist (lihat menu Zone, bisa disalin).")
    if snap.good_zones:
        profit = sum(r.profit for r in snap.good_zones)
        add("good", f"{len(snap.good_zones)} zone menguntungkan (profit ${profit:.2f})",
            "Pertimbangkan whitelist atau naikkan bid sedikit di zone ini.")
    return out


def _zone_line(r: Row) -> str:
    roi = f"{r.roi:.0f}%" if r.roi is not None else "-"
    return (
        f"  zone {r.zone} ({r.campaign}): spend ${r.cost:.2f}, konv {r.conversions}, "
        f"revenue ${r.revenue:.2f}, ROI {roi}"
    )


def facts_text(snap: Snapshot, max_zones: int = 15) -> str:
    """Ringkasan angka untuk dibaca para agent."""
    lines = [f"DATA HARI INI (sumber: {config.DATA_SOURCE})", f"TOTAL: {snap.total.describe()}"]
    lines.append(f"Batas spend harian: ${config.MAX_DAILY_SPEND_USD:g}")
    lines.append("PER CAMPAIGN:")
    for name, totals in sorted(snap.campaigns.items()):
        lines.append(f"  {name}: {totals.describe()}")
    lines.append(
        f"ZONE BOROS (spend >= ${config.ZONE_WASTE_USD:g} tanpa konversi atau ROI < "
        f"{config.ZONE_MIN_ROI_PCT:g}%):"
    )
    lines += [_zone_line(r) for r in snap.bad_zones[:max_zones]] or ["  (tidak ada)"]
    lines.append(f"ZONE BAGUS (ROI >= {config.ZONE_GOOD_ROI_PCT:g}%, konv >= 2):")
    lines += [_zone_line(r) for r in snap.good_zones[:max_zones]] or ["  (tidak ada)"]
    return "\n".join(lines)


def hourly_report(snap: Snapshot) -> str:
    waste = sum(r.cost for r in snap.bad_zones)
    lines = ["Laporan per jam", "", f"Total: {snap.total.describe()}", ""]
    for name, totals in sorted(snap.campaigns.items()):
        lines.append(f"- {name}: {totals.describe()}")
    lines += [
        "",
        f"Zone boros: {len(snap.bad_zones)} (total ${waste:.2f})",
        f"Zone bagus: {len(snap.good_zones)}",
    ]
    return "\n".join(lines)
