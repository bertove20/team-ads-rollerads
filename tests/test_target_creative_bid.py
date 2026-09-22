"""Target CPA dari nilai pemain, optimasi creative, dan bid khusus per zone."""
import asyncio

import autopause
import config
import creatives
import meeting
import rollerads
import targets


# ---------------------------------------------------------------- target CPA

def _ltv(monkeypatch, players=50, arpu=10.0, d30=None, matured=50):
    import ltv
    total = {"players": players, "arpu": arpu, "arpu_d7": arpu * 0.6, "arpu_d30": d30,
             "matured_d7": matured, "matured_d30": matured if d30 else 0}
    monkeypatch.setattr(ltv, "report", lambda days=30: {"ready": True, "total": total, "by_campaign": [
        {"name": "Pop ID", "players": players, "arpu": arpu * 1.5, "arpu_d7": arpu, "arpu_d30": d30,
         "matured_d7": matured, "matured_d30": matured if d30 else 0}]})


def test_target_cpa_dari_nilai_pemain(monkeypatch):
    monkeypatch.setattr(config, "TARGET_MARGIN_PCT", 30)
    _ltv(monkeypatch, arpu=10.0, d30=12.0)
    value, note = targets.max_cpa()
    assert value == 8.4 and "30%" in note        # 12 x (100% - 30%)


def test_target_cpa_per_campaign_lebih_diutamakan(monkeypatch):
    monkeypatch.setattr(config, "TARGET_MARGIN_PCT", 0)
    _ltv(monkeypatch, arpu=10.0, d30=None)
    umum, _ = targets.max_cpa()
    khusus, _ = targets.max_cpa("Pop ID")
    assert khusus > umum                          # campaign ini pemainnya lebih bernilai


def test_data_pemain_kurang_pakai_angka_manual(monkeypatch):
    import ltv
    monkeypatch.setattr(ltv, "report", lambda days=30: {"ready": False})
    monkeypatch.setattr(config, "AUTOPAUSE_MAX_CPA_USD", 3.0)
    value, note = targets.max_cpa()
    assert value == 3.0 and "Pengaturan" in note


def test_tanpa_data_dan_tanpa_setelan_tidak_ada_target(monkeypatch):
    import ltv
    monkeypatch.setattr(ltv, "report", lambda days=30: {"ready": False})
    monkeypatch.setattr(config, "AUTOPAUSE_MAX_CPA_USD", 0)
    assert targets.max_cpa()[0] is None


def test_autopause_memakai_target_cpa():
    alasan = autopause.judge(cost=20.0, conversions=4, account_cost=20.0, revenue=100.0,
                             max_cpa=3.0, cpa_note="dari nilai pemain")
    aman = autopause.judge(cost=20.0, conversions=4, account_cost=20.0, revenue=100.0, max_cpa=8.0)
    assert alasan and "CPA $5.00" in alasan and "nilai pemain" in alasan
    assert aman is None


# ---------------------------------------------------------------- creative

def _creative_rows(mem_storage, monkeypatch, rows, day="2026-09-22"):
    import bemob
    monkeypatch.setattr(config, "ROLLERADS_API_KEY", "x")
    monkeypatch.setattr(config, "ROLLERADS_PAYOUT_USD", 2.0)
    monkeypatch.setattr(bemob, "enabled", lambda: False)

    async def campaigns():
        return {7: {"id": 7, "title": "Pop ID", "status": "active"}}

    async def stats(group, ids, day=None):
        assert group == "creative"
        return rows
    monkeypatch.setattr(rollerads, "campaigns", campaigns)
    monkeypatch.setattr(rollerads, "stats", stats)
    return asyncio.run(creatives.collect(day))


def _crow(cid, imp, clicks, conv, cost):
    return {"campaign_id": 7, "campaign_title": "Pop ID", "creative_id": cid, "cnt_impression": imp,
            "cnt_click": clicks, "cnt_conversion": conv, "amt_imoney": cost}


def test_creative_boros_diusulkan_dihentikan(mem_storage, monkeypatch):
    _creative_rows(mem_storage, monkeypatch, [_crow(11, 10000, 300, 5, 8.0), _crow(12, 9000, 250, 0, 7.0)])
    usul = [s for s in creatives.suggestions() if s["type"] == "pause_creative"]
    assert usul and usul[0]["values"] == ["12"] and "11" in usul[0]["zones"]


def test_creative_pemenang_memicu_usulan_variasi_baru(mem_storage, monkeypatch):
    _creative_rows(mem_storage, monkeypatch, [_crow(11, 10000, 300, 5, 8.0), _crow(12, 9000, 250, 3, 7.0)])
    usul = [s for s in creatives.suggestions() if s["type"] == "new_creative"]
    assert usul and usul[0]["campaign"] == "Pop ID"


def test_satu_satunya_creative_tidak_dihentikan(mem_storage, monkeypatch):
    _creative_rows(mem_storage, monkeypatch, [_crow(11, 9000, 250, 0, 9.0)])
    assert [s for s in creatives.suggestions() if s["type"] == "pause_creative"] == []


def test_ctr_dan_cr_dihitung(mem_storage, monkeypatch):
    data = _creative_rows(mem_storage, monkeypatch, [_crow(11, 10000, 200, 10, 5.0)])
    stat = data["Pop ID|11"]
    assert stat["ctr"] == 2.0 and stat["cr"] == 5.0


def test_api_menolak_menghapus_semua_creative(monkeypatch):
    async def detail(cid):
        return {"campaign_title": "Pop ID", "campaign_url_target": "https://t", "campaign_bid": 1.0,
                "campaign_status": "active", "creative": [{"creative_id": 11, "creative_title": "A"}]}
    monkeypatch.setattr(rollerads, "detail", detail)
    try:
        asyncio.run(rollerads.keep_creatives(7, []))
        assert False, "seharusnya ditolak"
    except rollerads.RollerAdsError as e:
        assert "Minimal satu creative" in str(e)


# ---------------------------------------------------------------- bid khusus zone

def test_bid_zone_di_luar_batas_ditolak(monkeypatch):
    async def detail(cid):
        return {"campaign_status": "active"}
    monkeypatch.setattr(rollerads, "detail", detail)
    try:
        asyncio.run(rollerads.set_zone_bids(7, ["111"], config.CAMPAIGN_MAX_BID_USD + 1))
        assert False, "seharusnya ditolak"
    except rollerads.RollerAdsError as e:
        assert "batas pengaman" in str(e)


def test_usulan_bid_zone_lolos_guardrail():
    class Snap:
        all_zones = {"111", "222"}
        campaigns = {"Pop ID": object()}
    action = {"type": "zone_bid", "campaign": "Pop ID", "zones": ["111", "222"], "current_value": 1.0,
              "new_value": 1.2, "reason": "x", "website": "", "brief": "", "hours": [], "values": []}
    accepted, _ = meeting.validate([action], Snap())
    assert len(accepted) == 1
    mahal, rejected = meeting.validate([{**action, "new_value": config.CAMPAIGN_MAX_BID_USD + 5}], Snap())
    assert mahal == [] and rejected
