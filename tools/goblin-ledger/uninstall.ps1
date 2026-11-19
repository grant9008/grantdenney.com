<#
  Goblin Ledger uninstaller. Started by Uninstall-Goblin-Ledger.bat
  (double-click that, not this). Asks first, then removes:
    - the "Goblin Ledger Prices" task (and "ForeverAH Prices", its name
      before the rename), if there is one
    - the addon folder Interface\AddOns\GoblinLedger
    - the price updater in %LOCALAPPDATA%\GoblinLedger
  Your saved settings (watchlist, kept items) stay in the game's WTF folder
  unless you say yes when it asks about them.

    -GameDir  the WoW Forever folder (_forever_), if the uninstaller cannot
              tell where the addon is
  Test mode: see common.ps1.

  Exit codes: 0 done (or nothing to do), 1 cancelled or not everything removed.
#>
param([string]$GameDir)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')

$AppDir = Join-Path $env:LOCALAPPDATA 'GoblinLedger'

Blank
Say '============================================================'
Say ' Goblin Ledger: remove the addon from this PC'
Say '============================================================'
Blank

# ---- where it is installed ------------------------------------------------------
$Dest = $null
if ($GameDir) {
    $g = Resolve-GameDir $GameDir
    if ($g) { $Dest = Join-Path $g 'Interface\AddOns\GoblinLedger' }
}
if (-not $Dest) {
    $saved = Join-Path $AppDir 'addon_dir.txt'
    if (Test-Path -LiteralPath $saved -PathType Leaf) {
        $Dest = ([string](Get-Content -LiteralPath $saved -TotalCount 1)).Trim().Trim('"').TrimEnd('\')
    }
}
if (-not $Dest) {
    $g = Find-GameDir
    if ($g) { $Dest = Join-Path $g 'Interface\AddOns\GoblinLedger' }
}
# Safety: only ever delete a folder that is really called GoblinLedger.
if ($Dest -and (Split-Path -Leaf $Dest) -ne 'GoblinLedger') { $Dest = $null }
$Game = $null
if ($Dest) { $Game = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $Dest)) }
$hasAddon = $Dest -and (Test-Path -LiteralPath $Dest -PathType Container)
$hasTask = (Test-TaskExists $TaskName) -or (Test-TaskExists $OldTaskName)
$hasApp = Test-Path -LiteralPath $AppDir -PathType Container

if (-not ($hasAddon -or $hasTask -or $hasApp)) {
    Say 'Goblin Ledger is not installed on this PC (nothing to remove).'
    if (-not $Dest) {
        Say 'If the addon is still in your game, delete the GoblinLedger folder'
        Say 'from the game''s Interface\AddOns folder by hand.'
    }
    exit 0
}

Say 'This removes:'
if ($hasAddon) { Say "  - the addon: $Dest" }
if ($hasTask) { Say '  - the automatic price update (the "Goblin Ledger Prices" task)' }
if ($hasApp) { Say "  - the price updater: $AppDir" }
Say 'Nothing else is touched.'
Blank
if (-not (Ask-YesNo 'Remove Goblin Ledger? (Y/N) ' $false)) {
    Blank
    Say 'Nothing was removed.'
    exit 1
}

$problems = 0
foreach ($t in @($TaskName, $OldTaskName)) {
    if (-not (Remove-PriceTask $t)) { Say "Could not remove the task ""$t"". Delete it in Task Scheduler."; $problems++ }
}
if ($hasAddon) {
    try { Remove-Item -LiteralPath $Dest -Recurse -Force }
    catch {
        Say "Could not delete $Dest"
        Say 'Close WoW and try again, or right-click Uninstall-Goblin-Ledger.bat and'
        Say 'choose "Run as administrator".'
        $problems++
    }
}
if ($hasApp) {
    try { Remove-Item -LiteralPath $AppDir -Recurse -Force }
    catch { Say "Could not delete $AppDir : $($_.Exception.Message)"; $problems++ }
}

# ---- saved settings: only when asked ----------------------------------------------
$svFiles = @()
if ($Game) {
    $wtf = Join-Path $Game 'WTF\Account'
    if (Test-Path -LiteralPath $wtf -PathType Container) {
        foreach ($acct in @(Get-ChildItem -LiteralPath $wtf -Directory -ErrorAction SilentlyContinue)) {
            foreach ($n in @('GoblinLedger.lua', 'GoblinLedger.lua.bak')) {
                $f = Join-Path $acct.FullName ('SavedVariables\' + $n)
                if (Test-Path -LiteralPath $f -PathType Leaf) { $svFiles += $f }
            }
        }
    }
}
if ($svFiles.Count -gt 0) {
    Blank
    Say 'Your Goblin Ledger settings (watchlist, kept items, chart choices) are'
    Say 'still saved in the game folder, in case you install it again.'
    if (Ask-YesNo 'Delete those settings too? (Y/N, Enter = N) ' $false) {
        foreach ($f in $svFiles) {
            try { Remove-Item -LiteralPath $f -Force } catch { Say "Could not delete $f"; $problems++ }
        }
        Say 'Settings deleted.'
    } else {
        Say 'Settings kept.'
    }
}

Blank
if ($problems -gt 0) {
    Say 'Goblin Ledger is partly removed (see above).'
    exit 1
}
Say 'Goblin Ledger is removed. If WoW is open, type /reload or log out.'
exit 0
