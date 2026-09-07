# ==========================================================
# deploy-windows/register-services.ps1  (ASCII only)
# Регистрация автозапуска eval_server и nginx как задач Task Scheduler.
# Запуск от имени администратора:
#   powershell -ExecutionPolicy Bypass -File register-services.ps1
# ==========================================================
$ErrorActionPreference = "Stop"

$KB    = "D:\tradesoft-kb"
$SCR   = "$KB\scripts"
$PY    = "$SCR\.venv\Scripts\python.exe"
$NGINX = "C:\nginx\nginx.exe"

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent())`
           .IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) {
    Write-Host "ERROR: запускай от имени администратора."
    exit 1
}

Write-Host "Регистрирую задачи автозапуска..."

# --- eval_server ---
Write-Host "  kb-eval-server..."
schtasks /Create /F /TN "kb-eval-server" `
    /TR "\"$PY\" \"$SCR\eval_server.py\"" `
    /SC ONSTART /RU SYSTEM /RL HIGHEST | Out-Null
if ($LASTEXITCODE -eq 0) {
    Write-Host "    OK. Рабочая папка задачи по умолчанию может отличаться;"
    Write-Host "    задай 'Starting directory' = $SCR в свойствах задачи,"
    Write-Host "    и при необходимости переменные окружения (TYPESENSE_KEY)."
} else {
    Write-Host "    ОШИБКА. Задай задачу вручную (см. README)."
}

# --- nginx ---
Write-Host "  kb-nginx..."
schtasks /Create /F /TN "kb-nginx" `
    /TR "\"$NGINX\"" `
    /SC ONSTART /RU SYSTEM /RL HIGHEST | Out-Null
if ($LASTEXITCODE -eq 0) {
    Write-Host "    OK."
} else {
    Write-Host "    ОШИБКА. Задай задачу вручную (см. README)."
}

Write-Host ""
Write-Host "Готово. Docker Desktop настрой сам: Settings -> General ->"
Write-Host "'Start Docker Desktop when you sign in'."
Write-Host ""
Write-Host "Ручная проверка запуска eval_server:"
Write-Host "  cd $SCR"
Write-Host "  .venv\Scripts\python.exe eval_server.py"
