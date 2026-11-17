@echo off
setlocal EnableExtensions DisableDelayedExpansion
title Forever AH - uninstall
cd /d "%~dp0"
echo.
echo  ============================================================
echo   Forever AH: remove the addon from this PC
echo  ============================================================
echo.
set "APPDIR=%LOCALAPPDATA%\ForeverAH"
set "TASK=ForeverAH Prices"

rem ---- where it was installed ------------------------------------------------
set "DEST="
if defined FOREVERAH_WOW_DIR goto detect
if exist "%APPDIR%\addon_dir.txt" set /p DEST=<"%APPDIR%\addon_dir.txt"
if defined DEST goto check_dest
:detect
call "%~dp0tools\wow\addon\find_wow.bat"
if defined FAH_WOW set "DEST=%FAH_WOW%\Interface\AddOns\ForeverAH"
:check_dest
rem safety: only ever delete a folder that is really called ForeverAH
if not defined DEST goto ask
for %%I in ("%DEST%") do if /i not "%%~nxI"=="ForeverAH" set "DEST="
if not defined DEST goto ask
for %%I in ("%DEST%\..\..\..") do set "WOWDIR=%%~fI"

:ask
echo  This removes:
if defined DEST echo    - the addon folder %DEST%
if not defined DEST echo    - (the addon folder was not found, nothing to remove there)
echo    - the hourly price update (scheduled task "%TASK%"), if it was set up
echo    - the updater and its log in %APPDIR%
echo    - Forever AH's saved settings in the game's WTF folder
echo  Nothing else is touched.
echo.
choice /c YN /n /m "  Remove Forever AH? (Y/N) "
if errorlevel 2 goto cancelled

if "%FOREVERAH_NO_TASK%"=="1" goto task_done
schtasks /Query /TN "%TASK%" >nul 2>&1
if errorlevel 1 goto task_done
schtasks /Delete /TN "%TASK%" /F >nul 2>&1
if errorlevel 1 echo  Could not remove the scheduled task "%TASK%". Try running this as administrator.
:task_done

if not defined DEST goto addon_done
if exist "%DEST%\" rmdir /s /q "%DEST%"
if exist "%DEST%\" echo  Could not delete %DEST%. Close WoW and try again, or run this as administrator.
if not exist "%WOWDIR%\WTF\Account\" goto addon_done
for /d %%A in ("%WOWDIR%\WTF\Account\*") do (
  if exist "%%~A\SavedVariables\ForeverAH.lua" del /q "%%~A\SavedVariables\ForeverAH.lua"
  if exist "%%~A\SavedVariables\ForeverAH.lua.bak" del /q "%%~A\SavedVariables\ForeverAH.lua.bak"
)
:addon_done

if not defined LOCALAPPDATA goto done
if exist "%APPDIR%\" rmdir /s /q "%APPDIR%"

:done
echo.
echo  Forever AH is removed. If WoW is open, type /reload or log out.
echo.
pause
exit /b 0

:cancelled
echo.
echo  Nothing was removed.
echo.
pause
exit /b 0
