# ==========================================================
# deploy-windows/install-deps.ps1  (ASCII only)
# Automatic installer for KB: Python 3.14, Docker Desktop, nginx
# Run as Administrator:
#   powershell -ExecutionPolicy Bypass -File install-deps.ps1
#
# Docker Desktop requires a reboot. Two phases:
#   phase 1 (no reboot): Python, nginx, WSL
#   phase 2 (after reboot): finalize Docker Desktop
# After reboot run again with -Phase2:
#   powershell -ExecutionPolicy Bypass -File install-deps.ps1 -Phase2
# ==========================================================
param(
    [switch]$Phase2
)
$ErrorActionPreference = "Stop"

$LOGFILE = Join-Path $PSScriptRoot "install-deps.log"

function Log($msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $msg
    Write-Host $line
    Add-Content -Path $LOGFILE -Value $line
}

function Pause-Exit($code) {
    Write-Host ""
    Write-Host "Press Enter to close..."
    Read-Host | Out-Null
    exit $code
}

try {
    # ---------- admin check ----------
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $pr = New-Object Security.Principal.WindowsPrincipal($id)
    $isAdmin = $pr.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

    if (-not $isAdmin) {
        Log "ERROR: not running as administrator."
        Log "Right-click this file -> Run as administrator, or launch from an admin console."
        Pause-Exit 1
    }

    # ---------- helper ----------
    function Test-Cmd($name) {
        try { & $name --version 2>$null | Out-Null; return ($LASTEXITCODE -eq 0) } catch { return $false }
    }

    if (-not $Phase2) {
        Log "== PHASE 1: Python, nginx, WSL =="

        # ---------- Python 3.14 ----------
        if (Test-Cmd "python") {
            $ver = & python --version 2>&1
            Log "[ok] Python already installed: $ver"
        } else {
            Log "Installing Python 3.14..."
            $u = "https://www.python.org/ftp/python/3.14.0/python-3.14.0-amd64.exe"
            $inst = Join-Path $env:TEMP "python-installer.exe"
            Invoke-WebRequest -Uri $u -OutFile $inst
            Start-Process -Wait -FilePath $inst -ArgumentList "/quiet","InstallAllUsers=1","PrependPath=1","Include_pip=1","Include_launcher=1"
            Log "[ok] Python 3.14 installed (open a NEW console so PATH updates)."
        }

        # ---------- WSL ----------
        Log "Enabling WSL..."
        wsl --install --no-distribution 2>$null | Out-Null
        Log "WSL installed/updated."

        # ---------- nginx ----------
        $nginxDir = "C:\nginx"
        if (Test-Path (Join-Path $nginxDir "nginx.exe")) {
            Log "[ok] nginx already present in C:\nginx"
        } else {
            Log "Downloading nginx..."
            $u = "https://nginx.org/download/nginx-1.27.4.zip"
            $z = Join-Path $env:TEMP "nginx.zip"
            Invoke-WebRequest -Uri $u -OutFile $z
            $tmp = Join-Path $env:TEMP "nginx-extract"
            if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
            Expand-Archive $z -DestinationPath $tmp
            New-Item -ItemType Directory -Force -Path $nginxDir | Out-Null
            Copy-Item (Join-Path $tmp "nginx-*/*") $nginxDir -Recurse
            Log "[ok] nginx unpacked into $nginxDir"
        }

        # ---------- Docker Desktop ----------
        Log "Downloading Docker Desktop..."
        $d = Join-Path $env:TEMP "DockerDesktopInstaller.exe"
        if (-not (Test-Path $d)) {
            Invoke-WebRequest -Uri "https://desktop.docker.com/win/main/amd64/Docker%20Desktop%20Installer.exe" -OutFile $d
        }
        Log "Running Docker Desktop installer (a reboot will be required)..."
        Start-Process -Wait -FilePath $d -ArgumentList "install","--quiet","--accept-license","--backend=wsl-2"

        Log ""
        Log "== PHASE 1 DONE. REBOOT REQUIRED. =="
        Log "After reboot run:"
        Log "  powershell -ExecutionPolicy Bypass -File install-deps.ps1 -Phase2"
        Log ""
        $ans = Read-Host "Reboot now? (Enter = reboot, N = manual)"
        if ($ans -ne "N" -and $ans -ne "n") {
            Restart-Computer -Force
        }
    } else {
        # ================= PHASE 2 =================
        Log "== PHASE 2: finalize Docker =="

        $dockerExe = Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe"
        if (Test-Path $dockerExe) {
            Start-Process $dockerExe
        } else {
            Log "[warn] Docker Desktop.exe not found at $dockerExe"
        }

        Log "Waiting for Docker engine (up to 120s)..."
        $ok = $false
        for ($i = 0; $i -lt 24; $i++) {
            Start-Sleep -Seconds 5
            docker info 2>$null | Out-Null
            if ($LASTEXITCODE -eq 0) { $ok = $true; break }
        }
        if ($ok) {
            Log "[ok] Docker ready."
        } else {
            Log "[warn] Docker not ready in 120s. Start Docker Desktop manually and re-check with 'docker info'."
        }
        Log ""
        Log "Next: run setup.ps1 (venv + deps), then 'docker compose up -d' (see README)."
    }

    Log "Done."
    Pause-Exit 0
} catch {
    Log ("FATAL: " + $_.Exception.Message)
    Log ("       " + $_.ScriptStackTrace)
    Pause-Exit 1
}
