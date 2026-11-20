@echo off
title Goblin Ledger - backtest
cd /d "%~dp0"
echo.
echo  ============================================================
echo   Goblin Ledger backtest: was the addon's advice right?
echo  ============================================================
echo.
echo  Replays the addon's advice on the real price history the
echo  collector keeps on this PC, and prints PASS or FAIL for each
echo  rule. On new prices it takes 10 to 45 minutes, later runs less.
echo  It only reads the prices. It changes nothing in the game.
echo.
set "PY="
if exist "%LOCALAPPDATA%\ForeverAH\collector\python.txt" set /p PY=<"%LOCALAPPDATA%\ForeverAH\collector\python.txt"
if not defined PY set "PY=python"
"%PY%" -B "%~dp0run_backtest.py" %*
echo.
echo  The same table is in tools\wow\backtest\cache\last_run.txt
echo.
pause
