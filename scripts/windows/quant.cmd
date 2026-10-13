@echo off
rem Forwards to quant.ps1 (which forwards into WSL). Put this folder on PATH; setup.ps1 does that.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0quant.ps1" %*
exit /b %ERRORLEVEL%
