@echo off
title WoW Forever prices - Battle.net key setup
cd /d "%~dp0"
echo.
echo  ============================================================
echo   WoW Forever prices: Battle.net API key
echo  ============================================================
echo.
echo  1. In your browser go to   https://develop.battle.net/access/clients
echo     and log in with your Battle.net account.
echo  2. Click "Create Client". Name it anything (e.g. grantdenney-wow).
echo     Redirect URL: leave empty. Intended use: personal price tracker.
echo  3. Copy the Client ID and Client Secret it shows you.
echo.
set /p BNET_ID=  Paste the Client ID here and press Enter:
set /p BNET_SECRET=  Paste the Client Secret here and press Enter:
if "%BNET_ID%"=="" goto missing
if "%BNET_SECRET%"=="" goto missing

if not exist "%USERPROFILE%\.wow-ah" mkdir "%USERPROFILE%\.wow-ah"
> "%USERPROFILE%\.wow-ah\bnet.txt" echo %BNET_ID%
>> "%USERPROFILE%\.wow-ah\bnet.txt" echo %BNET_SECRET%
echo.
echo  Saved on this PC only (%USERPROFILE%\.wow-ah\bnet.txt).
echo  Checking it with Blizzard...
echo.
python tools\wow\check_key.py
echo.
echo  ------------------------------------------------------------
echo  Last step, so the site updates by itself every hour:
echo  open https://github.com/grant9008/grantdenney.com/settings/secrets/actions
echo  click "New repository secret" twice and add
echo      BNET_CLIENT_ID      = your Client ID
echo      BNET_CLIENT_SECRET  = your Client Secret
echo  ------------------------------------------------------------
echo.
pause
exit /b

:missing
echo.
echo  Both the Client ID and the Client Secret are needed. Run this again.
pause
