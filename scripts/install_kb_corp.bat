@echo off
setlocal

rem ============================================================
rem  Setup access to Tradesoft KB via kb.tradesoft.corp
rem  For Windows users (run as administrator)
rem ============================================================
rem
rem  Script:
rem   1) adds "192.168.128.56 kb.tradesoft.corp" to C:\Windows\System32\drivers\etc\hosts
rem   2) imports self-signed cert kb.tradesoft.corp.crt (next to this .bat)
rem      into the current user's trusted root store
rem   3) opens https://kb.tradesoft.corp/ in the browser
rem ============================================================

set "HOSTS_FILE=C:\Windows\System32\drivers\etc\hosts"
set "HOST_LINE=192.168.128.56 kb.tradesoft.corp"
set "CERT_FILE=%~dp0kb.tradesoft.corp.crt"
set "SITE_URL=https://kb.tradesoft.corp/"

rem ---- check admin rights ----
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [!] Administrator rights are required.
    echo     Right-click this file -^> "Run as administrator".
    pause
    exit /b 1
)

echo.
echo === Step 1/3: add hosts entry (if missing) ===

rem look for an existing line by hostname
findstr /i /c:"kb.tradesoft.corp" "%HOSTS_FILE%" >nul 2>&1
if %errorlevel% equ 0 (
    echo [ok] kb.tradesoft.corp already in hosts - skipping.
) else (
    echo %HOST_LINE%>>"%HOSTS_FILE%"
    if %errorlevel% equ 0 (
        echo [ok] Added: %HOST_LINE%
    ) else (
        echo [fail] Could not modify hosts. Check rights / antivirus.
    )
)

echo.
echo === Step 2/3: import self-signed certificate ===

if not exist "%CERT_FILE%" (
    echo [!] Certificate file not found next to the bat: kb.tradesoft.corp.crt
    echo     Put it in the same folder and run again.
) else (
    certutil -user -addstore -f root "%CERT_FILE%" >nul 2>&1
    if %errorlevel% equ 0 (
        echo [ok] Certificate imported to trusted root store.
    ) else (
        echo [fail] Could not import certificate. Check rights.
    )
)

echo.
echo === Step 3/3: open the site ===

start "" "%SITE_URL%"

echo.
echo Done. If the site does not open, fully close the browser and reopen it
echo (certificate cache is refreshed only after browser restart).
echo.
pause
endlocal
