$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
if ($env:OS -ne "Windows_NT") { throw "Этот сценарий запускается в Windows 10/11." }
if (-not (Get-Command py.exe -ErrorAction SilentlyContinue)) {
    throw "Установите Python 3.12 (64-bit) с python.org вместе с Python Launcher, затем снова запустите install.bat."
}

& py -3.12 -c "import sys; assert sys.version_info[:2] == (3, 12)"
if ($LASTEXITCODE -ne 0) { throw "Python 3.12 не найден. Установите его с python.org." }
$environment = Join-Path $PSScriptRoot ".venv"
$python = Join-Path $environment "Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    & py -3.12 -m venv $environment
    if ($LASTEXITCODE -ne 0) { throw "Не удалось создать локальное окружение Python." }
}
& $python -m pip install --disable-pip-version-check -r (Join-Path $PSScriptRoot "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "Не удалось установить зависимости. Проверьте Интернет и повторите." }
& $python -m pip check
if ($LASTEXITCODE -ne 0) { throw "Установленные зависимости несовместимы." }

# Existing config.yaml, .env, database and user images are deliberately preserved.
Write-Host "Установка завершена. Откройте start.bat двойным нажатием."
