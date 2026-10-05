@echo off
title Publish the NEW E-Bike Speedrunner to grantdenney.com/ebike2 (the original /ebike/ is not touched)
cd /d "%~dp0"
echo.
echo  Building the latest E-Bike Speedrunner and publishing it to
echo    https://grantdenney.com/ebike2/
echo  (the original at /ebike/ is left exactly as it is)
echo.

pushd "C:\Users\gdenn\ebike-speedrunner"
call npx vite build --base=/ebike2/
if errorlevel 1 goto :failed
popd

echo.
echo  Copying the build in...
rmdir /s /q "ebike2\assets" 2>nul
xcopy /e /i /y /q "C:\Users\gdenn\ebike-speedrunner\dist\*" "ebike2\" >nul
if errorlevel 1 goto :failed

python tools\noindex-game.py ebike2
if errorlevel 1 goto :failed

echo.
echo  Publishing...
git pull --ff-only origin main
git add ebike2
git commit -m "Update the E-Bike Speedrunner build at /ebike2/"
git push origin main
if errorlevel 1 goto :failed

echo.
echo  Done. Live in about a minute at https://grantdenney.com/ebike2/
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
