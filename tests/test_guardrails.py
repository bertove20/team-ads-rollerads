"""Batas pengaman: hal-hal yang melindungi uang Owner. Ini bagian paling berbahaya kalau rusak."""
import pytest

import config
import meeting


def _action(**fields):
    base = {"type": "other", "campaign": "Pop ID", "zones": [], "current_value": 0, "new_value": 0,
            "reason": "tes", "website": "", "brief": "", "hours": []}
    return {**base, **fields}


class Snap:
    all_zones = {"111", "222", "333"}
    campaigns = {"Pop ID": object()}


def test_bid_naik_melebihi_batas_ditolak():
    accepted, rejected = meeting.validate(
        [_action(type="change_bid", current_value=1.0, new_value=2.0)], Snap())
    assert accepted == [] and "ditolak" in rejected[0].lower()


def test_bid_naik_wajar_diterima():
    accepted, _ = meeting.validate([_action(type="change_bid", current_value=1.0, new_value=1.1)], Snap())
    assert len(accepted) == 1


def test_budget_melebihi_batas_ditolak():
    accepted, rejected = meeting.validate(
        [_action(type="change_daily_budget", new_value=config.MAX_CAMPAIGN_DAILY_BUDGET_USD + 10)], Snap())
    assert accepted == [] and rejected


def test_campaign_karangan_ditolak():
    accepted, rejected = meeting.validate(
        [_action(type="change_bid", campaign="Campaign Hantu", current_value=1, new_value=1.1)], Snap())
    assert accepted == [] and "tidak ada di data" in rejected[0]


def test_blacklist_hanya_zone_yang_ada(mem_storage, monkeypatch):
    import storage
    monkeypatch.setattr(storage, "recently_proposed_zones", lambda *a, **k: set())
    accepted, _ = meeting.validate([_action(type="blacklist_zones", zones=["111", "999"])], Snap())
    assert accepted[0]["zones"] == ["111"]


def test_whitelist_butuh_minimal_dua_zone():
    accepted, rejected = meeting.validate([_action(type="whitelist_zones", zones=["111"])], Snap())
    assert accepted == [] and "minimal 2 zone" in rejected[0]


def test_dayparting_minimal_enam_jam():
    kurang, _ = meeting.validate([_action(type="set_dayparting", hours=[1, 2, 3])], Snap())
    cukup, _ = meeting.validate([_action(type="set_dayparting", hours=[8, 9, 10, 11, 12, 13, 14])], Snap())
    assert kurang == [] and cukup[0]["hours"] == [8, 9, 10, 11, 12, 13, 14]


def test_freq_cap_diluar_rentang_ditolak():
    accepted, _ = meeting.validate([_action(type="set_freq_cap", new_value=200, hours=[24])], Snap())
    assert accepted == []


@pytest.mark.parametrize("cost,conversions,expected_none", [(5, 0, True), (50, 0, False)])
def test_autopause_hanya_setelah_spend_cukup(cost, conversions, expected_none):
    import autopause
    reason = autopause.judge(cost, conversions, account_cost=cost, revenue=0)
    assert (reason is None) is expected_none
