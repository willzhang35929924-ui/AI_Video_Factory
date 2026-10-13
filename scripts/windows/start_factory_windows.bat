@echo off
chcp 65001 >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_factory_windows.ps1" %*
set "FACTORY_EXIT=%ERRORLEVEL%"
if not "%FACTORY_EXIT%"=="0" (
  echo Startup failed. See logs\launcher and the Windows startup guide.
  pause
)
exit /b %FACTORY_EXIT%
