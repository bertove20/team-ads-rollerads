"""Kontrol akses dashboard: login wajib, akun lihat-saja tidak bisa mengubah, dan pencatat pemain aman."""
import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import auth
import config
import dashboard
import ltv

HOST = {"Host": "localhost:8090"}


def _run(coro):
    return asyncio.run(coro)


async def _client(team):
    app = web.Application(middlewares=[dashboard.guard])
    app["team"] = team
    app.add_routes(dashboard.routes)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


@pytest.fixture()
def owner_login(monkeypatch, mem_storage):
    monkeypatch.setattr(config, "DASHBOARD_PASSWORD", auth.hash_password("Owner-Kuat-2026"))
    monkeypatch.setattr(config, "DASHBOARD_VIEWER_PASSWORD", auth.hash_password("Lihat-Saja-2026"))
    monkeypatch.setattr(config, "DASHBOARD_HOST", "127.0.0.1")
    monkeypatch.setattr(config, "DASHBOARD_DOMAIN", "")
    return True


def test_tanpa_login_semua_tertutup(team, owner_login):
    async def go():
        c = await _client(team)
        try:
            halaman = await c.get("/", headers=HOST, allow_redirects=False)
            api = await c.get("/api/overview", headers=HOST)
            return halaman.status, halaman.headers.get("Location"), api.status
        finally:
            await c.close()
    status, lokasi, api_status = _run(go())
    assert status == 302 and lokasi == "/login" and api_status == 401


def test_akun_lihat_saja_tidak_bisa_mengubah(team, owner_login):
    async def go():
        c = await _client(team)
        try:
            await c.post("/api/login", json={"password": "Lihat-Saja-2026"}, headers=HOST)
            baca = await c.get("/api/auth/sessions", headers=HOST)
            ubah = await c.post("/api/settings", json={"values": {"MAX_DAILY_SPEND_USD": "999"}}, headers=HOST)
            keluar = await c.post("/api/logout", headers=HOST)
            return baca.status, ubah.status, (await ubah.json()).get("error", ""), keluar.status
        finally:
            await c.close()
    baca, ubah, pesan, keluar = _run(go())
    assert baca == 200 and ubah == 403 and "hanya bisa melihat" in pesan and keluar == 200


def test_owner_bisa_membaca_dan_tercatat_di_audit(team, owner_login, monkeypatch):
    import storage
    dicatat = []
    monkeypatch.setattr(storage, "add_audit", lambda *a: dicatat.append(a))

    async def go():
        c = await _client(team)
        try:
            masuk = await c.post("/api/login", json={"password": "Owner-Kuat-2026"}, headers=HOST)
            sesi = await c.get("/api/auth/sessions", headers=HOST)
            return masuk.status, sesi.status, (await sesi.json())["role"]
        finally:
            await c.close()
    masuk, sesi, role = _run(go())
    assert masuk == 200 and sesi == 200 and role == "owner"
    assert any("login berhasil" in str(a) for a in dicatat)


def test_pencatat_pemain_menolak_token_salah(team, owner_login):
    async def go():
        c = await _client(team)
        try:
            salah = await c.get("/collect?t=palsu&c=A&e=reg&v=0&x=1", headers=HOST)
            benar = await c.get(f"/collect?t={ltv.token()}&c=A&e=dep&v=5&x=TX1", headers=HOST)
            dobel = await c.get(f"/collect?t={ltv.token()}&c=A&e=dep&v=5&x=TX1", headers=HOST)
            kosong = await c.get(f"/collect?t={ltv.token()}&c=&e=dep&v=5&x=TX2", headers=HOST)
            return salah.status, benar.status, (await benar.json()), (await dobel.json()), kosong.status
        finally:
            await c.close()
    salah, benar, isi, dobel, kosong = _run(go())
    assert salah == 403 and benar == 200 and isi["baru"] is True
    assert dobel["baru"] is False   # txid sama tidak dihitung dua kali
    assert kosong == 400


def test_alamat_domain_asing_ditolak(team, owner_login):
    async def go():
        c = await _client(team)
        try:
            r = await c.get("/login", headers={"Host": "penipu.com"})
            return r.status
        finally:
            await c.close()
    assert _run(go()) == 421
