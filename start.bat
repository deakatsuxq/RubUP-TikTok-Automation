@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Сначала выполните установку из README.md.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" main.py
if errorlevel 1 pause
