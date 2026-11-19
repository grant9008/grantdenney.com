<#
  Goblin Ledger installer. Started by Install-Goblin-Ledger.bat (double-click
  that, not this). Plain text: anyone can read what it does.

    1. finds WoW Forever (or asks where it is)
    2. copies the addon into the game's Interface\AddOns\GoblinLedger folder,
       replacing an older copy (prices already there are kept)
    3. moves an install from before the rename (Forever AH) over: removes the
       old ForeverAH addon folder and copies its saved settings
    4. puts the price updater in %LOCALAPPDATA%\GoblinLedger, outside the
       unzipped folder, so that folder can be deleted afterwards
    5. downloads the latest prices into the addon right away
    6. if you say yes: a hidden Windows task, "Goblin Ledger Prices", that
       fetches new prices every 30 minutes while you are logged in

  Running it again updates the addon and fetches fresh prices.
  It needs no administrator rights unless Windows protects the game folder.

    -GameDir     the WoW Forever folder (_forever_); skips the search
    -AddonSource the addon to install (default: the GoblinLedger folder next
                 to the installer, or wow-addon\GoblinLedger in the repo)
    -DataUrl     where the updater gets the prices (testing: a file path)
    -AutoUpdate  Y or N: answers the "every 30 minutes" question
  Test mode: see common.ps1.

  Exit codes: 0 installed, 1 not installed (the reason is printed).
#>
param(
    [string]$GameDir,
    [string]$AddonSource,
    [string]$DataUrl,
    [string]$AutoUpdate
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
. (Join-Path $PSScriptRoot 'common.ps1')

$AppDir = Join-Path $env:LOCALAPPDATA 'GoblinLedger'
$LegacyAppDir = Join-Path $env:LOCALAPPDATA 'ForeverAH'
$Placeholder = @(
    '-- Goblin Ledger price data placeholder. Run Install-Goblin-Ledger.bat.',
    'ForeverAH_Data = ForeverAH_Data or { v = 1, updated = 0, items = {} }'
)

function Fail([string[]]$lines) {
    Blank
    foreach ($l in $lines) { Say $l }
    exit 1
}

function Get-PriceStamp([string]$file) {
    try {
        if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { return [int64]0 }
        $fs = [IO.File]::OpenRead($file)
        try { $buf = New-Object byte[] 4096; $n = $fs.Read($buf, 0, $buf.Length) } finally { $fs.Dispose() }
        $m = [regex]::Match([Text.Encoding]::UTF8.GetString($buf, 0, $n), '\bupdated\s*=\s*(\d+)')
        if ($m.Success) { return [int64]$m.Groups[1].Value }
    } catch { }
    return [int64]0
}

Blank
Say '============================================================'
Say ' Goblin Ledger: auction house prices, charts and price advice'
Say ' inside WoW Forever (Alliance, US PvP realms)'
Say '============================================================'
Blank

# ---- what to install ------------------------------------------------------------
if (-not $AddonSource) {
    foreach ($cand in @((Join-Path $PSScriptRoot '..\GoblinLedger'), (Join-Path $PSScriptRoot 'GoblinLedger'),
                        (Join-Path $PSScriptRoot '..\..\wow-addon\GoblinLedger'))) {
        if (Test-Path -LiteralPath (Join-Path $cand 'GoblinLedger.toc')) { $AddonSource = $cand; break }
    }
}
if (-not $AddonSource -or -not (Test-Path -LiteralPath (Join-Path $AddonSource 'GoblinLedger.toc'))) {
    Fail @('The GoblinLedger addon folder was not found next to the installer.',
           'Unzip GoblinLedger.zip first (right-click it, Extract All), then run',
           'Install-Goblin-Ledger.bat from the unzipped folder.')
}
$AddonSource = [IO.Path]::GetFullPath($AddonSource)
$Updater = $null
foreach ($cand in @((Join-Path $PSScriptRoot 'update_prices.ps1'), (Join-Path $PSScriptRoot '..\wow\addon\update_prices.ps1'))) {
    if (Test-Path -LiteralPath $cand) { $Updater = [IO.Path]::GetFullPath($cand); break }
}
if (-not $Updater) {
    Fail @('A file of the installer is missing (update_prices.ps1).',
           'Download GoblinLedger.zip again, unzip it and run Install-Goblin-Ledger.bat.')
}

# ---- 1. find the game ---------------------------------------------------------
if ($GameDir) {
    $found = Resolve-GameDir $GameDir
    if (-not $found) { Fail @("This is not the WoW Forever folder: $GameDir") }
    $GameDir = $found
} else {
    $GameDir = Find-GameDir
    if (-not $GameDir) { $GameDir = Ask-GameDir }
    if (-not $GameDir) {
        Fail @('Nothing was installed. Run Install-Goblin-Ledger.bat again when you',
               'know where WoW Forever is. (Never started WoW Forever on this PC?',
               'Install it from the Battle.net app and start it once first.)')
    }
}
$Addons = Join-Path $GameDir 'Interface\AddOns'
$Dest = Join-Path $Addons 'GoblinLedger'
$OldDest = Join-Path $Addons 'ForeverAH'
# Another addon is also called Forever AH: only a ForeverAH folder that is
# this addon from before the rename (its TOC names grantdenney.com) is ours.
$OldIsOurs = $false
$oldToc = Join-Path $OldDest 'ForeverAH.toc'
if (Test-Path -LiteralPath $oldToc -PathType Leaf) {
    try { $OldIsOurs = ([IO.File]::ReadAllText($oldToc)) -match 'grantdenney\.com' } catch { }
}
$AccessLines = @(
    'Windows did not allow copying into the game folder:',
    "    $Addons",
    'Fix: close this window, right-click Install-Goblin-Ledger.bat and choose',
    '"Run as administrator", then say Yes. That also lets the price updates',
    'save into the GoblinLedger folder afterwards, without administrator rights.'
)
Say 'Found WoW Forever in:'
Say "  $GameDir"
if (-not $script:TestMode -and (Get-Process -Name 'Wow*' -ErrorAction SilentlyContinue)) {
    Blank
    Say 'WoW is running. That is fine, but close it fully and start it again'
    Say 'after this: the game only finds a new addon when it starts.'
}
Blank

# ---- 2. copy the addon ------------------------------------------------------------
# Every file but Data.lua (the prices) is replaced; files the new version no
# longer has are removed. The prices already there are kept until new ones
# are downloaded below.
Say 'Copying the addon into Interface\AddOns\GoblinLedger ...'
try {
    New-Item -ItemType Directory -Path $Dest -Force | Out-Null
    $keep = @{}
    foreach ($f in @(Get-ChildItem -LiteralPath $AddonSource -Recurse -File)) {
        $rel = $f.FullName.Substring($AddonSource.Length).TrimStart('\')
        if ($rel -ieq 'Data.lua') { continue }
        $keep[$rel.ToLowerInvariant()] = $true
        $to = Join-Path $Dest $rel
        $toDir = Split-Path -Parent $to
        if (-not (Test-Path -LiteralPath $toDir)) { New-Item -ItemType Directory -Path $toDir -Force | Out-Null }
        Copy-Item -LiteralPath $f.FullName -Destination $to -Force
    }
    foreach ($f in @(Get-ChildItem -LiteralPath $Dest -Recurse -File)) {
        $rel = $f.FullName.Substring($Dest.Length).TrimStart('\')
        if ($rel -ieq 'Data.lua' -or $keep.ContainsKey($rel.ToLowerInvariant())) { continue }
        Remove-Item -LiteralPath $f.FullName -Force
    }
    $data = Join-Path $Dest 'Data.lua'
    $oldData = Join-Path $OldDest 'Data.lua'
    # Moving over from Forever AH: its prices are better than none.
    if ($OldIsOurs -and (Get-PriceStamp $oldData) -gt (Get-PriceStamp $data)) {
        Copy-Item -LiteralPath $oldData -Destination $data -Force
    }
    if (-not (Test-Path -LiteralPath $data)) { Set-Content -LiteralPath $data -Value $Placeholder -Encoding ASCII }
} catch {
    if (Test-AccessError $_) { Fail $AccessLines }
    Fail @('Could not copy the addon into the game folder:', "    $($_.Exception.Message)",
           'Close WoW and try again.')
}
Say 'Done.'

# Started with "Run as administrator": the updates run as you, not as
# administrator, so the GoblinLedger folder (only that folder) gets "modify"
# for normal users.
if (-not $script:TestMode -and (Test-Admin)) {
    & icacls.exe $Dest /grant '*S-1-5-32-545:(OI)(CI)M' /T /Q 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) { Say 'Price updates can now save into the GoblinLedger folder without administrator rights.' }
}

# ---- 3. moving over from Forever AH (the name before) ------------------------------
if ($OldIsOurs) {
    try {
        Remove-Item -LiteralPath $OldDest -Recurse -Force
        Say 'Removed the old Forever AH addon (Goblin Ledger replaces it).'
    } catch {
        Blank
        Say 'Could not remove the old Forever AH addon folder:'
        Say "    $OldDest"
        Say 'Delete it by hand (close WoW first), or the game runs both.'
    }
}
$wtf = Join-Path $GameDir 'WTF\Account'
if (Test-Path -LiteralPath $wtf -PathType Container) {
    foreach ($acct in @(Get-ChildItem -LiteralPath $wtf -Directory -ErrorAction SilentlyContinue)) {
        $sv = Join-Path $acct.FullName 'SavedVariables'
        $old = Join-Path $sv 'ForeverAH.lua'
        $new = Join-Path $sv 'GoblinLedger.lua'
        if ((Test-Path -LiteralPath $old -PathType Leaf) -and -not (Test-Path -LiteralPath $new) -and
                (Select-String -LiteralPath $old -Pattern 'ForeverAH_Settings' -SimpleMatch -Quiet)) {
            # The old file stays as it is (a backup); the game now saves GoblinLedger.lua.
            try {
                Copy-Item -LiteralPath $old -Destination $new
                Say "Kept your settings and watchlist ($($acct.Name))."
            } catch { Say "Could not copy your old settings for $($acct.Name): $($_.Exception.Message)" }
        }
    }
}
if (-not (Remove-PriceTask $OldTaskName)) { Say "Could not remove the old ""$OldTaskName"" task. Task Scheduler can delete it." }

# ---- 4. the price updater, outside the unzipped folder -----------------------------
try {
    New-Item -ItemType Directory -Path $AppDir -Force | Out-Null
    $upd = Join-Path $AppDir 'update_prices.ps1'
    Copy-Item -LiteralPath $Updater -Destination $upd -Force
    try { Unblock-File -LiteralPath $upd -ErrorAction Stop } catch { }
    Set-Content -LiteralPath (Join-Path $AppDir 'addon_dir.txt') -Value $Dest -Encoding ASCII
} catch {
    Fail @("Could not save the price updater in $AppDir", "    $($_.Exception.Message)")
}
# An older copy of the updater still pointed at the ForeverAH folder (the
# owner's price collector uses it): point it at the new folder.
$legacyPtr = Join-Path $LegacyAppDir 'addon_dir.txt'
if (Test-Path -LiteralPath $legacyPtr -PathType Leaf) {
    try { Set-Content -LiteralPath $legacyPtr -Value $Dest -Encoding ASCII } catch { }
}

# ---- 5. prices now ------------------------------------------------------------------
Blank
Say 'Getting the latest prices...'
$ps = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
if (-not (Test-Path -LiteralPath $ps)) { $ps = (Get-Process -Id $PID).Path }
$updArgs = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $upd, '-AddonDir', $Dest)
if ($DataUrl) { $updArgs += @('-DataUrl', $DataUrl) }
& $ps @updArgs
$updCode = $LASTEXITCODE
if ($updCode -ne 0) {
    Say 'The addon is installed; it shows prices once a download works.'
    Say 'Run Install-Goblin-Ledger.bat again later to try once more.'
}

# ---- 6. every 30 minutes (optional) ----------------------------------------------------
Blank
Say '------------------------------------------------------------'
Say 'Prices on the auction house change all the time. This PC can fetch'
Say 'new ones by itself every 30 minutes, in the background, without any'
Say 'window popping up (a Windows task called "Goblin Ledger Prices").'
Say 'It only downloads when the prices changed. Uninstall-Goblin-Ledger.bat'
Say 'removes it again.'
Say '------------------------------------------------------------'
if ($AutoUpdate) {
    $auto = $AutoUpdate.Trim() -match '^(?i)y'
    Say "Update prices automatically every 30 minutes? $(if ($auto) { 'Yes' } else { 'No' })"
} else {
    $auto = Ask-YesNo 'Update prices automatically every 30 minutes? (Y/N, Enter = Y) ' $true
}
if ($auto) {
    $conhost = Join-Path $env:SystemRoot 'System32\conhost.exe'
    $psArgs = "-NoProfile -ExecutionPolicy Bypass -File `"$upd`" -Quiet"
    # conhost --headless runs it with no window at all, so nothing flashes up
    # or takes the keyboard while you play.
    if (Test-Path -LiteralPath $conhost) { $exe = $conhost; $taskArgs = "--headless `"$ps`" $psArgs" }
    else { $exe = $ps; $taskArgs = "-WindowStyle Hidden $psArgs" }
    # First run at the next :15 or :45 (Blizzard's data changes about once an
    # hour), at least 2 minutes from now.
    $t = (Get-Date).AddMinutes(2)
    $start = $t.Date.AddHours($t.Hour)
    if ($t.Minute -lt 15) { $start = $start.AddMinutes(15) }
    elseif ($t.Minute -lt 45) { $start = $start.AddMinutes(45) }
    else { $start = $start.AddMinutes(75) }
    $ok = $false
    if ($script:TestMode) {
        Write-TaskLog "REGISTER $TaskName every 30 minutes from $($start.ToString('HH:mm')): $exe $taskArgs"
        $ok = $true
    } else {
        try {
            $action = New-ScheduledTaskAction -Execute $exe -Argument $taskArgs
            $trigger = New-ScheduledTaskTrigger -Once -At $start -RepetitionInterval (New-TimeSpan -Minutes 30)
            $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
                -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
            Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Force `
                -Description 'Goblin Ledger: downloads the newest WoW Forever auction house prices into the addon every 30 minutes. Uninstall-Goblin-Ledger.bat removes it.' | Out-Null
            $ok = $true
        } catch {
            # Older Windows: the classic tool. Windows PowerShell does not escape
            # the quotes inside an argument for it, so that is done here.
            $tr = ('"' + $exe + '" ' + $taskArgs) -replace '"', '\"'
            & schtasks.exe /Create /F /SC MINUTE /MO 30 /ST $start.ToString('HH:mm') /TN $TaskName /TR $tr 2>&1 | Out-Null
            $ok = ($LASTEXITCODE -eq 0)
        }
    }
    Blank
    if ($ok) {
        Say 'Done: prices update by themselves every 30 minutes while you are'
        Say 'logged in to Windows.'
    } else {
        Say 'Windows did not create the automatic update. That is fine: run'
        Say 'Install-Goblin-Ledger.bat again whenever you want fresh prices.'
    }
} else {
    if (-not (Remove-PriceTask $TaskName)) { Say "Could not remove the ""$TaskName"" task." }
    Blank
    Say 'OK, no automatic updates. Run Install-Goblin-Ledger.bat again'
    Say 'whenever you want fresh prices.'
}

Blank
Say '============================================================'
Say ' All set. Next:'
Say '  1. Start WoW Forever and log in. Was WoW already open? Close it'
Say '     fully and start it again: the game only finds a NEW addon when'
Say '     it starts. (Later price updates only need /reload.)'
Say '  2. Hover an item you could sell: the Goblin Ledger lines show its'
Say '     prices. Type /gl help for the commands.'
Say ' Not showing? At the character screen click AddOns and make sure'
Say ' Goblin Ledger is ticked.'
Say ' You can delete the zip and the unzipped folder now.'
Say '============================================================'
exit 0
