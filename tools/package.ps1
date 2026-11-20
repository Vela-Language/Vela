# Vela packaging: windows_x86_64 tar.gz + Inno Setup installer.
#
# Usage:
#   powershell -File tools/package.ps1 -Version 0.1.0 -OutDir dist
#   powershell -File tools/package.ps1 -Version 0.1.1-dev.12 -SkipSetup
#
# The setup.exe is built with Inno Setup 6 (ISCC.exe).  On GitHub's
# windows-latest image it is preinstalled; on a developer machine install
# Inno Setup 6 or pass -SkipSetup.

param(
    [string]$Version = "",
    [string]$OutDir = "dist",
    [switch]$SkipSetup
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not $Version) {
    $Version = (& python tools\version.py show).Trim()
}
$base = ($Version -split '-')[0]
if ($base -notmatch '^\d+\.\d+\.\d+$') {
    throw "version must start with x.y.z: $Version"
}
$build = if ($env:VELA_BUILD_NUMBER) { $env:VELA_BUILD_NUMBER } else { "0" }
if ($build -notmatch '^\d+$') { throw "VELA_BUILD_NUMBER must be numeric: $build" }

Write-Host "packaging Vela $Version (base $base, build $build)"

$stage = Join-Path $env:TEMP ("vela_pkg_" + ($Version -replace '[^0-9A-Za-z\.-]', '_'))
if (Test-Path $stage) { Remove-Item -LiteralPath $stage -Recurse -Force }
$dest = Join-Path $stage "vela-$Version"
New-Item -ItemType Directory -Force -Path $dest | Out-Null

Copy-Item -Path 'vela' -Destination $dest -Recurse -Force
Copy-Item -Path 'vela.cmd','README.md','LICENSE','pyproject.toml' -Destination $dest -Force
Copy-Item -Path 'examples','docs' -Destination $dest -Recurse -Force
Get-ChildItem -Path $dest -Recurse -Directory -Filter '__pycache__' |
    Remove-Item -Recurse -Force

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$tar = Join-Path $OutDir "vela-$Version-windows-x86_64.tar.gz"
if (Test-Path $tar) { Remove-Item -LiteralPath $tar -Force }
tar -czf $tar -C $stage "vela-$Version"
if ($LASTEXITCODE -ne 0) { throw "tar failed" }
Write-Host ("tar.gz: {0} ({1:N0} bytes)" -f $tar, (Get-Item $tar).Length)

if (-not $SkipSetup) {
    $candidates = @(
        'C:\Program Files (x86)\Inno Setup 6\ISCC.exe',
        'C:\Program Files\Inno Setup 6\ISCC.exe'
    )
    $iscc = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $iscc) {
        $cmd = Get-Command iscc.exe -ErrorAction SilentlyContinue
        if ($cmd) { $iscc = $cmd.Source }
    }
    if (-not $iscc) {
        throw "ISCC.exe not found; install Inno Setup 6 or pass -SkipSetup"
    }
    & $iscc "/DMyAppVersion=$base" "/DMyAppDisplayVersion=$Version" `
        "/DMyAppBuild=$build" "installer\vela.iss"
    if ($LASTEXITCODE -ne 0) { throw "ISCC failed with exit code $LASTEXITCODE" }

    $setup = Join-Path 'dist' "vela-$Version-setup.exe"
    if (-not (Test-Path $setup)) { throw "installer not produced: $setup" }
    if ((Resolve-Path $OutDir).Path -ne (Resolve-Path 'dist').Path) {
        Copy-Item -LiteralPath $setup -Destination $OutDir -Force
    }
    Write-Host ("setup:  {0} ({1:N0} bytes)" -f $setup, (Get-Item $setup).Length)
}

Get-ChildItem -Path $OutDir -File |
    Where-Object { $_.Name -like "vela-$Version-*" } |
    ForEach-Object {
        $hash = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLower()
        "$hash  $($_.Name)" | Out-File -FilePath "$($_.FullName).sha256" -Encoding ascii
        Write-Host ("{0}  {1}" -f $hash.Substring(0, 16), $_.Name)
    }
