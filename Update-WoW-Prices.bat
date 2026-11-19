@echo off
setlocal EnableExtensions DisableDelayedExpansion
title Goblin Ledger - update prices
cd /d "%~dp0"
echo.
echo  Goblin Ledger: getting the latest WoW Forever prices...
echo.
set "PSEXE=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if not exist "%PSEXE%" set "PSEXE=powershell.exe"
rem installed by Install-Goblin-Ledger.bat (or, before the rename, Install-WoW-Addon.bat)
set "INSTALLED="
if exist "%LOCALAPPDATA%\GoblinLedger\addon_dir.txt" set "INSTALLED=1"
if exist "%LOCALAPPDATA%\ForeverAH\addon_dir.txt" set "INSTALLED=1"
if not defined INSTALLED goto not_installed
rem use the copy next to this file (newest); the installed copy is the fallback
set "UPD=%~dp0tools\wow\addon\update_prices.ps1"
if not exist "%UPD%" set "UPD=%LOCALAPPDATA%\GoblinLedger\update_prices.ps1"
if not exist "%UPD%" goto not_installed

set "URLARG="
if defined GOBLINLEDGER_DATA_URL set URLARG=-DataUrl "%GOBLINLEDGER_DATA_URL%"
"%PSEXE%" -NoProfile -ExecutionPolicy Bypass -File "%UPD%" %URLARG%
if errorlevel 1 goto end
echo.
echo  In the game, type /reload and press Enter (or just log in)
echo  to load the new prices.
goto end

:not_installed
echo  Goblin Ledger is not installed yet. Double-click Install-Goblin-Ledger.bat first.

:end
echo.
pause
exit /b 0
