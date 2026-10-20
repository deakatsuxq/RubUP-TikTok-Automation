@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
if not exist "%~dp0.venv\Scripts\pythonw.exe" (
  echo Сначала запустите install.bat двойным нажатием.
  echo Нужен Python 3.12 для Windows с сайта python.org.
  pause
  exit /b 1
)
start "" /D "%~dp0" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0main.py"
if errorlevel 1 (
  echo Не удалось открыть программу. Проверьте установку.
  pause
  exit /b 1
)
exit /b 0
