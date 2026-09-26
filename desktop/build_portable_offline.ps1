# Offline variant of build_portable.ps1: build the trimmed BabelDOC asset
# package first so the portable zip ships it (release\offline_assets_*.zip)
# and a fresh install can translate without network. Regenerated on every
# run — the steps are idempotent (patch is a no-op when applied, downloads
# are hash-verified cache hits), so this costs seconds once the asset cache
# is warm. Parameters are passed through to build_portable.ps1.
param(
    [string]$NpmPath = "",
    [string]$PythonPath = ""
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

if (-not $PythonPath) {
    $PythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    if (-not $PythonCommand) {
        $PythonCommand = Get-Command python -ErrorAction SilentlyContinue
    }
    if (-not $PythonCommand) {
        throw "Python was not found. Install Python or pass -PythonPath explicitly."
    }
    $PythonPath = $PythonCommand.Source
}

& $PythonPath (Join-Path $ProjectRoot "desktop\make_offline_assets.py")
if ($LASTEXITCODE -ne 0) { throw "offline assets build failed" }

& (Join-Path $ProjectRoot "desktop\build_portable.ps1") @PSBoundParameters
if ($LASTEXITCODE -ne 0) { throw "portable build failed" }
