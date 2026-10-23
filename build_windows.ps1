$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
if ($env:OS -ne "Windows_NT") { throw "Этот сценарий запускается в Windows 10/11." }
$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "Сначала запустите install.bat для установки зависимостей."
}
& $python -m pip install --disable-pip-version-check pyinstaller==6.16.0
if ($LASTEXITCODE -ne 0) { throw "Не удалось установить PyInstaller." }

# Every path is passed as a separate argument; spaces and Cyrillic remain intact.
# --windowed avoids a separate console. --onedir keeps startup and troubleshooting simple.
$docs = (Join-Path $PSScriptRoot "docs") + ";docs"
# Never rebuild over an installed application that may contain user state.
$releaseName = "RubUP-Automation-3.0-" + (Get-Date -Format "yyyyMMdd-HHmmss-fff")
$releaseRoot = Join-Path (Join-Path $PSScriptRoot "dist") $releaseName
if (Test-Path -LiteralPath $releaseRoot) { throw "Папка выпуска уже существует. Повторите сборку." }
& $python -m PyInstaller --noconfirm --clean --windowed --onedir --name RubUP-Automation --distpath $releaseRoot --add-data $docs (Join-Path $PSScriptRoot "main.py")
if ($LASTEXITCODE -ne 0) { throw "Сборка Windows не выполнена." }

$output = Join-Path $releaseRoot "RubUP-Automation"
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "config.yaml") -Destination (Join-Path $output "config.yaml")
Copy-Item -LiteralPath (Join-Path $PSScriptRoot ".env.example") -Destination (Join-Path $output ".env.example")
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "README.md") -Destination (Join-Path $output "README.md")
Write-Host "Сборка создана: $output\RubUP-Automation.exe"
Write-Host "Ключи, картинки и базы не копировались. Настройте их в приложении."
