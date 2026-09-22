"""Target CPA dari nilai pemain: berapa biaya per konversi yang MASIH untung (tanpa AI, gratis).

Selama ini batas CPA diisi manual (tebakan). Padahal sistem sekarang tahu nilai asli satu pemain dari
script di website (lihat ltv.py). Dari situ bisa dihitung:

    CPA impas   = nilai rata-rata satu pemain
    CPA target  = CPA impas x (100% - margin yang diinginkan)

Angka ini dipakai otomatis oleh auto-pause, auto-scale, dan penilaian zone/negara/jam, jadi semua keputusan
memakai ukuran yang sama dan berdasar data, bukan tebakan.

Kalau data pemain belum cukup (atau script pencatat belum dipasang), sistem memakai angka manual di Pengaturan
dan mengatakannya terus terang.
"""
import logging

import config
import ltv

log = logging.getLogger(__name__)


def _value_of(stat: dict) -> tuple[float, str] | None:
    """Nilai satu pemain yang paling bisa dipercaya dari data yang ada."""
    if not stat or stat.get("players", 0) < config.TARGET_MIN_PLAYERS:
        return None
    if stat.get("arpu_d30") and stat.get("matured_d30", 0) >= config.TARGET_MIN_PLAYERS:
        return stat["arpu_d30"], "nilai pemain 30 hari"
    if stat.get("arpu_d7") and stat.get("matured_d7", 0) >= config.TARGET_MIN_PLAYERS:
        return stat["arpu_d7"], "nilai pemain 7 hari"
    if stat.get("arpu"):
        return stat["arpu"], "nilai pemain rata-rata"
    return None


def break_even(campaign: str = "") -> tuple[float | None, str]:
    """(CPA impas, penjelasan). None = data pemain belum cukup."""
    try:
        data = ltv.report()
    except Exception:  # noqa: BLE001 - target tidak boleh mematikan alur lain
        log.exception("Nilai pemain tidak bisa dibaca")
        return None, "data nilai pemain tidak terbaca"
    if not data.get("ready"):
        return None, "belum ada data pemain (script pencatat belum terpasang)"
    if campaign:
        for row in data.get("by_campaign", []):
            if row["name"] == campaign:
                found = _value_of(row)
                if found:
                    return found[0], f"{found[1]} campaign ini ({row['players']} pemain)"
    found = _value_of(data.get("total") or {})
    if found:
        return found[0], f"{found[1]} semua campaign ({data['total']['players']} pemain)"
    return None, f"pemain belum cukup (minimal {config.TARGET_MIN_PLAYERS})"


def max_cpa(campaign: str = "") -> tuple[float | None, str]:
    """(CPA maksimal yang masih untung, penjelasan). Dipakai auto-pause, auto-scale, dan penilaian data."""
    value, note = break_even(campaign)
    if value:
        margin = max(0.0, min(config.TARGET_MARGIN_PCT, 90)) / 100
        target = round(value * (1 - margin), 4)
        return target, (f"${target:.2f} = {note} ${value:.2f} dikurangi margin {config.TARGET_MARGIN_PCT:g}%")
    if config.AUTOPAUSE_MAX_CPA_USD > 0:
        return config.AUTOPAUSE_MAX_CPA_USD, f"${config.AUTOPAUSE_MAX_CPA_USD:.2f} dari Pengaturan ({note})"
    return None, note


def text() -> str:
    """Ringkasan untuk prompt agent & laporan."""
    target, note = max_cpa()
    if not target:
        return ""
    value, _ = break_even()
    lines = [f"TARGET CPA (dihitung dari nilai pemain): maksimal ${target:.2f} per konversi. {note}."]
    if value:
        lines.append(f"CPA impas ${value:.2f}. Campaign/zone/negara dengan CPA di atas ${target:.2f} berarti "
                     "margin tergerus; di bawahnya layak di-scale.")
    return "\n".join(lines)
