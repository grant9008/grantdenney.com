@echo off
rem Installs (or updates) Goblin Ledger from this repo: the addon in
rem wow-addon\GoblinLedger, with the same installer friends get in
rem GoblinLedger.zip (tools\goblin-ledger\install.ps1). Running it again
rem updates the addon and fetches fresh prices.
rem It also moves an install from before the rename (Forever AH) over.
rem Testing: set GOBLINLEDGER_DATA_URL to a local price file first.
call "%~dp0tools\goblin-ledger\Install-Goblin-Ledger.bat"
