<#
  Goblin Ledger: helpers shared by install.ps1 and uninstall.ps1 (dot-sourced,
  never run on its own).

  Test mode (GOBLINLEDGER_TEST=1, used by tools\wow\addon_test\test_installer.py):
    - nothing is looked up in the registry or on the real drives: only the
      folders listed in GOBLINLEDGER_SEARCH (separated by ;) are tried
    - no scheduled task is created, changed or deleted: what would happen is
      written to the file named by GOBLINLEDGER_TASKLOG instead, and the tasks
      listed in GOBLINLEDGER_TEST_TASKS (separated by ;) count as existing
    - answers to the questions come from GOBLINLEDGER_ANSWERS (separated by |)
    - GOBLINLEDGER_TEST_TASKFAIL=1: creating the task fails, as when Windows
      refuses it; GOBLINLEDGER_TEST_LOCKWAIT: seconds to wait for a running
      update (default 60)
  Tests also point LOCALAPPDATA at a temporary folder.
#>

$script:TestMode = ($env:GOBLINLEDGER_TEST -eq '1')
$script:Answers = New-Object System.Collections.Generic.Queue[string]
if ($env:GOBLINLEDGER_ANSWERS) {
    foreach ($a in ($env:GOBLINLEDGER_ANSWERS -split '\|')) { $script:Answers.Enqueue($a) }
}

$TaskName = 'Goblin Ledger Prices'
$OldTaskName = 'ForeverAH Prices'          # the price task from before the rename
# The owner's price collector ("ForeverAH Collector" and
# %LOCALAPPDATA%\ForeverAH\collector) is never touched by these scripts.

function Say([string]$text) { Write-Host "  $text" }
function Blank { Write-Host '' }

# One question. Test mode takes the next prepared answer.
function Ask([string]$question) {
    if ($script:Answers.Count -gt 0) {
        $a = $script:Answers.Dequeue()
        Write-Host "  $question$a"
        return $a
    }
    if ($script:TestMode) { Write-Host "  $question"; return '' }
    # Not Read-Host: it adds its own ": " after the question.
    Write-Host "  $question" -NoNewline
    $line = [Console]::ReadLine()
    if ($null -eq $line) { return '' }
    return $line
}

# Y/N question with a default for a plain Enter.
function Ask-YesNo([string]$question, [bool]$default) {
    for ($i = 0; $i -lt 5; $i++) {
        $a = ([string](Ask $question)).Trim().ToUpperInvariant()
        if ($a -eq '') { return $default }
        if ($a -eq 'Y' -or $a -eq 'YES') { return $true }
        if ($a -eq 'N' -or $a -eq 'NO') { return $false }
        Say 'Please type Y or N, then press Enter.'
    }
    return $default
}

# A folder that holds WoW Forever: called _forever_, or holding the game's
# Interface or WTF folder or a Wow*.exe.
function Test-GameDir([string]$dir) {
    if (-not $dir) { return $false }
    if (-not (Test-Path -LiteralPath $dir -PathType Container)) { return $false }
    if ((Split-Path -Leaf $dir) -eq '_forever_') { return $true }
    if (Test-Path -LiteralPath (Join-Path $dir 'Interface') -PathType Container) { return $true }
    if (Test-Path -LiteralPath (Join-Path $dir 'WTF') -PathType Container) { return $true }
    return [bool](Get-ChildItem -LiteralPath $dir -Filter 'Wow*.exe' -File -ErrorAction SilentlyContinue | Select-Object -First 1)
}

# What a player pasted: with or without quotes, the _forever_ folder, the
# World of Warcraft folder above it, or a folder inside _forever_ (Interface,
# AddOns). Returns the _forever_ folder, or $null. Another WoW version's
# folder (_retail_, _classic_, _classic_era_, _ptr_ ...) is never taken: its
# _forever_ neighbour is used when there is one, otherwise $null, and
# $script:OtherVersion names that folder for the message.
function Resolve-GameDir([string]$text) {
    $script:OtherVersion = $null
    if (-not $text) { return $null }
    $p = $text.Trim().Trim('"').Trim("'").Trim()
    if (-not $p) { return $null }
    $p = $p.TrimEnd('\', '/')
    if ($p -match '^[A-Za-z]:$') { $p += '\' }
    try { $p = [IO.Path]::GetFullPath($p) } catch { return $null }
    # Somewhere inside _forever_: cut the path there.
    $m = [regex]::Match($p, '^(.*?\\_forever_)(\\.*)?$', 'IgnoreCase')
    if ($m.Success -and (Test-Path -LiteralPath $m.Groups[1].Value -PathType Container)) { return $m.Groups[1].Value }
    if (Test-Path -LiteralPath (Join-Path $p '_forever_') -PathType Container) { return (Join-Path $p '_forever_') }
    # Another WoW version's folder, or its Interface or WTF folder.
    $m = [regex]::Match($p, '^(.+)\\(_[a-z0-9]+(?:_[a-z0-9]+)*_)(\\(?:Interface|WTF)(?:\\.*)?)?$', 'IgnoreCase')
    if ($m.Success -and $m.Groups[2].Value -ine '_forever_') {
        $script:OtherVersion = $m.Groups[2].Value
        $sib = Join-Path $m.Groups[1].Value '_forever_'
        if (Test-Path -LiteralPath $sib -PathType Container) { return $sib }
        return $null
    }
    if (Test-GameDir $p) { return $p }
    return $null
}

# Is WoW Forever running? Only a Wow* program started from the game folder
# counts (an addon manager such as WowUp does not). When Windows will not say
# where a program runs from (one started as administrator), only the game's
# own names count (Wow, WowClassic, WowT ...), as in update_prices.ps1.
# Test mode: the number in GOBLINLEDGER_TEST_WOW says how many checks still
# find it running.
$script:TestWowLeft = 0
if ($env:GOBLINLEDGER_TEST_WOW -match '^\d+$') { $script:TestWowLeft = [int]$env:GOBLINLEDGER_TEST_WOW }
function Test-WowRunning([string]$gameDir) {
    if ($script:TestMode) {
        if ($script:TestWowLeft -gt 0) { $script:TestWowLeft--; return $true }
        return $false
    }
    $root = $null
    try { $root = [IO.Path]::GetFullPath($gameDir).TrimEnd('\') + '\' } catch { }
    foreach ($p in @(Get-Process -Name 'Wow*' -ErrorAction SilentlyContinue)) {
        $path = $null
        try { $path = $p.Path } catch { }
        if ($path -and $root) {
            if ($path.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)) { return $true }
        } elseif ($p.ProcessName -match '^(?i)wow(classic)?[a-z]?(-64)?$') {
            return $true
        }
    }
    return $false
}

# The ## Version that both TOC files of a Goblin Ledger folder say, as a
# [Version]; $null when a TOC is missing, has none, or the two differ.
function Get-AddonVersion([string]$dir) {
    $v = $null
    foreach ($n in @('GoblinLedger.toc', 'GoblinLedger_Forever.toc')) {
        $p = Join-Path $dir $n
        if (-not (Test-Path -LiteralPath $p -PathType Leaf)) { return $null }
        $m = [regex]::Match([IO.File]::ReadAllText($p), '(?m)^##\s*Version\s*:\s*(\d{1,5})\.(\d{1,5})\.(\d{1,5})\s*$')
        if (-not $m.Success) { return $null }
        $x = New-Object Version ([int]$m.Groups[1].Value), ([int]$m.Groups[2].Value), ([int]$m.Groups[3].Value)
        if ($v -and $x -ne $v) { return $null }
        $v = $x
    }
    return $v
}

# "1.2.3" as a [Version], else $null.
function ConvertTo-Version([string]$text) {
    $m = [regex]::Match([string]$text, '^\s*(\d{1,5})\.(\d{1,5})\.(\d{1,5})\s*$')
    if (-not $m.Success) { return $null }
    return New-Object Version ([int]$m.Groups[1].Value), ([int]$m.Groups[2].Value), ([int]$m.Groups[3].Value)
}

# The lock update_prices.ps1 takes before it installs a new version or puts
# back one that stopped half way (the same name; tests: one per fake PC).
# Waits up to $seconds for a run that holds it. @{ Owned; Mutex }.
function Enter-UpdateLock([int]$seconds) {
    $name = 'Local\GoblinLedgerAddonUpdate'
    if ($script:TestMode) {
        $sha = [Security.Cryptography.SHA256]::Create()
        try { $h = $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes(([string]$env:LOCALAPPDATA).ToLowerInvariant())) }
        finally { $sha.Dispose() }
        $name += '_' + ([BitConverter]::ToString($h)).Replace('-', '').ToLowerInvariant().Substring(0, 16)
    }
    $l = @{ Owned = $true; Mutex = $null }
    try {
        $l.Mutex = New-Object Threading.Mutex($false, $name)
        try {
            $l.Owned = $l.Mutex.WaitOne(0)
            if (-not $l.Owned -and $seconds -gt 0) {
                Say 'Waiting for a Goblin Ledger update that is running right now...'
                $l.Owned = $l.Mutex.WaitOne($seconds * 1000)
            }
        } catch { $l.Owned = $true }   # abandoned by a run that stopped: ours now
    } catch { }   # Windows gave no lock at all: carry on without one
    if (-not $l.Owned -and $l.Mutex) { $l.Mutex.Dispose(); $l.Mutex = $null }
    return $l
}

function Exit-UpdateLock($l) {
    if ($l -and $l.Mutex) {
        if ($l.Owned) { try { $l.Mutex.ReleaseMutex() } catch { } }
        $l.Mutex.Dispose()
        $l.Mutex = $null
    }
}

# Saves a folder path for the updater and the uninstallers. Plain ASCII when
# it is (the owner's .bat files read the file with set /p, which would keep a
# byte-order mark); UTF-8 with a byte-order mark otherwise, so a name like
# "Jose" with an accent reads back right in Windows PowerShell.
function Save-PathFile([string]$file, [string]$path) {
    if ($path -match '[^\x00-\x7F]') { Set-Content -LiteralPath $file -Value $path -Encoding UTF8 }
    else { Set-Content -LiteralPath $file -Value $path -Encoding ASCII }
}

# Where WoW Forever usually is. Test mode: only GOBLINLEDGER_SEARCH.
function Get-GameCandidates {
    if ($script:TestMode) {
        if ($env:GOBLINLEDGER_SEARCH) { return @($env:GOBLINLEDGER_SEARCH -split ';' | Where-Object { $_ }) }
        return @()
    }
    $c = New-Object System.Collections.Generic.List[string]
    # Blizzard's install record: InstallPath is the WoW version started last
    # (often ...\_forever_\, but it can be _retail_ or _classic_), so look next to it too.
    foreach ($key in @('HKLM:\SOFTWARE\WOW6432Node\Blizzard Entertainment\World of Warcraft',
                       'HKLM:\SOFTWARE\Blizzard Entertainment\World of Warcraft')) {
        try {
            $ip = (Get-ItemProperty -LiteralPath $key -Name InstallPath -ErrorAction Stop).InstallPath
            if ($ip) {
                $ip = $ip.TrimEnd('\')
                $c.Add($ip); $c.Add((Join-Path (Split-Path -Parent $ip) '_forever_')); $c.Add((Join-Path $ip '_forever_'))
            }
        } catch { }
    }
    # The Battle.net uninstall entry knows the World of Warcraft folder.
    foreach ($key in @('HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\World of Warcraft',
                       'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\World of Warcraft')) {
        try {
            $il = (Get-ItemProperty -LiteralPath $key -Name InstallLocation -ErrorAction Stop).InstallLocation
            if ($il) { $c.Add((Join-Path $il.TrimEnd('\') '_forever_')) }
        } catch { }
    }
    # The usual folders, on every drive.
    $drives = @(Get-PSDrive -PSProvider FileSystem -ErrorAction SilentlyContinue | Where-Object { $_.Root -match '^[A-Za-z]:\\$' } |
        ForEach-Object { $_.Root })
    if (-not $drives) { $drives = @('C:\') }
    $first = @($drives | Where-Object { $_ -ieq 'C:\' }) + @($drives | Where-Object { $_ -ine 'C:\' })
    foreach ($d in $first) {
        foreach ($rel in @('Program Files (x86)\World of Warcraft\_forever_', 'Program Files\World of Warcraft\_forever_',
                          'World of Warcraft\_forever_', 'Games\World of Warcraft\_forever_',
                          'Battle.net\World of Warcraft\_forever_', 'Blizzard\World of Warcraft\_forever_')) {
            $c.Add((Join-Path $d $rel))
        }
    }
    return $c.ToArray()
}

function Find-GameDir {
    foreach ($cand in (Get-GameCandidates)) {
        try {
            $full = [IO.Path]::GetFullPath($cand)
            if ((Split-Path -Leaf $full) -eq '_forever_' -and (Test-GameDir $full)) { return $full }
        } catch { }
    }
    return $null
}

# Asks for the game folder until a usable one is pasted (or the player gives up).
function Ask-GameDir {
    Blank
    Say 'Could not find WoW Forever on this PC by itself. Where is it?'
    Say '  1. In the Battle.net app, pick WoW Forever, click the gear next to Play'
    Say '     and choose "Show in Explorer".'
    Say '  2. Open the _forever_ folder, click the address bar at the top and press Ctrl+C.'
    Say '  3. Click this window, press Ctrl+V (or right-click) and press Enter.'
    Say 'Just pressing Enter stops here without changing anything.'
    for ($i = 0; $i -lt 3; $i++) {
        Blank
        $a = Ask 'Game folder: '
        if (-not ([string]$a).Trim()) { return $null }
        $dir = Resolve-GameDir $a
        if ($dir) { return $dir }
        if ($script:OtherVersion) {
            Say "That is another WoW version ($($script:OtherVersion)), not WoW Forever. Paste the"
            Say 'folder called _forever_ (in the Battle.net app pick WoW Forever first). Try again.'
        } else {
            Say 'That does not look like the WoW Forever folder (the one called _forever_,'
            Say 'which holds the game''s Interface folder). Try again.'
        }
    }
    return $null
}

function Test-Admin {
    try {
        $id = [Security.Principal.WindowsIdentity]::GetCurrent()
        return (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    } catch { return $false }
}

function Test-AccessError($err) {
    $e = $err.Exception
    while ($e) {
        if ($e -is [UnauthorizedAccessException]) { return $true }
        $e = $e.InnerException
    }
    return ($err.CategoryInfo.Category -eq 'PermissionDenied') -or ($err.Exception.Message -match 'denied')
}

# Scheduled tasks. Test mode only writes down what would happen.
function Write-TaskLog([string]$line) {
    if ($env:GOBLINLEDGER_TASKLOG) { Add-Content -LiteralPath $env:GOBLINLEDGER_TASKLOG -Value $line -Encoding ASCII }
}

function Test-TaskExists([string]$name) {
    if ($script:TestMode) {
        return @($env:GOBLINLEDGER_TEST_TASKS -split ';' | Where-Object { $_ -eq $name }).Count -gt 0
    }
    return [bool](Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue)
}

function Remove-PriceTask([string]$name) {
    if ($name -notin @($TaskName, $OldTaskName)) { throw "refusing to remove the task $name" }
    if (-not (Test-TaskExists $name)) { return $true }
    if ($script:TestMode) { Write-TaskLog "DELETE $name"; return $true }
    try {
        Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction Stop
        return $true
    } catch {
        & schtasks.exe /Delete /TN $name /F 2>&1 | Out-Null
        return ($LASTEXITCODE -eq 0)
    }
}
