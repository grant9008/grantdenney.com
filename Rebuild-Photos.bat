@echo off
title grantdenney.com - rebuild photos
cd /d "%~dp0"
echo.
echo  Rebuilding the Showrun photos listed in tools\showrun-picks.txt
echo.
python tools/build-images.py
echo.
echo  If you changed which photos are in the slider, tell Claude to
echo  regenerate the slide markup as well.
echo.
pause
