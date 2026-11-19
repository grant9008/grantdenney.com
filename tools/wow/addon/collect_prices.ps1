<#
  Goblin Ledger price collector for this PC (the addon was called Forever AH).

  GitHub's free scheduler runs hours late, so this PC takes the hourly auction
  house snapshot itself:
    1. gets the published price history (the wow-data branch of
       github.com/grant9008/grantdenney.com),
    2. runs fetch_ah.py with the Battle.net key saved in %USERPROFILE%\.wow-ah\bnet.txt,
    3. when Blizzard had new data: installs the new prices into the addon and
       publishes them back to GitHub, so grantdenney.com/goblin-ledger/prices updates.
  GitHub's own (late) runs still work as a backup: both read and write the
  same branch, neither overwrites a snapshot the other published meanwhile,
  and an unchanged Blizzard snapshot is never collected twice.

  The wow-data branch is the only copy of the price history (Blizzard keeps
  none), so this script refuses to follow a history that suddenly shrank,
  never publishes deleted or half-written files, and keeps a daily backup of
  the last 14 days in %LOCALAPPDATA%\ForeverAH\collector\backup.

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
$Backup = Join-Path $Work 'backup'
$LogFile = Join-Path $AppDir 'collector.log'
$LockFile = Join-Path $Work 'running.lock'
$PS = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'

# git must never stop and wait for a password window in the background, and a
# stalled download gives up after a minute instead of hanging the run.
$env:GIT_TERMINAL_PROMPT = '0'
$env:GCM_INTERACTIVE = 'never'
$env:GIT_HTTP_LOW_SPEED_LIMIT = '1000'
$env:GIT_HTTP_LOW_SPEED_TIME = '60'

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
    $text = (($out | ForEach-Object {
        if ($_ -is [System.Management.Automation.ErrorRecord]) { $_.Exception.Message } else { "$_" }
    }) -join "`n").Trim()
    return @{ Out = $text; Code = $code }
}

# Runs git in the data folder; returns its output, throws with the output on failure.
function GitData([string[]]$GitArgs) {
    $r = Native $GitExe (@('-C', $Data) + $GitArgs)
    if ($r.Code -ne 0) { throw ("git {0} failed: {1}" -f ($GitArgs -join ' '), $r.Out) }
    return $r.Out
}

# Keeps the local copy small: old snapshots are not needed on this PC once
# they are on GitHub (and in the daily backup). Never fails a run.
function Tidy {
    try {
        $co = GitData @('count-objects', '-v')
        if ($co -match '(?m)^count: [1-9]' -or $co -match '(?m)^packs: ([2-9]|\d{2,})') {
            GitData @('reflog', 'expire', '--expire=now', '--all') | Out-Null
            GitData @('gc', '-q', '--prune=now') | Out-Null
        }
    } catch { Write-Log 'WARN' ('tidy: ' + ($_.Exception.Message -replace '\s+', ' ')) }
}

function Count-Hist([string]$rev) {
    $r = Native $GitExe @('-C', $Data, 'ls-tree', '--name-only', $rev, 'hist/')
    if ($r.Code -ne 0) { return 0 }
    return @(($r.Out -split "`n") | Where-Object { $_ }).Count
}

if (-not (Test-Path -LiteralPath $Work)) { New-Item -ItemType Directory -Path $Work -Force | Out-Null }

# The git program itself (by its file path, so no PowerShell function can get in the way).
$GitExe = (Get-Command git -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1).Source
if (-not $GitExe) { Say 'Git is not installed on this PC.'; Write-Log 'ERROR' 'git not found'; exit 1 }

# One run at a time. A lock older than 40 minutes is left over from a crash
# (the scheduled task stops a run after 25 minutes).
if (Test-Path -LiteralPath $LockFile) {
    $age = ((Get-Date) - (Get-Item -LiteralPath $LockFile).LastWriteTime).TotalMinutes
    if ($age -lt 40) { Say 'Another collection is still running.'; Write-Log 'SKIP' 'another run is still going'; exit 2 }
}
Set-Content -LiteralPath $LockFile -Value $PID
$script:HaveLock = $true

# We hold the run lock, so any git lock file left in the copy is from a run
# that was stopped halfway (shutdown, logoff): clear it or git refuses forever.
$gitDir = Join-Path $Data '.git'
if (Test-Path -LiteralPath $gitDir) {
    Get-ChildItem -LiteralPath $gitDir -Recurse -Force -File -Filter '*.lock' -ErrorAction SilentlyContinue |
        Remove-Item -Force -ErrorAction SilentlyContinue
}

# Python, saved by the setup (the scheduled task may not have the same PATH).
$py = $null
$pyFile = Join-Path $Work 'python.txt'
if (Test-Path -LiteralPath $pyFile) { $py = ([string](Get-Content -LiteralPath $pyFile -TotalCount 1)).Trim() }
if (-not $py -or -not (Test-Path -LiteralPath $py)) {
    $cmd = Get-Command python -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($cmd) { $py = $cmd.Source }
}
if (-not $py) { Finish 1 'ERROR' 'Python was not found on this PC.' }
$fetch = Join-Path $Work 'fetch_ah.py'
if (-not (Test-Path -LiteralPath $fetch)) { Finish 1 'ERROR' "collector script missing: $fetch" }
$key = Join-Path $env:USERPROFILE '.wow-ah\bnet.txt'
if (-not (Test-Path -LiteralPath $key)) { Finish 1 'ERROR' 'No Battle.net key on this PC (run Setup-WoW-Key.bat).' }

# Snapshots are filed by time, so a PC clock that is off would put prices in
# the wrong hours of the history. Checked against GitHub's clock.
try {
    $h = Invoke-WebRequest -UseBasicParsing -Method Head -Uri 'https://github.com' -TimeoutSec 30
    $off = ((Get-Date).ToUniversalTime() - [DateTimeOffset]::Parse($h.Headers['Date']).UtcDateTime).TotalMinutes
    if ([math]::Abs($off) -gt 5) { Finish 1 'ERROR' ("this PC's clock is {0:N0} minutes off, nothing collected (fix the Windows time)" -f $off) }
} catch { }   # offline: the steps below fail with a clear message anyway

try {
    $published = $null
    $installed = 'addon not updated'
    $fetchText = ''
    for ($try = 1; $try -le 2; $try++) {
        # 1. The published history, exactly as on GitHub (only the newest commit).
        $fresh = $true
        if (Test-Path -LiteralPath (Join-Path $Data '.git')) {
            try {
                GitData @('fetch', '-q', '--depth', '1', 'origin', 'wow-data') | Out-Null
                # The history only ever grows. If GitHub's copy suddenly has far
                # fewer items, something went wrong there: keep ours, stop.
                $had = Count-Hist 'HEAD'
                $got = Count-Hist 'FETCH_HEAD'
                if ($had -gt 100 -and $got -lt [int]($had * 0.9)) {
                    New-Item -ItemType Directory -Path $Backup -Force | Out-Null
                    $b = Join-Path $Backup ('wow-data-before-shrink-{0}.bundle' -f (Get-Date -Format 'yyyyMMdd-HHmm'))
                    GitData @('bundle', 'create', $b, 'HEAD') | Out-Null
                    Finish 1 'ERROR' "GitHub's price history shrank from $had to $got items. Nothing collected or published; the previous history is kept in $b"
                }
                GitData @('checkout', '-q', '-f', '-B', 'wow-data', 'FETCH_HEAD') | Out-Null
                GitData @('clean', '-q', '-fd') | Out-Null
                $fresh = $false
            } catch {
                # The local folder is only a copy of GitHub's: start it again.
                Write-Log 'WARN' ('getting a fresh copy: ' + ($_.Exception.Message -replace '\s+', ' '))
            }
        }
        if ($fresh) {
            if (Test-Path -LiteralPath $Data) { Remove-Item -LiteralPath $Data -Recurse -Force }
            $r = Native $GitExe @('clone', '-q', '--depth', '1', '--single-branch', '--branch', 'wow-data', $Repo, $Data)
            if ($r.Code -ne 0) { throw ("git clone failed: " + $r.Out) }
        }
        # Every run, so a copy whose first setup was interrupted is fixed too.
        GitData @('config', 'user.name', 'ForeverAH collector (PC)') | Out-Null
        GitData @('config', 'user.email', 'foreverah-collector@localhost') | Out-Null
        GitData @('config', 'gc.auto', '0') | Out-Null
        if (GitData @('branch', '--list', 'publish')) { GitData @('branch', '-D', 'publish') | Out-Null }
        Tidy
        $before = GitData @('rev-parse', 'HEAD')

        # 2. Collect. fetch_ah.py skips by itself when Blizzard has nothing new.
        $r = Native $py @($fetch, $Data)
        $fetchText = ($r.Out -replace '\s+', ' ')
        if ($r.Code -ne 0) { throw ("fetch_ah.py failed: " + $fetchText) }
        # Half-written leftovers are never published.
        Get-ChildItem -LiteralPath $Data -Recurse -Force -File -Filter '*.tmp' -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -notlike '*\.git\*' } | Remove-Item -Force -ErrorAction SilentlyContinue
        $changes = GitData @('status', '--porcelain')
        if (-not $changes) {
            if ($published) { break }   # second pass: GitHub's snapshot already had it
            Finish 0 'SKIP' ("nothing new from Blizzard. " + $fetchText).Trim()
        }
        # The collector only ever adds and updates files. Many deletions mean
        # something outside it (antivirus, a disk error) removed history.
        $gone = @(($changes -split "`n") | Where-Object { $_ -match '^(?: D|D )' })
        if ($gone.Count -gt 20) {
            throw ("refusing to publish: {0} history files would be deleted (first: {1})" -f $gone.Count, $gone[0].Substring(3))
        }

        # 3. One fresh local commit holding the whole tree, like the GitHub job.
        $stamp = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mmZ')
        GitData @('checkout', '-q', '--orphan', 'publish') | Out-Null
        GitData @('add', '-A') | Out-Null
        GitData @('commit', '-q', '-m', "WoW Forever prices $stamp (PC)") | Out-Null

        # 4. Install the new prices into the addon first (the updater checks
        #    the file), so the game gets them even if publishing fails. The
        #    updater finds the GoblinLedger addon from the path the installer
        #    saved (see update_prices.ps1).
        $upd = Join-Path $AppDir 'update_prices.ps1'
        if (Test-Path -LiteralPath $upd) {
            $u = Native $PS @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $upd, '-DataUrl', (Join-Path $Data 'ForeverAH_Data.lua'), '-Quiet')
            $installed = if ($u.Code -eq 0) { 'addon updated' } else { "addon not updated (updater exit $($u.Code))" }
        }

        # 5. Publish. Only replace what this run started from, so a newer
        #    snapshot that GitHub's own job published meanwhile is never
        #    overwritten. If that happened, collect once more on top of it.
        if ($NoPush) { $published = 'not published (test run)'; break }
        $r = Native $GitExe @('-C', $Data, 'push', '-q', "--force-with-lease=wow-data:$before", 'origin', 'publish:wow-data')
        if ($r.Code -eq 0) { $published = 'published'; break }
        if ($try -eq 2 -or $r.Out -notmatch 'stale info|rejected|fetch first') { throw ('git push failed: ' + $r.Out) }
        Write-Log 'INFO' 'GitHub published a snapshot first; collecting again on top of it'
        GitData @('checkout', '-q', '-f', '-B', 'wow-data', $before) | Out-Null
        GitData @('branch', '-D', 'publish') | Out-Null
        $published = $null
    }

    if (GitData @('branch', '--list', 'publish')) {
        GitData @('checkout', '-q', '-f', '-B', 'wow-data', 'publish') | Out-Null
        GitData @('branch', '-D', 'publish') | Out-Null
    }
    # A daily backup of the whole history, the last 14 days kept.
    try {
        New-Item -ItemType Directory -Path $Backup -Force | Out-Null
        $day = Join-Path $Backup ('wow-data-{0}.bundle' -f (Get-Date -Format 'yyyyMMdd'))
        if (-not (Test-Path -LiteralPath $day)) { GitData @('bundle', 'create', $day, 'wow-data') | Out-Null }
        Get-ChildItem -LiteralPath $Backup -Filter 'wow-data-2*.bundle' | Sort-Object Name -Descending |
            Select-Object -Skip 14 | Remove-Item -Force -ErrorAction SilentlyContinue
    } catch { Write-Log 'WARN' ('backup: ' + ($_.Exception.Message -replace '\s+', ' ')) }
    Tidy
    if (-not $published) { $published = 'GitHub had it already' }
    Finish 0 'OK' ("{0}, {1}. {2}" -f $published, $installed, $fetchText)
} catch {
    Finish 1 'ERROR' ($_.Exception.Message -replace '\s+', ' ')
}
