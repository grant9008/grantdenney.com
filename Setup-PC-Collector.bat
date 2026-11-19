@echo off
title Goblin Ledger - collect prices on this PC
cd /d "%~dp0"
echo.
echo  ============================================================
echo   Goblin Ledger: let this PC take the hourly price snapshot
echo  ============================================================
echo.
echo  GitHub's free scheduler often runs hours late, so this PC
echo  collects the WoW Forever auction house every hour instead
echo  (a hidden Windows task called "ForeverAH Collector", only
echo  while you are logged in). It publishes the prices to
echo  grantdenney.com/goblin-ledger/prices and updates the addon right away.
echo  Uninstall-WoW-Addon.bat removes it again.
echo.
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\wow\addon\install_collector.ps1"
echo.
pause
