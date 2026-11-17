<#
  Sets up the Forever AH price collector on this PC (see collect_prices.ps1):
  copies the scripts into %LOCALAPPDATA%\ForeverAH and creates the hidden
  "ForeverAH Collector" scheduled task, which runs every 30 minutes (at :05
  and :35, a few minutes after Blizzard publishes new auction data) while
  you are logged in to Windows, also on battery, and catches up after sleep.

    -FetchSource  use this fetch_ah.py instead of the repo's tools\wow\fetch_ah.py
    -NoTask       copy the files only, no scheduled task (testing)

  Uninstall-WoW-Addon.bat removes the task and the files again.
#>
param(
    [string]$FetchSource,
    [switch]$NoTask
)

$ErrorActionPreference = 'Stop'
$Here = $PSScriptRoot                                   # ...\tools\wow\addon
$Root = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $Here))
$AppDir = Join-Path $env:LOCALAPPDATA 'ForeverAH'
$Work = Join-Path $AppDir 'collector'
$TaskName = 'ForeverAH Collector'

function Say([string]$t) { Write-Host "  $t" }

if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Say 'Git is not installed on this PC, so prices cannot be published from here.'; exit 1 }
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { Say 'Python is not installed on this PC.'; exit 1 }
if (-not (Test-Path -LiteralPath (Join-Path $env:USERPROFILE '.wow-ah\bnet.txt'))) {
    Say 'No Battle.net key on this PC yet. Run Setup-WoW-Key.bat first.'; exit 1
}

New-Item -ItemType Directory -Path $Work -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $Here 'collect_prices.ps1') -Destination (Join-Path $AppDir 'collect_prices.ps1') -Force
Copy-Item -LiteralPath (Join-Path $Here 'update_prices.ps1') -Destination (Join-Path $AppDir 'update_prices.ps1') -Force
if (-not $FetchSource) { $FetchSource = Join-Path $Root 'tools\wow\fetch_ah.py' }
Copy-Item -LiteralPath $FetchSource -Destination (Join-Path $Work 'fetch_ah.py') -Force
Set-Content -LiteralPath (Join-Path $Work 'python.txt') -Value $py.Source -Encoding ASCII
Say "Collector files are in $AppDir"

if ($NoTask) { exit 0 }

$psexe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$conhost = Join-Path $env:SystemRoot 'System32\conhost.exe'
$script = Join-Path $AppDir 'collect_prices.ps1'
$psArgs = "-NoProfile -ExecutionPolicy Bypass -File `"$script`" -Quiet"
# conhost --headless runs it with no window at all, so nothing pops up in game.
if (Test-Path -LiteralPath $conhost) {
    $action = New-ScheduledTaskAction -Execute $conhost -Argument "--headless `"$psexe`" $psArgs"
} else {
    $action = New-ScheduledTaskAction -Execute $psexe -Argument "-WindowStyle Hidden $psArgs"
}
# First run at the next :05 or :35, at least 2 minutes from now.
$t = (Get-Date).AddMinutes(2)
$start = $t.Date.AddHours($t.Hour)
if ($t.Minute -lt 5) { $start = $start.AddMinutes(5) }
elseif ($t.Minute -lt 35) { $start = $start.AddMinutes(35) }
else { $start = $start.AddMinutes(65) }
$trigger = New-ScheduledTaskTrigger -Once -At $start -RepetitionInterval (New-TimeSpan -Minutes 30)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 25)
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Force `
    -Description 'Forever AH: takes the hourly WoW Forever auction house snapshot, publishes it to grantdenney.com/wow and updates the addon prices.' | Out-Null
# The price download task gets the same battery and catch-up settings.
if (Get-ScheduledTask -TaskName 'ForeverAH Prices' -ErrorAction SilentlyContinue) {
    Set-ScheduledTask -TaskName 'ForeverAH Prices' -Settings $settings | Out-Null
}
Say ("Done: this PC takes the price snapshot every 30 minutes, starting {0}." -f $start.ToString('h:mm tt'))
