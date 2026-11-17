@echo off
setlocal EnableExtensions DisableDelayedExpansion
title Forever AH - install the WoW Forever addon
cd /d "%~dp0"
echo.
echo  ============================================================
echo   Forever AH: auction house prices, price charts and price
echo   advice inside WoW Forever, from grantdenney.com/wow
echo  ============================================================
echo.

set "SRC=%~dp0wow-addon\ForeverAH"
set "UPD=%~dp0tools\wow\addon\update_prices.ps1"
set "APPDIR=%LOCALAPPDATA%\ForeverAH"
set "PSEXE=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if not exist "%PSEXE%" set "PSEXE=powershell.exe"
if not exist "%SRC%\ForeverAH.toc" goto no_src
if not exist "%UPD%" goto no_src

rem ---- 1. find the game ----------------------------------------------------
call "%~dp0tools\wow\addon\find_wow.bat"
if not defined FAH_WOW goto no_wow
set "ADDONS=%FAH_WOW%\Interface\AddOns"
set "DEST=%ADDONS%\ForeverAH"
echo  Found WoW Forever in:
echo    %FAH_WOW%
echo.

rem ---- 2. copy the addon (an existing Data.lua with real prices is kept) ----
echo  Copying the addon into Interface\AddOns\ForeverAH ...
if not exist "%ADDONS%\" mkdir "%ADDONS%" 2>nul
if not exist "%ADDONS%\" goto no_access
robocopy "%SRC%" "%DEST%" /MIR /XF Data.lua /R:2 /W:2 /NFL /NDL /NJH /NJS /NP >nul
if errorlevel 8 goto no_access
if not exist "%DEST%\Data.lua" if exist "%SRC%\Data.lua" copy /y "%SRC%\Data.lua" "%DEST%\Data.lua" >nul
if exist "%DEST%\Data.lua" goto copied
> "%DEST%\Data.lua" echo -- ForeverAH price data placeholder. Run Update-WoW-Prices.bat.
>> "%DEST%\Data.lua" echo ForeverAH_Data = ForeverAH_Data or { v = 1, updated = 0, items = {} }
if not exist "%DEST%\Data.lua" goto no_access
:copied
echo  Done.
echo.

rem ---- 2b. run as administrator: let the price updates save there too --------
rem Only when this was started with "Run as administrator" (fltmc needs it).
rem The hourly updates run as you, not as administrator, so the ForeverAH
rem folder (only that folder) gets "modify" for normal users.
fltmc >nul 2>&1
if errorlevel 1 goto perms_done
icacls "%DEST%" /grant *S-1-5-32-545:(OI)(CI)M /T /Q >nul 2>&1
if errorlevel 1 goto perms_done
echo  Price updates can now save into the ForeverAH folder without
echo  administrator rights.
echo.
:perms_done

rem ---- 3. the price updater lives in %LOCALAPPDATA%\ForeverAH ---------------
if not exist "%APPDIR%\" mkdir "%APPDIR%"
copy /y "%UPD%" "%APPDIR%\update_prices.ps1" >nul
if errorlevel 1 goto no_appdir
> "%APPDIR%\addon_dir.txt" echo %DEST%

rem ---- 4. get the prices now -------------------------------------------------
echo  Getting the latest prices...
set "URLARG="
if defined FOREVERAH_DATA_URL set URLARG=-DataUrl "%FOREVERAH_DATA_URL%"
"%PSEXE%" -NoProfile -ExecutionPolicy Bypass -File "%APPDIR%\update_prices.ps1" -AddonDir "%DEST%" %URLARG%
echo.

rem ---- 5. hourly updates (optional) -------------------------------------------
if "%FOREVERAH_NO_TASK%"=="1" goto finish
echo  ------------------------------------------------------------
echo  Prices on the site change every hour. This PC can fetch them
echo  by itself every 30 minutes in the background, without any
echo  window popping up (a Windows scheduled task called
echo  "ForeverAH Prices"). It only downloads when the prices changed.
echo  Uninstall-WoW-Addon.bat removes it again.
echo  ------------------------------------------------------------
choice /c YN /n /m "  Update prices automatically every 30 minutes? (Y/N) "
if errorlevel 2 goto no_task
rem Runs at :15 and :45. The site's hourly job starts at :11 but GitHub often
rem starts it late, so checking twice an hour picks up each new file soon.
set "NEXTHM="
for /f %%H in ('powershell.exe -NoProfile -Command "$t=(Get-Date).AddMinutes(2); $b=$t.Date.AddHours($t.Hour); if ($t.Minute -lt 15) {$b=$b.AddMinutes(15)} elseif ($t.Minute -lt 45) {$b=$b.AddMinutes(45)} else {$b=$b.AddMinutes(75)}; $b.ToString('HH:mm')"') do set "NEXTHM=%%H"
if not defined NEXTHM set "NEXTHM=00:15"
rem conhost --headless starts PowerShell with no console window at all, so
rem nothing flashes up or takes the keyboard while you play.
set "CONHOST=%SystemRoot%\System32\conhost.exe"
if exist "%CONHOST%" set TASKTR=%CONHOST% --headless %PSEXE% -NoProfile -ExecutionPolicy Bypass -File \"%APPDIR%\update_prices.ps1\" -Quiet
if not exist "%CONHOST%" set TASKTR=%PSEXE% -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File \"%APPDIR%\update_prices.ps1\" -Quiet
rem Windows allows at most 261 characters here: with a long user folder name,
rem name powershell.exe without its folder (Windows finds it by itself).
set "TRTEST=%TASKTR:~261,1%"
if defined TRTEST if exist "%CONHOST%" set TASKTR=%CONHOST% --headless powershell.exe -NoProfile -ExecutionPolicy Bypass -File \"%APPDIR%\update_prices.ps1\" -Quiet
if "%FOREVERAH_TASK_DRYRUN%"=="1" goto task_dryrun
schtasks /Create /F /SC MINUTE /MO 30 /ST %NEXTHM% /TN "ForeverAH Prices" /TR "%TASKTR%" >nul
if errorlevel 1 goto task_failed
echo.
echo  Done: prices will update by themselves every 30 minutes
echo  while you are logged in to Windows.
goto finish

:task_dryrun
echo  [test only, not run] schtasks /Create /F /SC MINUTE /MO 30 /ST %NEXTHM% /TN "ForeverAH Prices" /TR "%TASKTR%"
goto finish

:task_failed
echo.
echo  Windows did not create the hourly update. That is fine: double-click
echo  Update-WoW-Prices.bat whenever you want fresh prices.
goto finish

:no_task
echo.
echo  OK, no automatic updates. Double-click Update-WoW-Prices.bat
echo  whenever you want fresh prices.

:finish
echo.
echo  ============================================================
echo   All set. Next:
echo    1. Start WoW Forever and log in. Already in the game?
echo       Type /reload and press Enter.
echo    2. Type /fah to check the prices loaded.
echo    3. At the auction house, search for an item: its price chart
echo       and a suggested price show up.
echo   Not showing? At the character screen click AddOns and make
echo   sure Forever AH is ticked.
echo  ============================================================
echo.
pause
exit /b 0

:no_src
echo  The addon files were not found next to this file:
echo    %SRC%
echo  Run Install-WoW-Addon.bat from inside the grantdenney.com folder.
goto fail

:no_wow
echo  Could not find WoW Forever on this PC. Looked at Blizzard's install
echo  record and in C:\Program Files (x86)\World of Warcraft\_forever_
echo  Install or run WoW Forever once from the Battle.net app, then try again.
goto fail

:no_access
echo.
echo  Windows did not allow copying into the game folder:
echo    %ADDONS%
echo  Right-click Install-WoW-Addon.bat and choose "Run as administrator".
echo  That also lets the price updates save into the ForeverAH folder
echo  afterwards, without administrator rights.
goto fail

:no_appdir
echo  Could not save the price updater in %APPDIR%
goto fail

:fail
echo.
pause
exit /b 1
