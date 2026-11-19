@echo off
setlocal EnableExtensions DisableDelayedExpansion
title Goblin Ledger - update
rem Checks for a new Goblin Ledger version and installs it right now, with the
rem same checks as the automatic updates (only official releases, fingerprint
rem checked first). Your prices and settings are kept. The work is done by
rem update_prices.ps1 (plain text, open it in Notepad to read every step).
rem Install-Goblin-Ledger.bat puts a copy of this file in %LOCALAPPDATA%\GoblinLedger.
set "PSEXE=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if not exist "%PSEXE%" set "PSEXE=powershell.exe"
set "PS1=%~dp0update_prices.ps1"
if not exist "%PS1%" set "PS1=%~dp0scripts\update_prices.ps1"
if not exist "%PS1%" goto missing
"%PSEXE%" -NoProfile -ExecutionPolicy Bypass -File "%PS1%" -AddonCheck now
set "CODE=%ERRORLEVEL%"
echo.
pause
exit /b %CODE%

:missing
echo.
echo  The updater files were not found next to this file.
echo  Download GoblinLedger.zip again, unzip it and run Install-Goblin-Ledger.bat.
echo.
pause
exit /b 1
