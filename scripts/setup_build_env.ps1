# PaperReader 一键构建环境配置（Windows x64）。
#
# 用法:
#   powershell -ExecutionPolicy Bypass -File .\scripts\setup_build_env.ps1            只配置环境
#   powershell -ExecutionPolicy Bypass -File .\scripts\setup_build_env.ps1 -Build     配置环境后接着编译可移植 ZIP
#
# 脚本会依次: 校验平台 → 准备 Python 3.11(优先 py 启动器, 缺失时尝试 winget) → 检查 Node.js ≥ 20
# → 安装构建依赖与前端依赖 → 生成 .env → 准备 standalone 翻译运行时
# (desktop\worker-runtime, 不存在时自动下载 python-build-standalone)。
# 已就绪的步骤会自动跳过，可重复执行。
[CmdletBinding()]
param(
    [switch]$Build
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $ProjectRoot

function Log($msg) { Write-Host "`n==> $msg" }
function Fail($msg) { Write-Host "error: $msg" -ForegroundColor Red; exit 1 }

function Refresh-Path {
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")
}

function Resolve-Python311 {
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($launcher) {
        $out = & $launcher.Source -3.11 -c "import sys; print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $out) { return $out.Trim() }
    }
    $pythonCmd = Get-Command python.exe -ErrorAction SilentlyContinue
    if (-not $pythonCmd) { $pythonCmd = Get-Command python -ErrorAction SilentlyContinue }
    if ($pythonCmd) {
        $ver = & $pythonCmd.Source --version 2>&1
        if ("$ver" -match "^Python 3\.11\.") { return $pythonCmd.Source }
    }
    return $null
}

function Resolve-Npm {
    $npm = Get-Command npm.cmd -ErrorAction SilentlyContinue
    if (-not $npm) { $npm = Get-Command npm -ErrorAction SilentlyContinue }
    if (-not $npm) { return $null }
    $node = Get-Command node.exe -ErrorAction SilentlyContinue
    if (-not $node) { $node = Get-Command node -ErrorAction SilentlyContinue }
    if (-not $node) { return $null }
    $major = [int](& $node.Source -p "process.versions.node.split('.')[0]")
    if ($major -lt 20) { Fail "Node.js major $major found but >= 20 is required; upgrade from https://nodejs.org" }
    return $npm.Source
}

# --- 1. 平台 -------------------------------------------------------------
if (-not [System.Environment]::Is64BitOperatingSystem) { Fail "Windows x64 is required." }
Log "Platform: Windows x64"

# --- 2. Python 3.11 ------------------------------------------------------
Log "Resolving Python 3.11"
$PythonPath = Resolve-Python311
if (-not $PythonPath) {
    $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if ($winget) {
        Log "Installing Python 3.11 via winget"
        & $winget.Source install --id Python.Python.3.11 -e --accept-source-agreements --accept-package-agreements
        Refresh-Path
        $PythonPath = Resolve-Python311
    }
}
if (-not $PythonPath) {
    Fail "Python 3.11 not found; install it from https://www.python.org (tick 'Add python.exe to PATH') or install winget, then rerun"
}
Log "Using $PythonPath"
& $PythonPath --version

# --- 3. Node.js ≥ 20 -----------------------------------------------------
Log "Resolving Node.js"
$NpmPath = Resolve-Npm
if (-not $NpmPath) {
    $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if ($winget) {
        Log "Installing Node.js LTS via winget"
        & $winget.Source install --id OpenJS.NodeJS.LTS -e --accept-source-agreements --accept-package-agreements
        Refresh-Path
        $NpmPath = Resolve-Npm
    }
}
if (-not $NpmPath) { Fail "npm not found; install Node.js 20+ from https://nodejs.org" }
Log "Using npm at $NpmPath"

# --- 4. 构建依赖与前端依赖 ----------------------------------------------
Log "Installing Python build dependencies"
& $PythonPath -m pip install --upgrade pip
& $PythonPath -m pip install -r desktop/requirements-build.txt
if ($LASTEXITCODE -ne 0) { Fail "pip install failed" }

Log "Installing frontend dependencies"
& $NpmPath --prefix frontend ci
if ($LASTEXITCODE -ne 0) { Fail "npm ci failed" }

if (-not (Test-Path .env)) {
    Copy-Item .env.example .env
    Log "Created .env from .env.example — fill in OPENAI_API_KEY before translating papers"
}

# --- 5. standalone 翻译运行时 --------------------------------------------
$WorkerRuntime = Join-Path $ProjectRoot "desktop\worker-runtime"

function Test-WorkerRuntime([string]$root) {
    $manifestPath = Join-Path $root "runtime-manifest.json"
    if (-not (Test-Path -LiteralPath $manifestPath)) { return $false }
    try { $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json } catch { return $false }
    if ($manifest.runtime_type -ne "python-standalone") { return $false }
    $py = Join-Path $root "python.exe"
    if (-not (Test-Path -LiteralPath $py)) { return $false }
    & $py -c "import pdf2zh_next, babeldoc" 2>$null | Out-Null
    return ($LASTEXITCODE -eq 0)
}

if (Test-WorkerRuntime $WorkerRuntime) {
    Log "Worker runtime already present and valid: desktop\worker-runtime"
} else {
    if (Test-Path -LiteralPath $WorkerRuntime) {
        Fail "desktop\worker-runtime exists but is not a valid standalone runtime (need runtime-manifest.json with runtime_type=python-standalone, plus pdf2zh_next/babeldoc). Remove it and rerun."
    }
    Log "Preparing standalone Python runtime for the translation worker"
    $tmp = Join-Path ([IO.Path]::GetTempPath()) ("paperreader-setup-" + [Guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Force -Path $tmp | Out-Null
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        $triple = "x86_64-pc-windows-msvc"
        $pattern = "^cpython-3\.11\.\d+\+\d+-" + [regex]::Escape($triple) + "-install_only\.tar\.(gz|zst)$"
        $headers = @{ "User-Agent" = "paperreader-setup" }
        $release = Invoke-RestMethod -Uri "https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest" -Headers $headers -TimeoutSec 60
        $assets = @($release.assets | Where-Object { $_.name -match $pattern })
        if (-not $assets) {
            $releases = Invoke-RestMethod -Uri "https://api.github.com/repos/astral-sh/python-build-standalone/releases?per_page=30" -Headers $headers -TimeoutSec 60
            foreach ($rel in $releases) {
                $assets = @($rel.assets | Where-Object { $_.name -match $pattern })
                if ($assets) { break }
            }
        }
        if (-not $assets) { Fail "no cpython 3.11 $triple asset found in recent python-build-standalone releases" }
        $asset = $assets | Sort-Object { if ($_.name -like "*.tar.gz") { 0 } else { 1 } } | Select-Object -First 1
        Log "Downloading $($asset.name) ..."
        $archive = Join-Path $tmp "runtime.tar"
        Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $archive -Headers $headers -TimeoutSec 900
        & tar -xf $archive -C $tmp
        if ($LASTEXITCODE -ne 0) { Fail "failed to extract the runtime archive" }
        $extracted = Join-Path $tmp "python"
        if (-not (Test-Path -LiteralPath (Join-Path $extracted "python.exe"))) { Fail "unexpected layout in the runtime archive" }
        Move-Item -LiteralPath $extracted -Destination $WorkerRuntime
        $WorkerPy = Join-Path $WorkerRuntime "python.exe"
        & $WorkerPy -m pip install --upgrade pip
        & $WorkerPy -m pip install -r desktop/requirements-worker.txt
        if ($LASTEXITCODE -ne 0) { Fail "pip install into the worker runtime failed" }
        '{"runtime_type": "python-standalone"}' | Set-Content -Encoding ascii -Path (Join-Path $WorkerRuntime "runtime-manifest.json")
        & $WorkerPy -c "import pdf2zh_next, babeldoc"
        if ($LASTEXITCODE -ne 0) { Fail "worker runtime self-check failed (pdf2zh_next/babeldoc not importable)" }
        Log "Worker runtime ready: desktop\worker-runtime"
    } finally {
        Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
    }
}

# --- 6. 工具自检 ----------------------------------------------------------
Log "Verifying build tooling"
& $PythonPath -c "import webview, PyInstaller, pythonnet, clr_loader"
if ($LASTEXITCODE -ne 0) { Fail "pywebview/PyInstaller/pythonnet import failed" }

# --- 7. 一键编译 ----------------------------------------------------------
if ($Build) {
    Log "Building the Windows client (portable ZIP)"
    & (Join-Path $ProjectRoot "desktop\build_portable.ps1") -PythonPath $PythonPath -NpmPath $NpmPath
    if ($LASTEXITCODE -ne 0) { Fail "build failed" }
    $version = (Get-Content -LiteralPath (Join-Path $ProjectRoot "frontend\package.json") -Raw | ConvertFrom-Json).version
    Log "Build finished: release\PaperReader-v$version-Windows-x64.zip"
} else {
    Log "Environment ready. Build the client with: powershell -ExecutionPolicy Bypass -File .\desktop\build_portable.ps1 (or rerun this script with -Build)"
}
