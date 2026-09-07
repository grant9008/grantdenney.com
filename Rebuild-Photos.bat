@echo off
title grantdenney.com - rebuild photos
cd /d "%~dp0"
echo.
echo  Rebuilding the Showrun slider from tools\showrun-picks.txt
echo.
python tools/build-images.py
if errorlevel 1 goto :failed
echo.
python tools/gen-slides.py
if errorlevel 1 goto :failed
python tools/apply-slides.py
if errorlevel 1 goto :failed
echo.
echo  Done. Refresh the site to see the new slider.
echo.
pause
exit /b 0

:failed
echo.
echo  Something went wrong above. Nothing was half-applied to the page
echo  unless the last step is the one that failed.
echo.
pause
exit /b 1
