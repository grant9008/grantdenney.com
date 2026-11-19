<#
  HOW TO SHIP AN UPDATE (friends get it by themselves)
    1. Bump "## Version: X.Y.Z" in BOTH wow-addon\GoblinLedger\GoblinLedger.toc
       and GoblinLedger_Forever.toc (higher than the last release, e.g. 1.0.0
       -> 1.1.0). This script refuses to build when the two differ.
    2. Build: powershell -NoProfile -ExecutionPolicy Bypass -File tools\goblin-ledger\make_release.ps1
    3. On GitHub (grant9008/grantdenney.com, Releases, "Draft a new release"):
       tag vX.Y.Z (exactly the TOC version with a v in front), attach
       dist\GoblinLedger.zip as the asset (keep that name), and publish it as
       the latest release (not a draft, not a pre-release).
    Friends whose installer said Y to "Install new Goblin Ledger versions by
    themselves?" get it within about 6 hours (the 30-minute task asks GitHub
    at most every 6 hours, and waits while WoW is open); the others see
    "Goblin Ledger X.Y.Z is out" in chat and can double-click
    Update-Goblin-Ledger.bat. Their updater only installs a zip whose
    SHA-256 matches the one GitHub shows for the asset, whose TOCs say the
    tag's version, and whose GoblinLedger folder holds only .lua and .toc
    files (no subfolders: this script checks that too). A new or removed
    file in the TOC works, but needs a game restart (the chat line says so).
    The installer, the updater and the .bat files reach friends only when
    they run Install-Goblin-Ledger.bat from a new download.
    Every release of grant9008/grantdenney.com counts as a Goblin Ledger
    release: the share page's download and the updaters use the latest one.

  Builds dist\GoblinLedger.zip, the download for friends:

    GoblinLedger\                  the addon, without Data.lua (the installer
                                   downloads the prices)
    Install-Goblin-Ledger.bat
    Update-Goblin-Ledger.bat
    Uninstall-Goblin-Ledger.bat
    README.txt                     (its first line says the version)
    scripts\install.ps1, uninstall.ps1, common.ps1, update_prices.ps1

  Every text file goes in with Windows line endings (a .bat with Unix line
  endings can break) and must be plain ASCII. Nothing is uploaded: attach the
  zip to a GitHub release by hand (its name must stay GoblinLedger.zip, the
  share page links to releases/latest/download/GoblinLedger.zip).

    powershell -NoProfile -ExecutionPolicy Bypass -File tools\goblin-ledger\make_release.ps1
    -OutDir  where to write the zip (default: dist at the repo root)
#>
param([string]$OutDir)

$ErrorActionPreference = 'Stop'
$Here = $PSScriptRoot
$Root = [IO.Path]::GetFullPath((Join-Path $Here '..\..'))
if (-not $OutDir) { $OutDir = Join-Path $Root 'dist' }
$Addon = Join-Path $Root 'wow-addon\GoblinLedger'

function Stop-Build([string]$why) {
    Write-Host "Not built: $why"
    exit 1
}

# The version: both TOC files must say the same one.
$versions = @()
foreach ($t in @('GoblinLedger.toc', 'GoblinLedger_Forever.toc')) {
    $p = Join-Path $Addon $t
    $m = $null
    if (Test-Path -LiteralPath $p -PathType Leaf) {
        $m = [regex]::Match([IO.File]::ReadAllText($p), '(?m)^##\s*Version\s*:\s*(\d{1,5}\.\d{1,5}\.\d{1,5})\s*$')
    }
    if (-not $m -or -not $m.Success) { Stop-Build "$t has no ""## Version: X.Y.Z"" line." }
    $versions += $m.Groups[1].Value
}
if ($versions[0] -ne $versions[1]) {
    Stop-Build "the TOC versions differ (GoblinLedger.toc $($versions[0]), GoblinLedger_Forever.toc $($versions[1])). Set both to the same X.Y.Z."
}
$Version = $versions[0]
Write-Host "Goblin Ledger version $Version (GitHub release tag: v$Version)"
# The updater on friends' PCs installs only .lua and .toc files at the top of the addon folder.
foreach ($f in @(Get-ChildItem -LiteralPath $Addon -Recurse -Force)) {
    if ($f.PSIsContainer) { Stop-Build "a folder inside the addon ($($f.Name)) would stop automatic updates. Keep the addon flat." }
    if ($f.Name -notmatch '^[A-Za-z0-9_][A-Za-z0-9_.-]*\.(lua|toc)$') {
        Stop-Build "only .lua and .toc files can be in the addon folder (automatic updates install nothing else): $($f.Name)"
    }
}

# zip path -> source file
$files = New-Object System.Collections.ArrayList
function Add-File([string]$name, [string]$src) { [void]$files.Add([pscustomobject]@{ Name = $name; Src = $src }) }
foreach ($f in @(Get-ChildItem -LiteralPath $Addon -Recurse -File | Sort-Object FullName)) {
    $rel = $f.FullName.Substring($Addon.Length).TrimStart('\')
    if ($rel -ieq 'Data.lua') { continue }
    Add-File ('GoblinLedger/' + $rel.Replace('\', '/')) $f.FullName
}
foreach ($n in @('Install-Goblin-Ledger.bat', 'Update-Goblin-Ledger.bat', 'Uninstall-Goblin-Ledger.bat', 'README.txt')) {
    Add-File $n (Join-Path $Here $n)
}
foreach ($n in @('install.ps1', 'uninstall.ps1', 'common.ps1')) {
    Add-File "scripts/$n" (Join-Path $Here $n)
}
Add-File 'scripts/update_prices.ps1' (Join-Path $Root 'tools\wow\addon\update_prices.ps1')

$textExt = @('.bat', '.txt', '.ps1', '.lua', '.toc')
Add-Type -AssemblyName System.IO.Compression
New-Item -ItemType Directory -Path $OutDir -Force | Out-Null
$zipPath = Join-Path $OutDir 'GoblinLedger.zip'
$tmp = "$zipPath.tmp"
if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Force }
$fs = [IO.File]::Open($tmp, [IO.FileMode]::CreateNew)
try {
    $zip = New-Object IO.Compression.ZipArchive($fs, [IO.Compression.ZipArchiveMode]::Create)
    try {
        foreach ($it in $files) {
            $name, $src = $it.Name, $it.Src
            if (-not (Test-Path -LiteralPath $src -PathType Leaf)) { throw "missing: $src" }
            $bytes = [IO.File]::ReadAllBytes($src)
            if ($textExt -contains [IO.Path]::GetExtension($src).ToLowerInvariant()) {
                # Latin-1 maps every byte to one character, so a byte over 127 shows up here.
                $text = [Text.Encoding]::GetEncoding(28591).GetString($bytes)
                if ($text -match '[^\x00-\x7F]') { throw "not plain ASCII: $src" }
                $text = ($text -replace "`r`n", "`n") -replace "`n", "`r`n"
                if ($name -eq 'README.txt') {
                    if (-not $text.StartsWith("GOBLIN LEDGER`r`n")) { throw 'README.txt must start with the line GOBLIN LEDGER' }
                    $text = "GOBLIN LEDGER $Version" + $text.Substring(13)
                }
                $bytes = [Text.Encoding]::ASCII.GetBytes($text)
            }
            $entry = $zip.CreateEntry($name, [IO.Compression.CompressionLevel]::Optimal)
            $entry.LastWriteTime = (Get-Item -LiteralPath $src).LastWriteTime
            $s = $entry.Open()
            try { $s.Write($bytes, 0, $bytes.Length) } finally { $s.Dispose() }
        }
    } finally { $zip.Dispose() }
} finally { $fs.Dispose() }
Move-Item -LiteralPath $tmp -Destination $zipPath -Force

Write-Host "Built $zipPath (Goblin Ledger $Version)"
foreach ($it in $files) { Write-Host "  $($it.Name)" }
