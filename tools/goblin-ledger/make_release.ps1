<#
  Builds dist\GoblinLedger.zip, the download for friends:

    GoblinLedger\                  the addon, without Data.lua (the installer
                                   downloads the prices)
    Install-Goblin-Ledger.bat
    Uninstall-Goblin-Ledger.bat
    README.txt
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

# zip path -> source file
$files = New-Object System.Collections.ArrayList
function Add-File([string]$name, [string]$src) { [void]$files.Add([pscustomobject]@{ Name = $name; Src = $src }) }
foreach ($f in @(Get-ChildItem -LiteralPath $Addon -Recurse -File | Sort-Object FullName)) {
    $rel = $f.FullName.Substring($Addon.Length).TrimStart('\')
    if ($rel -ieq 'Data.lua') { continue }
    Add-File ('GoblinLedger/' + $rel.Replace('\', '/')) $f.FullName
}
foreach ($n in @('Install-Goblin-Ledger.bat', 'Uninstall-Goblin-Ledger.bat', 'README.txt')) {
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

Write-Host "Built $zipPath"
foreach ($it in $files) { Write-Host "  $($it.Name)" }
