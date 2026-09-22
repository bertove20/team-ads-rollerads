@echo off
cd /d "%~dp0"
if not exist .venv (
    rem Pakai Python dari PATH; jika tidak ada, pakai Python bawaan Laragon.
    set "PY=python"
    python --version >nul 2>&1 || for /d %%D in (C:\laragon\bin\python\python-*) do set "PY=%%D\python.exe"
    call echo Membuat virtual environment dengan %%PY%%...
    call "%%PY%%" -m venv .venv
    if not exist .venv\Scripts\python.exe (
        echo Python tidak ditemukan. Pasang Python dari python.org atau aktifkan Python di Laragon.
        pause
        exit /b
    )
)
call .venv\Scripts\activate.bat
echo Memeriksa kebutuhan program...
pip install -q --disable-pip-version-check -r requirements.txt
if not exist .env copy .env.example .env >nul

rem Menandai bahwa program dijalankan lewat file ini, agar tombol "jalankan ulang" di dashboard bisa dipakai.
set AIADS_LAUNCHER=1
set PORT=8090
for /f "tokens=2 delims==" %%P in ('findstr /b "DASHBOARD_PORT=" .env') do set PORT=%%P
echo.
echo Dashboard akan terbuka di browser: http://localhost:%PORT%
echo Semua pengaturan (API key, token bot, dll.) bisa diisi di menu Pengaturan pada dashboard.
echo Jangan tutup jendela ini selama tim AI bekerja.
echo.
start "" cmd /c "timeout /t 5 /nobreak >nul & start http://localhost:%PORT%"

:mulai
python main.py
if %errorlevel%==3 (
    echo.
    echo Menjalankan ulang dengan pengaturan baru...
    goto mulai
)
pause
