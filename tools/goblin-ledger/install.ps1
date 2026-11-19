<#
  Goblin Ledger installer. Started by Install-Goblin-Ledger.bat (double-click
  that, not this). Plain text: anyone can read what it does.

    1. finds WoW Forever (or asks where it is)
    2. copies the addon into the game's Interface\AddOns\GoblinLedger folder,
       replacing an older copy (prices already there are kept)
    3. moves an install from before the rename (Forever AH) over: removes the
       old ForeverAH addon folder and copies its saved settings
    4. puts the price updater and Update-Goblin-Ledger.bat in
       %LOCALAPPDATA%\GoblinLedger, outside the unzipped folder, so that
       folder can be deleted afterwards
    5. downloads the latest prices into the addon right away
    6. if you say yes: a hidden Windows task, "Goblin Ledger Prices", that
       fetches new prices every 30 minutes while you are logged in
    7. if you say yes to that too: the same task checks every few hours for
       a new official Goblin Ledger version and installs it by itself
       (the choice is saved in %LOCALAPPDATA%\GoblinLedger\config.txt)

  Running it again updates the addon and fetches fresh prices. A Goblin
  Ledger that is newer than this download (it installed a new version by
  itself since) is kept as it is, and so is a newer updater.
  It needs no administrator rights unless Windows protects the game folder.

    -GameDir         the WoW Forever folder (_forever_); skips the search
    -AddonSource     the addon to install (default: the GoblinLedger folder
                     next to the installer, or wow-addon\GoblinLedger in the repo)
    -DataUrl         where the updater gets the prices (testing: a file path)
    -AutoUpdate      Y or N: answers the "every 30 minutes" question
    -AutoAddonUpdate Y or N: answers "Install new Goblin Ledger versions by
                     themselves?" (only asked when the 30-minute task is on)
  Test mode: see common.ps1.

  Exit codes: 0 installed, 1 not installed (the reason is printed).
#>
param(
    [string]$GameDir,
    [string]$AddonSource,
    [string]$DataUrl,
    [string]$AutoUpdate,
    [string]$AutoAddonUpdate
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
$AddonSource = [IO.Path]::GetFullPath($AddonSource).TrimEnd('\')
# The version of this download (both TOC files say it; make_release.ps1 checks that).
$SourceVersion = Get-AddonVersion $AddonSource
# The unzipped release: the folder with Install-Goblin-Ledger.bat and the
# scripts folder (in the repo: this folder).
$ReleaseRoot = $PSScriptRoot
$up = Split-Path -Parent $PSScriptRoot
if ($up -and (Test-Path -LiteralPath (Join-Path $up 'Install-Goblin-Ledger.bat') -PathType Leaf)) { $ReleaseRoot = $up }
$ReleaseRoot = [IO.Path]::GetFullPath($ReleaseRoot).TrimEnd('\')

# Whether $path is the folder $dir or inside it.
function Test-Within([string]$path, [string]$dir) {
    try {
        $a = [IO.Path]::GetFullPath($path).TrimEnd('\')
        $b = [IO.Path]::GetFullPath($dir).TrimEnd('\')
    } catch { return $false }
    return $a.Equals($b, [StringComparison]::OrdinalIgnoreCase) -or
           $a.StartsWith($b + '\', [StringComparison]::OrdinalIgnoreCase)
}

# Saved settings from before the rename that have no Goblin Ledger copy yet:
# one entry (account, old file, new file) per WTF\Account folder.
function Get-SettingsMoves([string]$game) {
    $moves = @()
    $wtf = Join-Path $game 'WTF\Account'
    if (-not (Test-Path -LiteralPath $wtf -PathType Container)) { return ,$moves }
    foreach ($acct in @(Get-ChildItem -LiteralPath $wtf -Directory -ErrorAction SilentlyContinue)) {
        $sv = Join-Path $acct.FullName 'SavedVariables'
        $old = Join-Path $sv 'ForeverAH.lua'
        $new = Join-Path $sv 'GoblinLedger.lua'
        if ((Test-Path -LiteralPath $old -PathType Leaf) -and -not (Test-Path -LiteralPath $new) -and
                (Select-String -LiteralPath $old -Pattern 'ForeverAH_Settings' -SimpleMatch -Quiet)) {
            $moves += ,@($acct.Name, $old, $new)
        }
    }
    return ,$moves
}

$Updater = $null
foreach ($cand in @((Join-Path $PSScriptRoot 'update_prices.ps1'), (Join-Path $PSScriptRoot '..\wow\addon\update_prices.ps1'))) {
    if (Test-Path -LiteralPath $cand) { $Updater = [IO.Path]::GetFullPath($cand); break }
}
if (-not $Updater) {
    Fail @('A file of the installer is missing (update_prices.ps1).',
           'Download GoblinLedger.zip again, unzip it and run Install-Goblin-Ledger.bat.')
}
$UpdateBat = $null
foreach ($cand in @((Join-Path $ReleaseRoot 'Update-Goblin-Ledger.bat'), (Join-Path $PSScriptRoot 'Update-Goblin-Ledger.bat'))) {
    if (Test-Path -LiteralPath $cand -PathType Leaf) { $UpdateBat = [IO.Path]::GetFullPath($cand); break }
}
if (-not $UpdateBat) {
    Fail @('A file of the installer is missing (Update-Goblin-Ledger.bat).',
           'Download GoblinLedger.zip again, unzip it and run Install-Goblin-Ledger.bat.')
}

# ---- 1. find the game ---------------------------------------------------------
if ($GameDir) {
    $found = Resolve-GameDir $GameDir
    if (-not $found -and $script:OtherVersion) {
        Fail @("That is another WoW version ($($script:OtherVersion)), not WoW Forever:", "    $GameDir")
    }
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
# The release unzipped straight into the game (AddOns, or AddOns\GoblinLedger):
# its files stay where they are.
$InGame = Test-Within $ReleaseRoot $GameDir
$wowOpen = Test-WowRunning $GameDir
if ($wowOpen -and ($OldIsOurs -or (Get-SettingsMoves $GameDir).Count -gt 0)) {
    # The running game still holds the Forever AH settings and writes them to
    # ForeverAH.lua when it closes: copying them before that would miss
    # whatever changed since the last login.
    Blank
    Say 'WoW Forever is running. Close the game fully now (exit it, not just'
    Say 'log out), so your Forever AH settings and watchlist carry over.'
    $skipped = $true
    for ($i = 0; $i -lt 20; $i++) {
        $a = ([string](Ask 'Press Enter once WoW is closed (or type S and Enter to skip): ')).Trim()
        if ($a -match '^(?i)s') { break }
        if (-not (Test-WowRunning $GameDir)) { $skipped = $false; $wowOpen = $false; break }
        Say 'WoW is still running (closing can take a few seconds).'
    }
    if ($skipped) { Say 'Skipped: changes made since you last logged in may not carry over.' }
    else { Say 'WoW is closed. Carrying on.' }
} elseif ($wowOpen) {
    Blank
    Say 'WoW is running. That is fine, but close it fully and start it again'
    Say 'after this: the game only finds a new addon when it starts.'
}
Blank

# ---- 2. copy the addon ------------------------------------------------------------
# Every file but Data.lua (the prices) is replaced; files the new version no
# longer has are removed. The prices already there are kept until new ones
# are downloaded below. Unzipped straight into AddOns, the addon is already
# in place (nothing to copy), and the unzipped files are never removed.
# An installed Goblin Ledger that is newer than this download (it installed
# a new version by itself) is kept: running an old download again for fresh
# prices never takes it back to the old version.
# While an automatic update runs, this waits for it (they share a lock).
$lockWait = 60
if ($script:TestMode -and $env:GOBLINLEDGER_TEST_LOCKWAIT -match '^\d+$') { $lockWait = [int]$env:GOBLINLEDGER_TEST_LOCKWAIT }
$lock = Enter-UpdateLock $lockWait
if (-not $lock.Owned) {
    Fail @('A Goblin Ledger update is running right now and has not finished.',
           'Wait a minute, then run Install-Goblin-Ledger.bat again. Nothing was changed.')
}
$sameDir = (Test-Within $AddonSource $Dest) -and (Test-Within $Dest $AddonSource)
$staged = Join-Path $Dest '_update'
$InstalledVersion = $null
# An automatic update that stopped half way: the files there are no version at all.
if (-not $sameDir -and -not (Test-Path -LiteralPath (Join-Path $staged 'swap.txt') -PathType Leaf)) {
    try { $InstalledVersion = Get-AddonVersion $Dest } catch { }
}
$keepNewer = $SourceVersion -and $InstalledVersion -and ($InstalledVersion -gt $SourceVersion)
if ($sameDir) {
    Say 'The addon is already in Interface\AddOns\GoblinLedger (unzipped there).'
} elseif ($keepNewer) {
    Say "You already have Goblin Ledger $InstalledVersion, newer than this download ($SourceVersion),"
    Say 'so it was kept as it is.'
} else {
    Say 'Copying the addon into Interface\AddOns\GoblinLedger ...'
}
try {
    New-Item -ItemType Directory -Path $Dest -Force | Out-Null
    # Left over from an automatic update that stopped half way: this copy replaces it.
    if (Test-Path -LiteralPath $staged -PathType Container) { Remove-Item -LiteralPath $staged -Recurse -Force }
    $keep = @{}
    if (-not $sameDir -and -not $keepNewer) {
        foreach ($f in @(Get-ChildItem -LiteralPath $AddonSource -Recurse -File)) {
            $rel = $f.FullName.Substring($AddonSource.Length).TrimStart('\')
            if ($rel -ieq 'Data.lua') { continue }
            $keep[$rel.ToLowerInvariant()] = $true
            $to = Join-Path $Dest $rel
            $toDir = Split-Path -Parent $to
            if (-not (Test-Path -LiteralPath $toDir)) { New-Item -ItemType Directory -Path $toDir -Force | Out-Null }
            Copy-Item -LiteralPath $f.FullName -Destination $to -Force
        }
    }
    foreach ($f in @(Get-ChildItem -LiteralPath $Dest -Recurse -File)) {
        if ($keepNewer) { break }
        $rel = $f.FullName.Substring($Dest.Length).TrimStart('\')
        if ($rel -ieq 'Data.lua' -or $keep.ContainsKey($rel.ToLowerInvariant())) { continue }
        if ((Test-Within $f.FullName $AddonSource) -or (Test-Within $f.FullName $ReleaseRoot)) { continue }
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
    $err = $_
    Exit-UpdateLock $lock
    if (Test-AccessError $err) { Fail $AccessLines }
    Fail @('Could not copy the addon into the game folder:', "    $($err.Exception.Message)",
           'Close WoW and try again.')
}
Exit-UpdateLock $lock
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
# Looked up again now: closing WoW may just have written ForeverAH.lua.
foreach ($mv in (Get-SettingsMoves $GameDir)) {
    $acctName, $old, $new = $mv
    # The old file stays as it is (a backup); the game now saves GoblinLedger.lua.
    try {
        Copy-Item -LiteralPath $old -Destination $new
        Say "Kept your settings and watchlist ($acctName)."
    } catch { Say "Could not copy your old settings for ${acctName}: $($_.Exception.Message)" }
}
if (-not (Remove-PriceTask $OldTaskName)) { Say "Could not remove the old ""$OldTaskName"" task. Task Scheduler can delete it." }

# ---- 4. the price updater, outside the unzipped folder -----------------------------
# config.txt says which version's installer put the updater there: a newer
# one than this download stays.
$ConfigFile = Join-Path $AppDir 'config.txt'
$SavedInstaller = $null
try {
    if (Test-Path -LiteralPath $ConfigFile -PathType Leaf) {
        $m = [regex]::Match([IO.File]::ReadAllText($ConfigFile), '(?m)^\s*installer\s*=\s*(\S+)\s*$')
        if ($m.Success) { $SavedInstaller = ConvertTo-Version $m.Groups[1].Value }
    }
} catch { }
$InstallerVersion = $SourceVersion
try {
    New-Item -ItemType Directory -Path $AppDir -Force | Out-Null
    $upd = Join-Path $AppDir 'update_prices.ps1'
    $updBat = Join-Path $AppDir 'Update-Goblin-Ledger.bat'
    if ($SourceVersion -and $SavedInstaller -and $SavedInstaller -gt $SourceVersion -and
            (Test-Path -LiteralPath $upd -PathType Leaf) -and (Test-Path -LiteralPath $updBat -PathType Leaf)) {
        $InstallerVersion = $SavedInstaller
        Say "Kept the newer updater (from Goblin Ledger $SavedInstaller)."
    } else {
        Copy-Item -LiteralPath $Updater -Destination $upd -Force
        try { Unblock-File -LiteralPath $upd -ErrorAction Stop } catch { }
        Copy-Item -LiteralPath $UpdateBat -Destination $updBat -Force
        try { Unblock-File -LiteralPath $updBat -ErrorAction Stop } catch { }
    }
    Save-PathFile (Join-Path $AppDir 'addon_dir.txt') $Dest
} catch {
    Fail @("Could not save the price updater in $AppDir", "    $($_.Exception.Message)")
}
# An older copy of the updater still pointed at the ForeverAH folder (the
# owner's price collector uses it): point it at the new folder.
$legacyPtr = Join-Path $LegacyAppDir 'addon_dir.txt'
if (Test-Path -LiteralPath $legacyPtr -PathType Leaf) {
    try { Save-PathFile $legacyPtr $Dest } catch { }
}

# ---- 5. prices now ------------------------------------------------------------------
Blank
Say 'Getting the latest prices...'
$ps = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
if (-not (Test-Path -LiteralPath $ps)) { $ps = (Get-Process -Id $PID).Path }
$updArgs = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $upd, '-AddonDir', $Dest, '-AddonCheck', 'skip')
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
        $ok = ($env:GOBLINLEDGER_TEST_TASKFAIL -ne '1')
        if ($ok) { Write-TaskLog "REGISTER $TaskName every 30 minutes from $($start.ToString('HH:mm')): $exe $taskArgs" }
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

# ---- 7. new versions by themselves (optional, needs the task above) --------------------
# Only asked when the task is really there: without it nothing checks by itself.
$taskOn = $auto -and $ok
Blank
Say '------------------------------------------------------------'
if ($taskOn) {
    Say 'The same task can also keep Goblin Ledger itself up to date: when a'
    Say 'new version is out, it downloads it, checks it is the official one'
    Say 'and swaps in the new addon files. Your prices and settings are kept.'
    Say 'Checked every few hours; only official releases are installed.'
    Say '------------------------------------------------------------'
    if ($AutoAddonUpdate) {
        $addonAuto = $AutoAddonUpdate.Trim() -match '^(?i)y'
        Say "Install new Goblin Ledger versions by themselves? $(if ($addonAuto) { 'Yes' } else { 'No' })"
    } else {
        $addonAuto = Ask-YesNo 'Install new Goblin Ledger versions by themselves? (Y/N, Enter = Y) ' $true
    }
} else {
    $addonAuto = $false
    Say 'New versions: double-click Update-Goblin-Ledger.bat whenever you want'
    Say 'the newest one (without the 30-minute task nothing checks by itself).'
    Say '------------------------------------------------------------'
}
try {
    $lines = @('# Goblin Ledger: your answers to the installer (run Install-Goblin-Ledger.bat again to change them).',
               ('addon_updates=' + $(if ($addonAuto) { 'yes' } else { 'no' })))
    # The version of the installer that put the updater here.
    if ($InstallerVersion) { $lines += ('installer=' + $InstallerVersion.ToString()) }
    Set-Content -LiteralPath $ConfigFile -Encoding ASCII -Value $lines
    # Look for a newer version at the next run of the task (this copy may be old).
    foreach ($n in @('addon_update.txt', 'release.json')) {
        Remove-Item -LiteralPath (Join-Path $AppDir $n) -Force -ErrorAction SilentlyContinue
    }
} catch {
    Say "Could not save that answer: $($_.Exception.Message)"
}
if ($taskOn) {
    Blank
    if ($addonAuto) {
        Say 'Done: new versions install by themselves. After one, the game says'
        Say '"Goblin Ledger updated to ..." in chat when you log in.'
    } else {
        Say 'OK, new versions are not installed by themselves. When one is out,'
        Say 'the game says so in chat: then double-click Update-Goblin-Ledger.bat.'
    }
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
Say ' The newest version right now: double-click Update-Goblin-Ledger.bat'
Say ' (a copy stays in %LOCALAPPDATA%\GoblinLedger).'
if ($InGame) {
    Say ' You can delete the zip now. Leave the files you unzipped into the'
    Say ' game folder where they are.'
} else {
    Say ' You can delete the zip and the unzipped folder now.'
}
Say '============================================================'
exit 0
