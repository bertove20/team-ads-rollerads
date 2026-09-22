"""Buat / ganti password dashboard dari terminal (dipakai di VPS, tempat dashboard dibuka lewat internet).

Jalankan:  .venv/bin/python set_password.py
Password disimpan sebagai hash di .env. Semua perangkat yang sedang login akan dikeluarkan.
"""
import getpass
import sys

import auth
import settings


def main() -> int:
    print("Buat password dashboard (minimal 10 karakter, campur huruf, angka, simbol).")
    password = getpass.getpass("Password baru: ")
    problem = auth.weakness(password)
    if problem:
        print(f"Ditolak: {problem}")
        return 1
    if getpass.getpass("Ulangi password: ") != password:
        print("Ditolak: ulangi password tidak sama.")
        return 1
    try:
        settings.save({"DASHBOARD_PASSWORD": password})
    except settings.SettingsError as e:
        print(f"Ditolak: {e}")
        return 1
    ended = auth.end_all_sessions()
    print(f"Password tersimpan (sebagai hash). {ended} sesi login lama dikeluarkan.")
    print("Jalankan ulang program agar langsung berlaku:  sudo systemctl restart ai-ads-team")
    return 0


if __name__ == "__main__":
    sys.exit(main())
