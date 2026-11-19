@echo off
setlocal EnableExtensions DisableDelayedExpansion
title Goblin Ledger - install
rem Installs the Goblin Ledger addon for WoW Forever. The work is done by
rem install.ps1 (plain text, open it in Notepad to read every step).
set "PSEXE=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if not exist "%PSEXE%" set "PSEXE=powershell.exe"
set "PS1=%~dp0scripts\install.ps1"
if not exist "%PS1%" set "PS1=%~dp0install.ps1"
if not exist "%PS1%" goto missing
set "URLARG="
if defined GOBLINLEDGER_DATA_URL set URLARG=-DataUrl "%GOBLINLEDGER_DATA_URL%"
"%PSEXE%" -NoProfile -ExecutionPolicy Bypass -File "%PS1%" %URLARG%
echo.
pause
exit /b 0

:missing
echo.
echo  The installer files were not found next to this file.
echo  Unzip GoblinLedger.zip first (right-click it, Extract All), then
echo  double-click Install-Goblin-Ledger.bat in the unzipped folder.
echo.
pause
exit /b 1
