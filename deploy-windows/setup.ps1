# ==========================================================
# deploy-windows/setup.ps1  (ASCII only)
# KB Tradesoft deploy on Windows (Python + Docker + nginx)
# Run:  powershell -ExecutionPolicy Bypass -File setup.ps1
# ==========================================================
$ErrorActionPreference = "Stop"

$KB     = Split-Path $PSScriptRoot -Parent
$SCR    = "$KB\scripts"
$CACHE  = "$KB\cache"

Write-Host "== KB deploy on Windows =="
Write-Host "Target dir : $KB"
Write-Host ""

# ---------- 0. Admin check ----------
$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) {
    Write-Host "WARN: not admin. Python/Task Scheduler install may require privileges."
}

# ---------- 1. Python check ----------
function Get-Python {
    $cands = @(
        "python3.14", "py -3.14", "python", "py"
    )
    foreach ($cmd in $cands) {
        try {
            $v = & $cmd --version 2>&1
            if ($LASTEXITCODE -eq 0 -and "$v" -match "Python 3\.\d+") {
                Write-Host "Python: $v"
                return $cmd
            }
        } catch {}
    }
    Write-Host "ERROR: Python 3 not found. Install Python 3.14 from python.org (check 'Add to PATH')."
    exit 1
}
$py = Get-Python

# ---------- 2. venv + pymorphy3 ----------
if (-not (Test-Path "$SCR\.venv\Scripts\python.exe")) {
    Write-Host "Creating venv..."
    Push-Location $SCR
    & $py -m venv .venv
    if ($LASTEXITCODE -ne 0) { Pop-Location; exit 1 }
    Pop-Location
}
$vp = "$SCR\.venv\Scripts\python.exe"
Write-Host "Installing dependencies..."
& $vp -m pip install --upgrade pip | Out-Null
& $vp -m pip install -r "$KB\requirements.txt"
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: failed to install dependencies (pymorphy3)."
    exit 1
}
Write-Host "Dependencies installed."

# ---------- 3. Cache check ----------
if (-not (Test-Path "$CACHE\kb_index.db")) {
    Write-Host "WARN: $CACHE\kb_index.db not found."
    Write-Host "  Copy cache/ from macOS to $CACHE"
    Write-Host "  or rebuild index (see README)."
    Write-Host "  Continuing, but search may not work until data is transferred."
} else {
    $size = (Get-Item "$CACHE\kb_index.db").Length
    Write-Host "cache/kb_index.db found: $size bytes"
}

# ---------- 4. Docker check ----------
docker --version 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "WARN: Docker Desktop not found. Install Docker Desktop and start it."
} else {
    $dv = docker --version
    Write-Host "Docker: $dv"
}

Write-Host ""
Write-Host "== Next steps (manual) =="
Write-Host "1) For semantic/hybrid search:"
Write-Host "     Copy .env.example to .env (in deploy-windows)"
Write-Host "     docker compose up -d"
Write-Host "     docker exec kb-ollama ollama pull qwen3-embedding:4b"
Write-Host "     $vp build_vector_index.py --rebuild"
Write-Host "   (pure FTS search works without Docker)"
Write-Host "2) Start web server:"
Write-Host "     cd $SCR; .venv\Scripts\python.exe eval_server.py"
Write-Host ""
Write-Host "Done. For auto-start see README (Task Scheduler)."