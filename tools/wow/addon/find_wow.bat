@echo off
rem Used by Install-WoW-Addon.bat and Uninstall-WoW-Addon.bat (call it, do not run it).
rem Sets FAH_WOW to the WoW Forever game folder (the _forever_ folder that holds
rem Wow.exe and Interface), or leaves it empty when none is found.
rem Looks in this order:
rem   1. FOREVERAH_WOW_DIR, if set (used for testing; nothing else is tried then)
rem   2. the Blizzard install record in the registry
rem   3. C:\Program Files (x86)\World of Warcraft\_forever_
rem   4. C:\Program Files\World of Warcraft\_forever_
set "FAH_WOW="
if defined FOREVERAH_WOW_DIR goto fw_env

set "FW_REG="
for /f "tokens=2,*" %%A in ('reg query "HKLM\SOFTWARE\WOW6432Node\Blizzard Entertainment\World of Warcraft" /v InstallPath 2^>nul ^| findstr /i /c:"InstallPath"') do set "FW_REG=%%B"
rem InstallPath is the WoW version launched last: usually ...\_forever_\ but it
rem can be ...\_retail_\ or ...\_classic_\, so look next to it as well.
if defined FW_REG call :fw_try "%FW_REG%\..\_forever_"
if defined FAH_WOW goto :eof
if defined FW_REG call :fw_try "%FW_REG%\_forever_"
if defined FAH_WOW goto :eof
call :fw_try "C:\Program Files (x86)\World of Warcraft\_forever_"
if defined FAH_WOW goto :eof
call :fw_try "C:\Program Files\World of Warcraft\_forever_"
goto :eof

:fw_env
call :fw_try "%FOREVERAH_WOW_DIR%"
goto :eof

:fw_try
set "FW_P=%~f1"
if "%FW_P:~-1%"=="\" set "FW_P=%FW_P:~0,-1%"
if exist "%FW_P%\Wow.exe" set "FAH_WOW=%FW_P%"
if exist "%FW_P%\Interface\" set "FAH_WOW=%FW_P%"
goto :eof
