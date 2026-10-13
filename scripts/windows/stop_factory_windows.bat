@echo off
chcp 65001 >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop_factory_windows.ps1" %*
set "FACTORY_EXIT=%ERRORLEVEL%"
if not "%FACTORY_EXIT%"=="0" (
  echo Safe stop refused. Existing services preserved. Review logs\launcher.
  pause
)
exit /b %FACTORY_EXIT%
