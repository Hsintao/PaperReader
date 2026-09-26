#!/usr/bin/env bash
# PaperReader 一键构建环境配置（macOS / Apple Silicon）。
#
# 用法:
#   ./scripts/setup_build_env.sh            只配置环境
#   ./scripts/setup_build_env.sh --build    配置环境后接着编译 .app + .dmg
#
# 脚本会依次: 校验平台 → 准备 Python 3.11(优先 conda 环境 pt) → 检查 Node.js ≥ 20
# → 安装构建依赖与前端依赖 → 生成 .env → 准备 standalone 翻译运行时
# (desktop/worker-runtime, 不存在时自动下载 python-build-standalone)。
# 已就绪的步骤会自动跳过，可重复执行。
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

BUILD_AFTER_SETUP=0
for arg in "$@"; do
  case "$arg" in
    --build) BUILD_AFTER_SETUP=1 ;;
    -h|--help)
      grep '^#' "$0" | head -n 12
      exit 0 ;;
    *) echo "error: unknown option: $arg (see --help)" >&2; exit 2 ;;
  esac
done

log()  { printf '\n==> %s\n' "$*"; }
fail() { echo "error: $*" >&2; exit 1; }

TMP_DL=""
cleanup() { [[ -n "$TMP_DL" && -d "$TMP_DL" ]] && rm -rf "$TMP_DL" || true; }
trap cleanup EXIT

# --- 1. 平台 -------------------------------------------------------------
[[ "$(uname -s)" == "Darwin" ]] || fail "this script is for macOS"
[[ "$(uname -m)" == "arm64" ]] || fail "macOS packaging requires an Apple Silicon (arm64) Mac"
command -v git >/dev/null || fail "git not found; install Xcode Command Line Tools first"
xcode-select -p >/dev/null 2>&1 || fail "Xcode Command Line Tools missing; run: xcode-select --install"
log "Platform: macOS $(sw_vers -productVersion) arm64"

# --- 2. Python 3.11 ------------------------------------------------------
# 打包要求 Python 3.11（py2app 按解释器版本生成 bundle 目录）。优先复用已有
# 的 conda 环境（pt 或 paperreader-dev），都不是 3.11 时才新建。
log "Resolving Python 3.11"
if command -v conda >/dev/null 2>&1; then
  # shellcheck disable=SC1091
  source "$(conda info --base)/etc/profile.d/conda.sh"
  PYTHON_ENV=""
  for env_name in pt paperreader-dev; do
    if conda env list | awk '{print $1}' | grep -qx "$env_name"; then
      conda activate "$env_name"
      if [[ "$(python -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')" == "3.11" ]]; then
        PYTHON_ENV="$env_name"
        break
      fi
      conda deactivate
    fi
  done
  if [[ -z "$PYTHON_ENV" ]]; then
    NEW_ENV=""
    for env_name in pt paperreader-dev; do
      conda env list | awk '{print $1}' | grep -qx "$env_name" || { NEW_ENV="$env_name"; break; }
    done
    [[ -n "$NEW_ENV" ]] || fail "conda envs 'pt' and 'paperreader-dev' both exist but neither is Python 3.11; recreate one of them with Python 3.11"
    log "No conda env is on Python 3.11; creating env '$NEW_ENV'"
    conda create -n "$NEW_ENV" python=3.11 -y
    conda activate "$NEW_ENV"
  fi
elif command -v python3.11 >/dev/null 2>&1; then
  python3.11 -m venv .venv
  # shellcheck disable=SC1091
  source .venv/bin/activate
elif command -v brew >/dev/null 2>&1; then
  log "Installing python@3.11 via Homebrew"
  brew install python@3.11
  "$(brew --prefix python@3.11)/bin/python3.11" -m venv .venv
  # shellcheck disable=SC1091
  source .venv/bin/activate
else
  fail "no conda, python3.11 or Homebrew found; install Python 3.11 from https://www.python.org or install miniconda, then rerun"
fi
[[ "$(python -c 'import platform; print(platform.machine())')" == "arm64" ]] \
  || fail "active Python is not arm64; macOS packaging requires an arm64 interpreter"
log "Using $(command -v python) ($(python --version 2>&1))"

# --- 3. Node.js ≥ 20 -----------------------------------------------------
log "Resolving Node.js"
if command -v node >/dev/null 2>&1; then
  node_major="$(node -p 'process.versions.node.split(".")[0]')"
  [[ "$node_major" -ge 20 ]] || fail "Node.js $node_major found but >= 20 is required; upgrade from https://nodejs.org"
else
  command -v brew >/dev/null 2>&1 || fail "Node.js not found and no Homebrew to install it; install Node.js 20+ from https://nodejs.org"
  log "Installing Node.js via Homebrew"
  brew install node
  command -v node >/dev/null 2>&1 || export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
  command -v node >/dev/null 2>&1 || fail "node installed by Homebrew is not on PATH; open a new terminal and rerun"
fi
log "Using node $(node --version), npm $(npm --version)"

# --- 4. 构建依赖与前端依赖 ----------------------------------------------
log "Installing Python build dependencies"
python -m pip install --upgrade pip
python -m pip install -r desktop/requirements-build.txt

log "Installing frontend dependencies"
npm --prefix frontend ci

if [[ ! -f .env ]]; then
  cp .env.example .env
  log "Created .env from .env.example — fill in OPENAI_API_KEY before translating papers"
fi

# --- 5. standalone 翻译运行时 --------------------------------------------
WORKER_RUNTIME="$PROJECT_ROOT/desktop/worker-runtime"

worker_runtime_ok() {
  [[ -f "$WORKER_RUNTIME/runtime-manifest.json" ]] || return 1
  grep -q '"runtime_type"[[:space:]]*:[[:space:]]*"python-standalone"' "$WORKER_RUNTIME/runtime-manifest.json" || return 1
  local py="$WORKER_RUNTIME/bin/python3"
  [[ -x "$py" ]] || py="$WORKER_RUNTIME/bin/python"
  [[ -x "$py" ]] || return 1
  "$py" -c 'import pdf2zh_next, babeldoc' >/dev/null 2>&1
}

if worker_runtime_ok; then
  log "Worker runtime already present and valid: desktop/worker-runtime"
else
  [[ -e "$WORKER_RUNTIME" ]] \
    && fail "desktop/worker-runtime exists but is not a valid standalone runtime (need runtime-manifest.json with runtime_type=python-standalone, plus pdf2zh_next/babeldoc). Remove it and rerun."
  log "Preparing standalone Python runtime for the translation worker"
  command -v curl >/dev/null || fail "curl not found"
  TMP_DL="$(mktemp -d)"
  python - "$TMP_DL" <<'PY'
import json, re, sys, urllib.request
from pathlib import Path

dest = Path(sys.argv[1])
triple = "aarch64-apple-darwin"
pattern = re.compile(rf"^cpython-3\.11\.\d+\+\d+-{re.escape(triple)}-install_only\.tar\.(gz|zst)$")

def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "paperreader-setup"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)

candidates = []
release = fetch("https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest")
candidates = [a for a in release.get("assets", []) if pattern.match(a["name"])]
if not candidates:
    for rel in fetch("https://api.github.com/repos/astral-sh/python-build-standalone/releases?per_page=30"):
        candidates = [a for a in rel.get("assets", []) if pattern.match(a["name"])]
        if candidates:
            break
if not candidates:
    sys.exit(f"no cpython 3.11 {triple} asset found in recent python-build-standalone releases")
asset = sorted(candidates, key=lambda a: 0 if a["name"].endswith(".tar.gz") else 1)[0]
print(f"Downloading {asset['name']} ...", flush=True)
req = urllib.request.Request(asset["browser_download_url"], headers={"User-Agent": "paperreader-setup"})
with urllib.request.urlopen(req, timeout=900) as r, open(dest / "runtime.tar", "wb") as f:
    while True:
        chunk = r.read(1 << 20)
        if not chunk:
            break
        f.write(chunk)
print("Download complete.", flush=True)
PY
  tar -xf "$TMP_DL/runtime.tar" -C "$TMP_DL"
  [[ -x "$TMP_DL/python/bin/python3" ]] || fail "unexpected layout in the runtime archive"
  mv "$TMP_DL/python" "$WORKER_RUNTIME"
  WORKER_PY="$WORKER_RUNTIME/bin/python3"
  "$WORKER_PY" -m pip install --upgrade pip
  "$WORKER_PY" -m pip install -r desktop/requirements-worker.txt
  echo '{"runtime_type": "python-standalone"}' > "$WORKER_RUNTIME/runtime-manifest.json"
  "$WORKER_PY" -c 'import pdf2zh_next, babeldoc' \
    || fail "worker runtime self-check failed (pdf2zh_next/babeldoc not importable)"
  log "Worker runtime ready: desktop/worker-runtime"
fi

# Trim BabelDOC's asset set to the CN font family so the offline assets
# package stays small. Idempotent, so it also repairs runtimes prepared
# before this step existed; see desktop/patch_worker_assets.py.
WORKER_PY="$WORKER_RUNTIME/bin/python3"
[[ -x "$WORKER_PY" ]] || WORKER_PY="$WORKER_RUNTIME/bin/python"
"$WORKER_PY" desktop/patch_worker_assets.py "$WORKER_RUNTIME" \
  || fail "worker asset trim failed"

# --- 6. 工具自检 ----------------------------------------------------------
log "Verifying build tooling"
python -c 'import webview, py2app' || fail "pywebview/py2app import failed"

# --- 7. 一键编译 ----------------------------------------------------------
if [[ "$BUILD_AFTER_SETUP" -eq 1 ]]; then
  log "Building the macOS client (.app + .dmg)"
  bash "$PROJECT_ROOT/desktop/build_macos.sh"
  VERSION="$(node -p "require('./frontend/package.json').version")"
  log "Build finished: release/PaperReader-v${VERSION}-macOS-arm64.dmg"
else
  log "Environment ready. Build the client with: ./desktop/build_macos.sh (or rerun this script with --build)"
fi
