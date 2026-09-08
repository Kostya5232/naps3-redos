param(
    [string]$ApplicationDirectory = "",
    [string]$OutputDirectory = ""
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($ApplicationDirectory)) {
    $ApplicationDirectory = Join-Path $root "dist\windows\NAPS3"
}
if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
    $OutputDirectory = Join-Path $root "dist\windows\release"
}

$source = Get-Content -LiteralPath (Join-Path $root "naps3.py") -Raw
$match = [regex]::Match($source, '(?m)^APP_VERSION = "([^"]+)"')
if (-not $match.Success) {
    throw "Не удалось определить версию NAPS3."
}
$version = $match.Groups[1].Value
$fileVersion = "$version.0.0"

if (-not (Test-Path -LiteralPath (Join-Path $ApplicationDirectory "NAPS3.exe") -PathType Leaf)) {
    throw "Не найдена собранная программа: $ApplicationDirectory"
}

$makensisCandidates = @(
    (Get-Command "makensis.exe" -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -ErrorAction SilentlyContinue),
    (Join-Path ${env:ProgramFiles(x86)} "NSIS\makensis.exe"),
    (Join-Path $env:ProgramFiles "NSIS\makensis.exe")
) | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) }
if ($makensisCandidates.Count -eq 0) {
    throw "Не найден NSIS (makensis.exe)."
}
$makensis = $makensisCandidates[0]

$null = New-Item -ItemType Directory -Path $OutputDirectory -Force
$installer = Join-Path $OutputDirectory "NAPS3-$version-Windows-x64.exe"
$portable = Join-Path $OutputDirectory "NAPS3-$version-Windows-x64-portable.zip"
$checksums = Join-Path $OutputDirectory "SHA256SUMS.windows"

Remove-Item -LiteralPath $installer, $portable, $checksums -Force -ErrorAction SilentlyContinue

& $makensis "/INPUTCHARSET" "UTF8" "/DAPP_VERSION=$version" "/DAPP_FILE_VERSION=$fileVersion" "/DSOURCE_DIR=$ApplicationDirectory" "/DOUTPUT_FILE=$installer" (Join-Path $PSScriptRoot "installer.nsi")
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $installer -PathType Leaf)) {
    throw "NSIS не создал установщик."
}

Compress-Archive -LiteralPath $ApplicationDirectory -DestinationPath $portable -CompressionLevel Optimal
$lines = foreach ($file in @($installer, $portable)) {
    $hash = (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLowerInvariant()
    "$hash  $([IO.Path]::GetFileName($file))"
}
[IO.File]::WriteAllLines($checksums, $lines, [Text.UTF8Encoding]::new($false))

Write-Host "Готово: $installer"
Write-Host "Готово: $portable"
