@echo off
setlocal EnableExtensions DisableDelayedExpansion
title Goblin Ledger - uninstall
rem Removes the Goblin Ledger addon, its price updater and its scheduled task.
rem The work is done by uninstall.ps1 (plain text, open it in Notepad to read it).
set "PSEXE=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if not exist "%PSEXE%" set "PSEXE=powershell.exe"
set "PS1=%~dp0scripts\uninstall.ps1"
if not exist "%PS1%" set "PS1=%~dp0uninstall.ps1"
if not exist "%PS1%" goto missing
"%PSEXE%" -NoProfile -ExecutionPolicy Bypass -File "%PS1%"
echo.
pause
exit /b 0

:missing
echo.
echo  The uninstaller files were not found next to this file.
echo  Unzip GoblinLedger.zip first (right-click it, Extract All), then
echo  double-click Uninstall-Goblin-Ledger.bat in the unzipped folder.
echo.
pause
exit /b 1
