<#
  Forever AH price collector for this PC.

  GitHub's free scheduler runs hours late, so this PC takes the hourly auction
  house snapshot itself:
    1. gets the published price history (the wow-data branch of
       github.com/grant9008/grantdenney.com),
    2. runs fetch_ah.py with the Battle.net key saved in %USERPROFILE%\.wow-ah\bnet.txt,
    3. when Blizzard had new data: publishes it back to GitHub, so
       grantdenney.com/wow updates, and installs the new prices into the addon.
  GitHub's own (late) runs still work as a backup: both read and write the
  same branch, and an unchanged Blizzard snapshot is never collected twice.

  Started by the "ForeverAH Collector" scheduled task every 30 minutes.
  One line per run goes to %LOCALAPPDATA%\ForeverAH\collector.log.

    -Quiet   print nothing (the scheduled task uses this)
    -NoPush  collect and install, but do not publish to GitHub (testing)

  Exit codes: 0 done or nothing new, 1 failed (see the log), 2 already running.
#>
param(
    [switch]$Quiet,
    [switch]$NoPush,
    [string]$Repo = 'https://github.com/grant9008/grantdenney.com.git'
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$AppDir = Join-Path $env:LOCALAPPDATA 'ForeverAH'
$Work = Join-Path $AppDir 'collector'
$Data = Join-Path $Work 'wowdata'
$LogFile = Join-Path $AppDir 'collector.log'
$LockFile = Join-Path $Work 'running.lock'
$PS = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'

# git must never stop and wait for a password window in the background.
$env:GIT_TERMINAL_PROMPT = '0'
$env:GCM_INTERACTIVE = 'never'

function Say([string]$text) { if (-not $Quiet) { Write-Host "  $text" } }

function Write-Log([string]$status, [string]$text) {
    try {
        if (-not (Test-Path -LiteralPath $AppDir)) { New-Item -ItemType Directory -Path $AppDir -Force | Out-Null }
        $line = '{0}  {1,-5}  {2}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $status, $text
        $lines = @()
        if (Test-Path -LiteralPath $LogFile) { $lines = @(Get-Content -LiteralPath $LogFile -Encoding UTF8) }
        $lines = @($lines + $line)
        if ($lines.Count -gt 400) { $lines = $lines[($lines.Count - 400)..($lines.Count - 1)] }
        Set-Content -LiteralPath $LogFile -Value $lines -Encoding UTF8
    } catch { }
}

function Finish([int]$code, [string]$status, [string]$text) {
    if ($script:HaveLock) { Remove-Item -LiteralPath $LockFile -Force -ErrorAction SilentlyContinue }
    Say $text
    Write-Log $status $text
    exit $code
}

# Runs a program and returns @{ Out = its text output; Code = exit code }.
# Windows PowerShell 5.1 (what the scheduled task uses) would treat any line a
# program writes to its error stream as a fatal error, so that is switched off
# around the call and the exit code decides instead.
function Native([string]$exe, [string[]]$argList) {
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = & $exe @argList 2>&1
        $code = $LASTEXITCODE
    } finally { $ErrorActionPreference = $old }
    $text = (($out | ForEach-Object { "$_" }) -join "`n").Trim()
    return @{ Out = $text; Code = $code }
}

# Runs git in the data folder; returns its output, throws with the output on failure.
function GitData([string[]]$GitArgs) {
    $r = Native $GitExe (@('-C', $Data) + $GitArgs)
    if ($r.Code -ne 0) { throw ("git {0} failed: {1}" -f ($GitArgs -join ' '), $r.Out) }
    return $r.Out
}

if (-not (Test-Path -LiteralPath $Work)) { New-Item -ItemType Directory -Path $Work -Force | Out-Null }

# The git program itself (by its file path, so no PowerShell function can get in the way).
$GitExe = (Get-Command git -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1).Source
if (-not $GitExe) { Say 'Git is not installed on this PC.'; Write-Log 'ERROR' 'git not found'; exit 1 }

# One run at a time. A lock older than 40 minutes is left over from a crash.
if (Test-Path -LiteralPath $LockFile) {
    $age = ((Get-Date) - (Get-Item -LiteralPath $LockFile).LastWriteTime).TotalMinutes
    if ($age -lt 40) { Say 'Another collection is still running.'; Write-Log 'SKIP' 'another run is still going'; exit 2 }
}
Set-Content -LiteralPath $LockFile -Value $PID
$script:HaveLock = $true

# Python, saved by the setup (the scheduled task may not have the same PATH).
$py = $null
$pyFile = Join-Path $Work 'python.txt'
if (Test-Path -LiteralPath $pyFile) { $py = ([string](Get-Content -LiteralPath $pyFile -TotalCount 1)).Trim() }
if (-not $py -or -not (Test-Path -LiteralPath $py)) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { $py = $cmd.Source }
}
if (-not $py) { Finish 1 'ERROR' 'Python was not found on this PC.' }
$fetch = Join-Path $Work 'fetch_ah.py'
if (-not (Test-Path -LiteralPath $fetch)) { Finish 1 'ERROR' "collector script missing: $fetch" }
$key = Join-Path $env:USERPROFILE '.wow-ah\bnet.txt'
if (-not (Test-Path -LiteralPath $key)) { Finish 1 'ERROR' 'No Battle.net key on this PC (run Setup-WoW-Key.bat).' }

try {
    # 1. The published history, exactly as on GitHub (small: only the newest commit).
    if (-not (Test-Path -LiteralPath (Join-Path $Data '.git'))) {
        if (Test-Path -LiteralPath $Data) { Remove-Item -LiteralPath $Data -Recurse -Force }
        $r = Native $GitExe @('clone', '-q', '--depth', '1', '--single-branch', '--branch', 'wow-data', $Repo, $Data)
        if ($r.Code -ne 0) { throw ("git clone failed: " + $r.Out) }
        GitData @('config', 'user.name', 'ForeverAH collector (PC)') | Out-Null
        GitData @('config', 'user.email', 'foreverah-collector@localhost') | Out-Null
        GitData @('config', 'gc.auto', '0') | Out-Null
    } else {
        GitData @('fetch', '-q', '--depth', '1', 'origin', 'wow-data') | Out-Null
        GitData @('checkout', '-q', '-f', '-B', 'wow-data', 'FETCH_HEAD') | Out-Null
        GitData @('clean', '-q', '-fd') | Out-Null
    }
    $before = GitData @('rev-parse', 'HEAD')

    # 2. Collect. fetch_ah.py skips by itself when Blizzard has nothing new.
    $r = Native $py @($fetch, $Data)
    $fetchText = ($r.Out -replace '\s+', ' ')
    if ($r.Code -ne 0) { throw ("fetch_ah.py failed: " + $fetchText) }
    $changes = GitData @('status', '--porcelain')
    if (-not $changes) { Finish 0 'SKIP' ("nothing new from Blizzard. " + $fetchText).Trim() }

    # 3. Publish: one fresh commit holding the whole tree, like the GitHub job does.
    $stamp = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mmZ')
    if (GitData @('branch', '--list', 'publish')) { GitData @('branch', '-D', 'publish') | Out-Null }
    GitData @('checkout', '-q', '--orphan', 'publish') | Out-Null
    GitData @('add', '-A') | Out-Null
    GitData @('commit', '-q', '-m', "WoW Forever prices $stamp (PC)") | Out-Null
    $published = 'not published (test run)'
    if (-not $NoPush) {
        # Only replace what this run started from, so a newer snapshot that
        # GitHub's own job published meanwhile is never overwritten.
        GitData @('push', '-q', "--force-with-lease=wow-data:$before", 'origin', 'publish:wow-data') | Out-Null
        $published = 'published'
    }
    GitData @('checkout', '-q', '-f', '-B', 'wow-data', 'publish') | Out-Null
    GitData @('branch', '-D', 'publish') | Out-Null
    # Old snapshots are not needed on this PC: keep the folder small.
    GitData @('reflog', 'expire', '--expire=now', '--all') | Out-Null
    GitData @('gc', '-q', '--prune=now') | Out-Null

    # 4. Install the new prices into the addon (the updater checks the file first).
    $upd = Join-Path $AppDir 'update_prices.ps1'
    $installed = 'addon not updated (updater missing)'
    if (Test-Path -LiteralPath $upd) {
        $r = Native $PS @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $upd, '-DataUrl', (Join-Path $Data 'ForeverAH_Data.lua'), '-Quiet')
        $installed = if ($r.Code -eq 0) { 'addon updated' } else { "addon not updated (updater exit $($r.Code))" }
    }
    Finish 0 'OK' ("{0}, {1}. {2}" -f $published, $installed, $fetchText)
} catch {
    Finish 1 'ERROR' ($_.Exception.Message -replace '\s+', ' ')
}
