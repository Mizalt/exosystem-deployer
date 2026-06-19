# install.ps1 — установка exosystem-deployer одной командой (Windows + Docker Desktop).
#   irm https://raw.githubusercontent.com/Mizalt/exosystem-deployer/main/install.ps1 | iex
#
# Идемпотентен: повторный запуск обновляет код и пересобирает стек.
$ErrorActionPreference = "Stop"

$RepoUrl    = if ($env:EXOSYSTEM_REPO) { $env:EXOSYSTEM_REPO } else { "https://github.com/Mizalt/exosystem-deployer.git" }
$InstallDir = if ($env:EXOSYSTEM_DIR) { $env:EXOSYSTEM_DIR } else { "$env:USERPROFILE\exosystem-deployer" }

function Log($m) { Write-Host "[install] $m" -ForegroundColor Cyan }
function Die($m) { Write-Host "[install][error] $m" -ForegroundColor Red; exit 1 }

# 1. Зависимости
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { Die "Docker Desktop не установлен." }
docker compose version *> $null; if ($LASTEXITCODE -ne 0) { Die "Не найден плагин 'docker compose'." }
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Die "git не установлен." }

# 2. Код
if (Test-Path "$InstallDir\.git") {
  Log "Обновляю код в $InstallDir ..."
  git -C $InstallDir pull --ff-only
} else {
  Log "Клонирую $RepoUrl -> $InstallDir ..."
  git clone --depth 1 $RepoUrl $InstallDir
}
Set-Location $InstallDir

# 3. Секрет JWT (.env)
if (-not (Test-Path ".env")) {
  $bytes = New-Object 'System.Byte[]' 32
  [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
  $key = -join ($bytes | ForEach-Object { $_.ToString("x2") })
  "DEPLOYER_SECRET_KEY=$key" | Out-File -FilePath ".env" -Encoding ascii -NoNewline
  Log "Сгенерирован .env с DEPLOYER_SECRET_KEY."
}

# 4. Каталоги состояния
foreach ($d in "data","uploads","nginx_configs","ssl_certs","acme_challenge") {
  New-Item -ItemType Directory -Force -Path $d | Out-Null
}

# 5. Подъём
Log "Собираю и запускаю стек (docker compose up -d --build) ..."
docker compose up -d --build

# 6. Одноразовый пароль администратора
Log "Ожидаю инициализацию деплоера ..."
for ($i = 0; $i -lt 30; $i++) {
  if ((docker compose logs deployer 2>$null) -match "СОЗДАН АДМИНИСТРАТОР") { break }
  Start-Sleep -Seconds 1
}
Write-Host "=================================================================="
$logs = docker compose logs deployer 2>$null
$idx = ($logs | Select-String "СОЗДАН АДМИНИСТРАТОР" | Select-Object -First 1).LineNumber
if ($idx) { $logs[($idx-1)..([Math]::Min($idx+4, $logs.Count-1))] | ForEach-Object { Write-Host $_ } }
else { Log "Администратор уже существовал (повторная установка)." }
Write-Host "=================================================================="
Log "Готово. Задайте домен панели в настройках и откройте https://<домен>."
