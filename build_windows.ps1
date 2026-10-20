$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
if (-not (Test-Path ".venv\Scripts\python.exe")) { throw "Создайте .venv согласно README.md" }
& .\.venv\Scripts\python.exe -m pip install pyinstaller==6.16.0
if ($LASTEXITCODE -ne 0) { throw "Не удалось установить PyInstaller" }
& .\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --windowed --onedir --name RubUP main.py
if ($LASTEXITCODE -ne 0) { throw "Сборка не выполнена" }
Copy-Item config.yaml dist\RubUP\config.yaml
Copy-Item .env.example dist\RubUP\.env.example
Copy-Item README.md dist\RubUP\README.md
Write-Host "Сборка: dist\RubUP. Добавьте .env и изображения при первом запуске."
