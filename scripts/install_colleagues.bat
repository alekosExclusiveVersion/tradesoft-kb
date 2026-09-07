@echo off
setlocal EnableDelayedExpansion

rem ============================================================
rem  Unified installer: access to Tradesoft services for colleagues
rem  Run as Administrator (right-click -> Run as administrator)
rem
rem  Creates/updates hosts entries + imports self-signed certs for:
rem    - kb.tradesoft.corp        (Knowledge Base)
rem    - b24.search               (Bitrix24 CRM search)
rem    - ts-b24-knowledge.search  (Solutions KB)
rem
rem  All services are reverse-proxied through nginx on the Mac
rem  at IP 10.182.174.97.
rem ============================================================

set "HOSTS_FILE=C:\Windows\System32\drivers\etc\hosts"
set "MAC_IP=10.182.174.97"
set "CERT_DIR=%~dp0certs"

rem ---- hosts entries (hostname -> IP) ----
set "H1=%MAC_IP% kb.tradesoft.corp"
set "H2=%MAC_IP% b24.search"
set "H3=%MAC_IP% ts-b24-knowledge.search"

rem ---- certificate files (expected in certs\ subfolder) ----
set "C1=%CERT_DIR%\kb.tradesoft.corp.crt"
set "C2=%CERT_DIR%\b24.search.crt"
set "C3=%CERT_DIR%\ts-b24-knowledge.search.crt"

rem ---- site to open after install ----
set "SITE_URL=https://kb.tradesoft.corp/"

rem ===================== admin check =====================
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [!] Administrator rights are required.
    echo     Right-click this file -^> "Run as administrator".
    pause
    exit /b 1
)

echo.
echo  ========================================
echo   Tradesoft: setup access for colleagues
echo  ========================================
echo.

rem ===================== step 1: hosts =====================
echo === Step 1/2: create/update hosts entries ===
set "ADDED=0"

call :set_hosts_line "kb.tradesoft.corp" "%H1%"
call :set_hosts_line "b24.search" "%H2%"
call :set_hosts_line "ts-b24-knowledge.search" "%H3%"

if %ADDED% equ 0 (
    echo [info] All hosts entries already correct.
)

rem ===================== step 2: certs =====================
echo.
echo === Step 2/2: import certificates ===

if not exist "%CERT_DIR%" (
    echo [!] Certificate folder not found: %CERT_DIR%
    echo     Make sure the "certs" folder is next to this .bat file.
    pause
    exit /b 1
)

set "IMPORTED=0"

rem --- kb.tradesoft.corp ---
if not exist "%C1%" (
    echo [!] Certificate not found: kb.tradesoft.corp.crt
) else (
    certutil -user -addstore -f root "%C1%" >nul 2>&1
    if %errorlevel% equ 0 (
        echo [ok] Imported: kb.tradesoft.corp.crt
        set /a IMPORTED+=1
    ) else (
        echo [fail] Could not import kb.tradesoft.corp.crt
    )
)

rem --- b24.search ---
if not exist "%C2%" (
    echo [!] Certificate not found: b24.search.crt
) else (
    certutil -user -addstore -f root "%C2%" >nul 2>&1
    if %errorlevel% equ 0 (
        echo [ok] Imported: b24.search.crt
        set /a IMPORTED+=1
    ) else (
        echo [fail] Could not import b24.search.crt
    )
)

rem --- ts-b24-knowledge.search ---
if not exist "%C3%" (
    echo [!] Certificate not found: ts-b24-knowledge.search.crt
) else (
    certutil -user -addstore -f root "%C3%" >nul 2>&1
    if %errorlevel% equ 0 (
        echo [ok] Imported: ts-b24-knowledge.search.crt
        set /a IMPORTED+=1
    ) else (
        echo [fail] Could not import ts-b24-knowledge.search.crt
    )
)

if %IMPORTED% equ 0 (
    echo [info] No certificates were imported (files missing or errors).
)

rem ===================== done =====================
echo.
echo === Opening Knowledge Base ===
start "" "%SITE_URL%"

echo.
echo ========================================
echo  Done.
echo.
echo  Services available:
echo    https://kb.tradesoft.corp         Knowledge Base
echo    https://b24.search                Bitrix24 CRM search
echo    https://ts-b24-knowledge.search   Solutions KB
echo.
echo  If a site does not open, fully close the browser and reopen it
echo  (certificate cache is refreshed only after browser restart).
echo ========================================
echo.
pause
goto :eof

rem ============================================================
rem  Subroutine: replace-or-add a hostname line in hosts file
rem  %1 = hostname to find/remove, %2 = full "IP hostname" line
rem ============================================================
:set_hosts_line
set "SRV_NAME=%~1"
set "FULL_LINE=%~2"
set "TMPH=%HOSTS_FILE%.tmp"
rem rebuild hosts without any line that mentions the hostname, then append ours
findstr /v /i /c:"%SRV_NAME%" "%HOSTS_FILE%" > "%TMPH%" 2>nul
if %errorlevel% neq 0 (
    del "%TMPH%" >nul 2>&1
    echo [fail] Could not read %HOSTS_FILE%
    goto :eof
)
echo %FULL_LINE%>>"%TMPH%"
copy /y "%TMPH%" "%HOSTS_FILE%" >nul 2>&1
if %errorlevel% equ 0 (
    echo [ok] Set: %FULL_LINE%
    set /a ADDED+=1
) else (
    del "%TMPH%" >nul 2>&1
    echo [fail] Could not update hosts for %SRV_NAME%
)
del "%TMPH%" >nul 2>&1
goto :eof
