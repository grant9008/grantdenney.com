@echo off
setlocal EnableExtensions DisableDelayedExpansion
title WoW Forever prices - Battle.net key setup
cd /d "%~dp0"
set "KEYDIR=%USERPROFILE%\.wow-ah"
echo.
echo  ============================================================
echo   WoW Forever prices: Battle.net API key  (step 1 of 2)
echo  ============================================================
echo.
echo  Your browser is opening Blizzard's developer page.
echo.
echo  1. Log in with your normal Battle.net account.
echo  2. Click "Create Client".
echo       Client name:   grantdenney-wow   (any name works)
echo       Redirect URLs: leave empty. If it will not let you,
echo                      type  http://localhost
echo       Intended use:  personal auction house price tracker
echo     Then click Save.
echo  3. The page shows a Client ID and a Client Secret.
echo     Copy each one and paste it below. In this window you paste
echo     with a right-click (or Ctrl+V), then press Enter.
echo.
start "" "https://develop.battle.net/access/clients"
set "BNET_ID="
set "BNET_SECRET="
set /p "BNET_ID=  Paste the Client ID here and press Enter: "
set /p "BNET_SECRET=  Paste the Client Secret here and press Enter: "
if not defined BNET_ID goto missing
if not defined BNET_SECRET goto missing

if not exist "%KEYDIR%" mkdir "%KEYDIR%"
> "%KEYDIR%\bnet.txt" echo %BNET_ID%
>> "%KEYDIR%\bnet.txt" echo %BNET_SECRET%
echo.
echo  Saved on this PC only (%KEYDIR%\bnet.txt).
echo  Checking it with Blizzard...
echo.
python tools\wow\check_key.py > "%KEYDIR%\check.txt" 2>&1
set "CHECK=%errorlevel%"
type "%KEYDIR%\check.txt"
echo.
if not "%CHECK%"=="0" goto bad_key

echo  ============================================================
echo   Step 2 of 2: let the website update itself every hour
echo  ============================================================
echo.
echo  Your browser opens your website's GitHub settings next.
echo  Click "New repository secret" and add two secrets:
echo.
echo     Name: BNET_CLIENT_ID       Secret: your Client ID
echo     Name: BNET_CLIENT_SECRET   Secret: your Client Secret
echo.
echo  This window can copy each one for you, so you only paste.
echo.
pause
start "" "https://github.com/grant9008/grantdenney.com/settings/secrets/actions/new"
<nul set /p "=%BNET_ID%" | clip
echo.
echo  The Client ID is copied. On the GitHub page:
echo    Name:   BNET_CLIENT_ID
echo    Secret: paste (Ctrl+V)
echo    then click "Add secret".
echo.
echo  Done? Press a key here to copy the Client Secret.
pause >nul
<nul set /p "=%BNET_SECRET%" | clip
echo.
echo  The Client Secret is copied. Click "New repository secret" again:
echo    Name:   BNET_CLIENT_SECRET
echo    Secret: paste (Ctrl+V)
echo    then click "Add secret".
echo.
echo  Done? Press a key here to finish.
pause >nul
echo off | clip
echo.
echo  All set. The clipboard is cleared again. The website starts
echo  collecting prices at its next hourly run (about 11 minutes past
echo  each hour). Tell Claude you are done and it checks everything.
echo.
pause
exit /b 0

:bad_key
echo  Blizzard did not accept that key, so nothing else was changed.
echo  Check you copied the whole Client ID and Client Secret (no
echo  spaces), then run Setup-WoW-Key.bat again.
echo.
pause
exit /b 1

:missing
echo.
echo  Both the Client ID and the Client Secret are needed. Run this again.
pause
exit /b 1
