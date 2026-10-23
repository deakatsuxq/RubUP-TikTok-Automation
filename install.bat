@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
echo Установка RubUP Automation 4.0 и Playwright Chromium. Дождитесь завершения.
echo История, настройки, ключи и картинки сохранятся.
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_windows.ps1"
if errorlevel 1 (
  echo Установка не завершена. Проверьте сообщения выше.
  echo Инструкция есть в README.md.
  pause
  exit /b 1
)
echo Всё готово. Теперь запускайте start.bat двойным нажатием.
pause
exit /b 0
