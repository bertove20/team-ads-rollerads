"""Login dashboard: password, sesi, dan akun lihat-saja."""
import time

import auth
import config


def test_password_disimpan_sebagai_hash_dan_bisa_diverifikasi():
    hashed = auth.hash_password("Kopi-Hitam-Pagi-42")
    assert auth.is_hashed(hashed) and "Kopi-Hitam-Pagi-42" not in hashed
    assert auth.verify_password("Kopi-Hitam-Pagi-42", hashed)
    assert not auth.verify_password("salah", hashed)


def test_dua_hash_berbeda_untuk_password_sama():
    assert auth.hash_password("Kopi-Hitam-Pagi-42") != auth.hash_password("Kopi-Hitam-Pagi-42")


def test_password_lama_teks_biasa_masih_bisa_login():
    assert auth.verify_password("teks-lama-123", "teks-lama-123")


def test_password_lemah_ditolak():
    assert auth.weakness("pendek")
    assert auth.weakness("1234567890")
    assert auth.weakness("aaaaaaaaaaaa")
    assert auth.weakness("Kopi-Hitam-Pagi-42") is None
    assert auth.weakness("kudajingkrakdipagihari") is None  # frasa panjang boleh


def test_sesi_berlaku_lalu_bisa_dihentikan(mem_storage):
    token = auth.create_session("1.2.3.4", "browser")
    assert auth.session_role(token) == "owner"
    auth.end_session(token)
    assert auth.session_role(token) is None


def test_sesi_kedaluwarsa_karena_lama_tidak_dipakai(mem_storage):
    token = auth.create_session("1.2.3.4", "browser")
    data = mem_storage[auth.SESSIONS_KEY]
    for s in data.values():
        s["last"] = time.time() - auth.IDLE_SECONDS - 10
    assert auth.session_role(token) is None


def test_keluarkan_perangkat_lain_menyisakan_sesi_ini(mem_storage):
    mine = auth.create_session("1.1.1.1", "punya saya")
    auth.create_session("2.2.2.2", "perangkat lain")
    auth.create_session("3.3.3.3", "perangkat lain 2")
    assert auth.end_all_sessions(keep=mine) == 2
    assert auth.session_role(mine) == "owner"


def test_akun_lihat_saja_dikenali(mem_storage, monkeypatch):
    monkeypatch.setattr(config, "DASHBOARD_PASSWORD", auth.hash_password("Owner-Kuat-2026"))
    monkeypatch.setattr(config, "DASHBOARD_VIEWER_PASSWORD", auth.hash_password("Lihat-Saja-2026"))
    assert auth.role_of_password("Owner-Kuat-2026") == "owner"
    assert auth.role_of_password("Lihat-Saja-2026") == "viewer"
    assert auth.role_of_password("bukan-password") is None


def test_token_sesi_tidak_disimpan_mentah(mem_storage):
    token = auth.create_session("1.2.3.4", "browser")
    assert token not in str(mem_storage[auth.SESSIONS_KEY])
