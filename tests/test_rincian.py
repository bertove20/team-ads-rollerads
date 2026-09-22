"""Rincian per negara/device/jam: pengambilan data, usulan otomatis, dan pengamannya."""
import asyncio

import breakdown
import config
import meeting
import rollerads


def _row(campaign="Pop ID", country="ID", os="Android", hour=3, clicks=100, conv=0, cost=5.0):
    return {"campaign_id": 7, "campaign_title": campaign, "country_iso2": country, "country_name": country,
            "dev_os_family": os, "ts_title": f"2026-09-22 {hour:02d}:00:00", "cnt_click": clicks,
            "cnt_conversion": conv, "amt_imoney": cost}


def _collect(rows, monkeypatch, mem_storage, day="2026-09-22"):
    import bemob
    monkeypatch.setattr(config, "ROLLERADS_API_KEY", "x")
    monkeypatch.setattr(config, "ROLLERADS_PAYOUT_USD", 2.0)
    monkeypatch.setattr(bemob, "enabled", lambda: False)

    async def campaigns():
        return {7: {"id": 7, "title": "Pop ID", "status": "active"}}

    async def stats(group, ids, day=None):
        assert group == breakdown.GROUP
        return rows

    monkeypatch.setattr(rollerads, "campaigns", campaigns)
    monkeypatch.setattr(rollerads, "stats", stats)
    return asyncio.run(breakdown.collect(day))


def test_jam_dibaca_dari_kolom_waktu():
    assert breakdown._hour_of({"ts_title": "2026-09-22 14:00:00"}) == 14
    assert breakdown._hour_of({"hour": "7"}) == 7
    assert breakdown._hour_of({"ts_title": "tanpa jam"}) is None


def test_data_dikelompokkan_per_negara_os_dan_jam(mem_storage, monkeypatch):
    rows = [_row(country="ID", conv=3, cost=6.0, hour=20), _row(country="MY", conv=0, cost=4.0, hour=3, os="iOS")]
    summary = _collect(rows, monkeypatch, mem_storage)
    assert summary["country"]["ID"]["conversions"] == 3
    assert summary["country"]["MY"]["cost"] == 4.0
    assert summary["os"]["iOS"]["conversions"] == 0
    assert summary["hour"]["20"]["conversions"] == 3 and summary["has_hour"] is True
    # pendapatan = konversi x payout (BeMob mati)
    assert summary["country"]["ID"]["revenue"] == 6.0 and summary["country"]["ID"]["roi"] == 0.0


def test_jam_boros_diusulkan_dimatikan(mem_storage, monkeypatch):
    rows = [_row(hour=h, conv=2, cost=3.0) for h in range(8, 22)]        # jam siang menghasilkan
    rows += [_row(hour=h, conv=0, cost=5.0) for h in (1, 2, 3)]          # dini hari boros
    _collect(rows, monkeypatch, mem_storage)
    usul = [s for s in breakdown.suggestions() if s["type"] == "set_dayparting"]
    assert usul, "jam boros seharusnya diusulkan"
    assert set(usul[0]["hours"]).isdisjoint({1, 2, 3}) and len(usul[0]["hours"]) >= breakdown.MIN_HOURS_KEPT


def test_negara_rugi_diusulkan_dikecualikan(mem_storage, monkeypatch):
    rows = [_row(country="ID", conv=5, cost=10.0, hour=10), _row(country="MY", conv=0, cost=9.0, hour=11)]
    _collect(rows, monkeypatch, mem_storage)
    usul = [s for s in breakdown.suggestions() if s["type"] == "exclude_country"]
    assert usul and usul[0]["values"] == ["MY"]


def test_os_rugi_diusulkan_dikecualikan(mem_storage, monkeypatch):
    rows = [_row(os="Android", conv=5, cost=10.0, hour=10), _row(os="iOS", conv=0, cost=9.0, hour=11)]
    _collect(rows, monkeypatch, mem_storage)
    usul = [s for s in breakdown.suggestions() if s["type"] == "exclude_os"]
    assert usul and usul[0]["values"] == ["iOS"]


def test_semua_rugi_tidak_diusulkan_kecuali_semua(mem_storage, monkeypatch):
    """Kalau semua negara rugi, jangan usulkan mengecualikan semuanya (lebih tepat pause campaign)."""
    rows = [_row(country="ID", conv=0, cost=9.0, hour=10), _row(country="MY", conv=0, cost=9.0, hour=11)]
    _collect(rows, monkeypatch, mem_storage)
    assert [s for s in breakdown.suggestions() if s["type"] == "exclude_country"] == []


def test_belum_cukup_spend_belum_diusulkan(mem_storage, monkeypatch):
    rows = [_row(country="ID", conv=2, cost=5.0, hour=10), _row(country="MY", conv=0, cost=0.5, hour=11)]
    _collect(rows, monkeypatch, mem_storage)
    assert [s for s in breakdown.suggestions() if s["type"] == "exclude_country"] == []


def test_usulan_kecualikan_tanpa_nilai_ditolak_guardrail():
    class Snap:
        all_zones = set()
        campaigns = {"Pop ID": object()}
    action = {"type": "exclude_country", "campaign": "Pop ID", "zones": [], "current_value": 0, "new_value": 0,
              "reason": "x", "website": "", "brief": "", "hours": [], "values": []}
    accepted, rejected = meeting.validate([action], Snap())
    assert accepted == [] and rejected
