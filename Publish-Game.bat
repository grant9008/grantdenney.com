@echo off
title Publish E-Bike Speedrunner to grantdenney.com
cd /d "%~dp0"
echo.
echo  Building the latest E-Bike Speedrunner and publishing it to
echo    https://grantdenney.com/ebike/
echo.

pushd "C:\Users\gdenn\ebike-speedrunner"
call npx vite build --base=/ebike/
if errorlevel 1 goto :failed
popd

echo.
echo  Copying the build in...
rmdir /s /q "ebike\assets" 2>nul
xcopy /e /i /y /q "C:\Users\gdenn\ebike-speedrunner\dist\*" "ebike\" >nul
if errorlevel 1 goto :failed

python tools\noindex-game.py
if errorlevel 1 goto :failed

echo.
echo  Publishing...
git add ebike
git commit -m "Update the E-Bike Speedrunner build"
git push origin main
if errorlevel 1 goto :failed

echo.
echo  Done. Live in about a minute at https://grantdenney.com/ebike/
echo  If it looks old in your browser, press Ctrl+Shift+R.
echo.
pause
exit /b 0

:failed
popd 2>nul
echo.
echo  Something went wrong above. Nothing was published.
echo.
pause
exit /b 1
