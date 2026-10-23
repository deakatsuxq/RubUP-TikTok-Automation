$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
if ($env:OS -ne "Windows_NT") { throw "Этот сценарий запускается в Windows 10/11." }
$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (Test-Path -LiteralPath $python) {
    & $python -m playwright install chromium
} else {
    # PyInstaller --collect-all playwright includes its own Node driver.
    # This path works without Python on a computer receiving the EXE folder.
    $driver = Join-Path $PSScriptRoot "_internal\playwright\driver"
    $node = Join-Path $driver "node.exe"
    $cli = Join-Path $driver "package\cli.js"
    if (-not (Test-Path -LiteralPath $node) -or -not (Test-Path -LiteralPath $cli)) {
        throw "Нет установленного Playwright. Для исходников сначала запустите install.bat; для EXE скопируйте всю папку приложения."
    }
    & $node $cli install chromium
}
if ($LASTEXITCODE -ne 0) { throw "Не удалось установить Chromium. Проверьте Интернет и повторите установку." }
Write-Host "Chromium установлен для текущего пользователя Windows. Можно запускать RubUP Automation."
