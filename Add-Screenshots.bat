@echo off
title grantdenney.com - add screenshots
cd /d "%~dp0"
echo.
echo  Reading screenshots from assets\img\work\incoming
echo.
python tools/add-screenshots.py
echo.
pause
