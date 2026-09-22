"""Setelan bersama untuk semua tes.

Tes TIDAK PERNAH menyentuh database asli, file .env asli, internet, atau API RollerAds/BeMob/AI:
database dialihkan ke file sementara, dan semua pemanggilan luar diganti tiruan.
"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Database sementara + pengaturan netral, dipasang SEBELUM modul program di-import.
_TMP = tempfile.mkdtemp(prefix="aiads-test-")
os.environ.update({
    "DATA_DIR": _TMP,
    "TIMEZONE": "Asia/Jakarta",
    "TELEGRAM_GROUP_ID": "0",
    "ROLLERADS_API_KEY": "",
    "ANTHROPIC_API_KEY": "",
    "OPENROUTER_API_KEY": "",
    "DASHBOARD_PASSWORD": "",
})

import config  # noqa: E402  (harus setelah environment disiapkan)

config.DB_PATH = Path(_TMP) / "test.db"
config.DATA_DIR = Path(_TMP)


@pytest.fixture()
def mem_storage(monkeypatch):
    """storage.get/put diganti kamus di memori, jadi tiap tes mulai bersih."""
    import storage
    data: dict = {}
    monkeypatch.setattr(storage, "get", lambda k, d=None: data.get(k, d))
    monkeypatch.setattr(storage, "put", lambda k, v: data.__setitem__(k, v))
    return data


class FakeTeam:
    """Pengganti Team: pesan hanya dikumpulkan, tidak dikirim ke Telegram."""

    online = False

    def __init__(self):
        self.msgs: list[tuple[str, str]] = []

    async def send(self, agent_key, topic, text, **kwargs):
        self.msgs.append((agent_key, text))
        return None

    async def send_file(self, agent_key, topic, filename, content, caption=""):
        self.msgs.append((agent_key, f"FILE {filename}"))

    @property
    def last(self) -> str:
        return self.msgs[-1][1] if self.msgs else ""


@pytest.fixture()
def team():
    return FakeTeam()
