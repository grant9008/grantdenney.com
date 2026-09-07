@echo off
title grantdenney.com - local preview
cd /d "%~dp0"
echo.
echo  Starting a local preview of grantdenney.com
echo  Your browser will open at http://localhost:4321
echo.
echo  Leave this window open while you look at the site.
echo  Close it (or press Ctrl+C) when you're done.
echo.
start "" http://localhost:4321
npx --yes serve -l 4321 .
pause
