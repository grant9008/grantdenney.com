@echo off
setlocal EnableExtensions DisableDelayedExpansion
title Forever AH - update prices
cd /d "%~dp0"
echo.
echo  Forever AH: getting the latest WoW Forever prices...
echo.
set "APPDIR=%LOCALAPPDATA%\ForeverAH"
set "PSEXE=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if not exist "%PSEXE%" set "PSEXE=powershell.exe"
if not exist "%APPDIR%\addon_dir.txt" goto not_installed
rem use the copy next to this file (newest); the installed copy is the fallback
set "UPD=%~dp0tools\wow\addon\update_prices.ps1"
if not exist "%UPD%" set "UPD=%APPDIR%\update_prices.ps1"
if not exist "%UPD%" goto not_installed

set "URLARG="
if defined FOREVERAH_DATA_URL set URLARG=-DataUrl "%FOREVERAH_DATA_URL%"
"%PSEXE%" -NoProfile -ExecutionPolicy Bypass -File "%UPD%" %URLARG%
if errorlevel 1 goto end
echo.
echo  In the game, type /reload and press Enter (or just log in)
echo  to load the new prices.
goto end

:not_installed
echo  Forever AH is not installed yet. Double-click Install-WoW-Addon.bat first.

:end
echo.
pause
exit /b 0
