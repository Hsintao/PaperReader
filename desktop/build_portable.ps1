param(
    [string]$NpmPath = "",
    [string]$PythonPath = ""
)

$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Version = (Get-Content (Join-Path $ProjectRoot "frontend\package.json") -Raw | ConvertFrom-Json).version

# A portable app still running from a previous build keeps files in
# dist\PaperReader locked, which breaks both PyInstaller COLLECT and
# zip creation ("...正由另一进程使用").
Get-Process -Name "PaperReader" -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Milliseconds 500

if (-not $NpmPath) {
    $NpmCommand = Get-Command npm.cmd -ErrorAction SilentlyContinue
    if (-not $NpmCommand) {
        $NpmCommand = Get-Command npm -ErrorAction SilentlyContinue
    }
    if (-not $NpmCommand) {
        throw "npm was not found. Install Node.js or pass -NpmPath explicitly."
    }
    $NpmPath = $NpmCommand.Source
}

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
# The backend uses 3.10+ syntax (`str | None`); freezing with an older Python
# produces a package that crashes on startup.
& $PythonPath -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"
if ($LASTEXITCODE -ne 0) {
    throw "$PythonPath is older than Python 3.10. Run scripts\setup_build_env.ps1 or pass -PythonPath to a 3.10+ interpreter."
}
# PyInstaller only logs a warning for modules it cannot import at build time,
# and the frozen app then crashes at startup (e.g. "No module named
# 'webview'"); verify the build interpreter has them before freezing.
& $PythonPath -c "import webview, PyInstaller, pythonnet, clr_loader"
if ($LASTEXITCODE -ne 0) {
    throw "$PythonPath is missing build dependencies (pywebview/PyInstaller/pythonnet/clr-loader). Run scripts\setup_build_env.ps1 first, or pass -PythonPath to the prepared build interpreter."
}

Push-Location (Join-Path $ProjectRoot "frontend")
try {
    & $NpmPath ci --prefer-offline --no-audit --no-fund
    if ($LASTEXITCODE -ne 0) { throw "npm ci failed" }
    & $NpmPath run build
    if ($LASTEXITCODE -ne 0) { throw "Frontend build failed" }
} finally {
    Pop-Location
}

& $PythonPath -m PyInstaller --clean --noconfirm --distpath (Join-Path $ProjectRoot "dist") --workpath (Join-Path $ProjectRoot "build") (Join-Path $ProjectRoot "desktop\PaperReader.spec")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed" }

$PortableDir = Join-Path $ProjectRoot "dist\PaperReader"
# Write with a UTF-8 BOM: without one, Notepad on Chinese Windows reads the
# file as GBK and shows mojibake.
$ReadmeText = [IO.File]::ReadAllText((Join-Path $ProjectRoot "desktop\README_zh.md"), [Text.UTF8Encoding]::new($false))
[IO.File]::WriteAllText((Join-Path $PortableDir "README.txt"), $ReadmeText, [Text.UTF8Encoding]::new($true))
Copy-Item -LiteralPath (Join-Path $ProjectRoot "desktop\create_shortcut.ps1") -Destination $PortableDir -Force

# The PDF translation worker's dependencies stay out of the frozen app; a
# prepared runtime is copied beside it so the launcher finds it automatically.
$WorkerRuntime = Join-Path $ProjectRoot "desktop\worker-runtime"
if (-not (Test-Path -LiteralPath $WorkerRuntime)) {
    throw "desktop\worker-runtime is required for a portable release. Prepare a standalone Python runtime first."
}
if (Test-Path -LiteralPath (Join-Path $WorkerRuntime "pyvenv.cfg")) {
    throw "desktop\worker-runtime is a venv, not a portable Python runtime. Use a standalone distribution."
}
$WorkerManifest = Join-Path $WorkerRuntime "runtime-manifest.json"
if (-not (Test-Path -LiteralPath $WorkerManifest)) {
    throw "desktop\worker-runtime/runtime-manifest.json is missing. The manifest must identify a standalone runtime."
}
$WorkerMetadata = Get-Content -LiteralPath $WorkerManifest -Raw | ConvertFrom-Json
if ($WorkerMetadata.runtime_type -ne "python-standalone") {
    throw "desktop\worker-runtime/runtime-manifest.json must declare runtime_type=python-standalone."
}
$WorkerPython = Join-Path $WorkerRuntime "python.exe"
if (-not (Test-Path -LiteralPath $WorkerPython)) {
    $WorkerPython = Join-Path $WorkerRuntime "Scripts\python.exe"
}
if (-not (Test-Path -LiteralPath $WorkerPython)) {
    throw "desktop\worker-runtime does not contain python.exe."
}
& $WorkerPython -c "import pdf2zh_next, babeldoc"
if ($LASTEXITCODE -ne 0) { throw "The standalone worker runtime cannot import pdf2zh_next and babeldoc." }
$Target = Join-Path $PortableDir "worker-runtime"
if (Test-Path -LiteralPath $Target) { Remove-Item -LiteralPath $Target -Recurse -Force }
# robocopy copies the 1+ GB runtime multithreaded; exit codes below 8 are success.
robocopy "$WorkerRuntime" "$Target" /E /MT:16 /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy failed with exit code $LASTEXITCODE" }
$global:LASTEXITCODE = 0
Write-Host "Bundled standalone worker runtime"

# Slim the bundled copy. The worker only ever runs `python -m
# workers.pdfmathtranslate`, so this drops what that entry point never loads:
# pdf2zh_next's gradio GUI (which also pulls in pandas), pip and the console
# scripts, debug symbols, bytecode caches, tkinter, the OpenCV video backend,
# compiler headers/import libraries, and test suites shipped inside packages.
$SitePackages = Join-Path $Target "Lib\site-packages"
foreach ($Name in @("gradio", "gradio_client", "gradio_pdf", "pandas", "pip")) {
    Get-ChildItem -Path $SitePackages -Directory -Filter "$Name*" |
        Where-Object { $_.Name -eq $Name -or $_.Name -like "$Name-*.dist-info" } |
        Remove-Item -Recurse -Force
}
foreach ($Rel in @("Scripts", "tcl", "include", "libs", "share",
        "Lib\tkinter", "Lib\turtledemo", "Lib\turtle.py",
        "DLLs\_tkinter.pyd", "DLLs\tcl86t.dll", "DLLs\tk86t.dll")) {
    Remove-Item -LiteralPath (Join-Path $Target $Rel) -Recurse -Force -ErrorAction SilentlyContinue
}
Get-ChildItem -Path (Join-Path $SitePackages "cv2") -Filter "opencv_videoio_ffmpeg*.dll" | Remove-Item -Force
# One recursive walk instead of three: .pdb files and __pycache__ anywhere,
# tests/test directories only inside site-packages (Lib\test is the stdlib
# test suite and stays).
Get-ChildItem -Path $Target -Recurse -Force |
    Where-Object {
        if ($_.PSIsContainer) {
            $_.Name -eq "__pycache__" -or
            ($_.FullName.StartsWith($SitePackages) -and $_.Name -in @("tests", "test"))
        } else {
            $_.Extension -eq ".pdb"
        }
    } |
    Sort-Object { $_.FullName.Length } -Descending |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
Write-Host "Pruned bundled worker runtime"

# The worker's fonts and layout model otherwise download on first use; the
# offline package (release/offline_assets_*.zip, generated by `babeldoc
# --generate-offline-assets`) lets a fresh install translate offline. The
# backend restores it on startup; absent the zip the build is still valid and
# first use downloads as before.
$OfflineZip = Get-ChildItem -Path (Join-Path $ProjectRoot "release") -Filter "offline_assets_*.zip" -ErrorAction SilentlyContinue | Select-Object -First 1
if ($OfflineZip) {
    $OfflineDir = Join-Path $PortableDir "offline_assets"
    New-Item -ItemType Directory -Force -Path $OfflineDir | Out-Null
    Copy-Item -LiteralPath $OfflineZip.FullName -Destination $OfflineDir -Force
    Write-Host "Bundled offline translation assets"
} else {
    Write-Host "no release\offline_assets_*.zip found; first translation will download fonts and models"
}

$ReleaseDir = Join-Path $ProjectRoot "release"
New-Item -ItemType Directory -Force -Path $ReleaseDir | Out-Null
$ZipPath = Join-Path $ReleaseDir "PaperReader-v$Version-Windows-x64.zip"
# ZipFile.CreateFromDirectory is ~8x faster than Compress-Archive at the same
# compression level; the zips are byte-comparable.
Add-Type -AssemblyName System.IO.Compression.FileSystem
for ($Attempt = 1; $Attempt -le 3; $Attempt++) {
    try {
        if (Test-Path -LiteralPath $ZipPath) {
            Remove-Item -LiteralPath $ZipPath -Force
        }
        [System.IO.Compression.ZipFile]::CreateFromDirectory(
            $PortableDir, $ZipPath,
            [System.IO.Compression.CompressionLevel]::Fastest, $false)
        break
    }
    catch {
        if ($Attempt -ge 3) { throw }
        # Freshly written EXEs/DLLs can be held briefly by antivirus or the
        # shell; give the handle a moment to clear and try again.
        Start-Sleep -Seconds 5
    }
}
$Hash = (Get-FileHash -LiteralPath $ZipPath -Algorithm SHA256).Hash.ToLower()
"$Hash  $([IO.Path]::GetFileName($ZipPath))" | Set-Content -Encoding ascii -Path "$ZipPath.sha256"
Write-Host "Portable package: $ZipPath"
