@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
echo Установка Chromium для Playwright. Дождитесь завершения.
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_chromium.ps1"
if errorlevel 1 (
  echo Не удалось установить Chromium. Проверьте сообщение выше.
  pause
  exit /b 1
)
pause
exit /b 0
