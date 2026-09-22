"""Auto-scale (menggas campaign untung), rapor kinerja tim, dan nilai pemain."""
import datetime as dt
import time

import autoscale
import config
import ltv
import scorecard


UNTUNG = {"conversions": 6, "roi": 120.0, "cost": 19.0, "profit": 22.0}
DETAIL = {"campaign_spent_day": 20.0, "campaign_bid": 1.0}


def test_campaign_untung_dan_budget_habis_diusulkan_naik_budget():
    kind, old, new, _ = autoscale.decide(UNTUNG, DETAIL)
    assert kind == "change_daily_budget" and new > old
    assert new <= config.MAX_CAMPAIGN_DAILY_BUDGET_USD


def test_budget_masih_sisa_maka_yang_dinaikkan_bid():
    hemat = {**UNTUNG, "cost": 5.0}
    kind, old, new, _ = autoscale.decide(hemat, DETAIL)
    assert kind == "change_bid" and new > old


def test_campaign_rugi_tidak_di_scale():
    assert autoscale.decide({**UNTUNG, "roi": -20.0}, DETAIL) is None


def test_konversi_terlalu_sedikit_tidak_di_scale():
    assert autoscale.decide({**UNTUNG, "conversions": 1}, DETAIL) is None


def test_tidak_melewati_batas_budget_maksimal():
    mentok = {"campaign_spent_day": config.MAX_CAMPAIGN_DAILY_BUDGET_USD, "campaign_bid": 1.0}
    hasil = autoscale.decide({**UNTUNG, "cost": config.MAX_CAMPAIGN_DAILY_BUDGET_USD}, mentok)
    assert hasil is None


def test_kenaikan_bid_tidak_melewati_batas_perubahan(monkeypatch):
    monkeypatch.setattr(config, "AUTOSCALE_STEP_PCT", 90)  # jauh di atas MAX_BID_CHANGE_PCT
    assert autoscale.decide({**UNTUNG, "cost": 5.0}, DETAIL) is None


def test_penilaian_tindakan_berhasil_dan_gagal():
    naik = scorecard._verdict({"profit": 1.0, "cpa": 2.0}, {"profit": 5.0, "cpa": 1.0})
    turun = scorecard._verdict({"profit": 5.0, "cpa": 1.0}, {"profit": 1.0, "cpa": 2.0})
    kosong = scorecard._verdict(None, {"profit": 1.0})
    assert naik[0] == "berhasil" and turun[0] == "gagal" and kosong[0] == "belum jelas"


def test_sumber_usulan_dikenali():
    assert scorecard._source({"by": "Tim AI (usulan #3 disetujui Owner)"}) == "Rapat tim"
    assert scorecard._source({"by": "x", "reason": "Auto-scale: untung"}) == "Auto-scale"
    assert scorecard._source({"by": "Owner (dashboard)"}) == "Owner"
    assert scorecard._source({"by": "sistem"}) == "Auto-pause"


def test_nilai_pemain_dihitung_dari_kejadian(mem_storage, monkeypatch):
    import storage
    now = time.time()
    events = [
        {"ts": now - 20 * 86400, "click_id": "A", "event": "reg", "value": 0, "campaign": "Pop ID", "zone": "1", "site": "s"},
        {"ts": now - 19 * 86400, "click_id": "A", "event": "dep", "value": 10, "campaign": "Pop ID", "zone": "1", "site": "s"},
        {"ts": now - 10 * 86400, "click_id": "A", "event": "dep", "value": 15, "campaign": "Pop ID", "zone": "1", "site": "s"},
        {"ts": now - 18 * 86400, "click_id": "B", "event": "reg", "value": 0, "campaign": "Pop ID", "zone": "1", "site": "s"},
    ]
    monkeypatch.setattr(storage, "player_events_since", lambda since: [e for e in events if e["ts"] >= since])
    monkeypatch.setattr(storage, "player_event_count", lambda: len(events))
    data = ltv.report()
    assert data["ready"] and data["total"]["players"] == 2
    assert data["total"]["depositors"] == 1 and data["total"]["repeat"] == 1
    assert data["total"]["revenue"] == 25.0 and data["total"]["arpu"] == 12.5
    assert data["total"]["arpu_d1"] == 5.0     # deposit dalam 1 hari pertama: $10 untuk 2 pemain
    assert data["total"]["arpu_d7"] == 5.0     # deposit kedua terjadi hari ke-10, di luar jendela 7 hari
    assert data["total"]["arpu_d30"] is None   # belum ada pemain yang berumur 30 hari
    assert data["total"]["matured_d30"] == 0
    assert data["by_campaign"][0]["name"] == "Pop ID"


def test_token_pencatat_dibuat_sekali(mem_storage):
    assert ltv.token() == ltv.token() and len(ltv.token()) > 20
