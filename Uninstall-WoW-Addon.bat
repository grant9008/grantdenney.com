@echo off
setlocal EnableExtensions DisableDelayedExpansion
title Goblin Ledger - remove everything (owner)
cd /d "%~dp0"
echo.
echo  ============================================================
echo   Goblin Ledger: remove the addon AND this PC's price collector
echo  ============================================================
echo.
rem Owner tool. Friends use Uninstall-Goblin-Ledger.bat from the zip, which
rem never touches the collector. This one removes everything, including what
rem was installed before the rename (Forever AH).
set "APPDIR=%LOCALAPPDATA%\GoblinLedger"
set "OLDAPPDIR=%LOCALAPPDATA%\ForeverAH"

rem ---- where the game is ------------------------------------------------------
set "DEST="
if defined FOREVERAH_WOW_DIR goto detect
if exist "%APPDIR%\addon_dir.txt" set /p DEST=<"%APPDIR%\addon_dir.txt"
if not defined DEST if exist "%OLDAPPDIR%\addon_dir.txt" set /p DEST=<"%OLDAPPDIR%\addon_dir.txt"
if defined DEST goto check_dest
:detect
call "%~dp0tools\wow\addon\find_wow.bat"
if defined FAH_WOW set "DEST=%FAH_WOW%\Interface\AddOns\GoblinLedger"
:check_dest
set "WOWDIR="
if not defined DEST goto ask
rem safety: only work from a folder that is really called GoblinLedger or ForeverAH
set "OK="
for %%I in ("%DEST%") do if /i "%%~nxI"=="GoblinLedger" set "OK=1"
for %%I in ("%DEST%") do if /i "%%~nxI"=="ForeverAH" set "OK=1"
if not defined OK set "DEST="
if not defined DEST goto ask
for %%I in ("%DEST%\..\..\..") do set "WOWDIR=%%~fI"
set "NEWDEST=%WOWDIR%\Interface\AddOns\GoblinLedger"
set "OLDDEST=%WOWDIR%\Interface\AddOns\ForeverAH"

:ask
echo  This removes:
if defined WOWDIR echo    - the addon folders GoblinLedger and ForeverAH (the old name) in
if defined WOWDIR echo      %WOWDIR%\Interface\AddOns
if not defined WOWDIR echo    - (the game folder was not found, nothing to remove there)
echo    - the price update tasks "Goblin Ledger Prices" and "ForeverAH Prices"
echo    - this PC's price collector (scheduled task "ForeverAH Collector")
echo    - the updater, the collector and their logs in
echo      %APPDIR% and %OLDAPPDIR%
echo    - the saved settings (GoblinLedger.lua, ForeverAH.lua) in the game's WTF folder
echo  Nothing else is touched.
echo.
choice /c YN /n /m "  Remove Goblin Ledger and the collector? (Y/N) "
if errorlevel 2 goto cancelled

if "%GOBLINLEDGER_NO_TASK%"=="1" goto task_done
for %%T in ("Goblin Ledger Prices" "ForeverAH Prices" "ForeverAH Collector") do (
  schtasks /Query /TN %%T >nul 2>&1
  if not errorlevel 1 schtasks /Delete /TN %%T /F >nul 2>&1
)
:task_done

if not defined WOWDIR goto addon_done
if exist "%NEWDEST%\" rmdir /s /q "%NEWDEST%"
if exist "%NEWDEST%\" echo  Could not delete %NEWDEST%. Close WoW and try again, or run this as administrator.
rem The old folder only when it is this addon (another addon is also called Forever AH).
if exist "%OLDDEST%\ForeverAH.toc" findstr /m /c:"grantdenney.com" "%OLDDEST%\ForeverAH.toc" >nul 2>&1 && rmdir /s /q "%OLDDEST%"
if not exist "%WOWDIR%\WTF\Account\" goto addon_done
for /d %%A in ("%WOWDIR%\WTF\Account\*") do (
  if exist "%%~A\SavedVariables\GoblinLedger.lua" del /q "%%~A\SavedVariables\GoblinLedger.lua"
  if exist "%%~A\SavedVariables\GoblinLedger.lua.bak" del /q "%%~A\SavedVariables\GoblinLedger.lua.bak"
  if exist "%%~A\SavedVariables\ForeverAH.lua" findstr /m /c:"ForeverAH_Settings" "%%~A\SavedVariables\ForeverAH.lua" >nul 2>&1 && del /q "%%~A\SavedVariables\ForeverAH.lua"
  if exist "%%~A\SavedVariables\ForeverAH.lua.bak" findstr /m /c:"ForeverAH_Settings" "%%~A\SavedVariables\ForeverAH.lua.bak" >nul 2>&1 && del /q "%%~A\SavedVariables\ForeverAH.lua.bak"
)
:addon_done

if not defined LOCALAPPDATA goto done
if exist "%APPDIR%\" rmdir /s /q "%APPDIR%"
if exist "%OLDAPPDIR%\" rmdir /s /q "%OLDAPPDIR%"

:done
echo.
echo  Goblin Ledger and the collector are removed. If WoW is open, type
echo  /reload or log out.
echo.
pause
exit /b 0

:cancelled
echo.
echo  Nothing was removed.
echo.
pause
exit /b 0
