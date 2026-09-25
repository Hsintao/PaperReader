#!/usr/bin/env bash
set -euo pipefail

FAST=0
for arg in "$@"; do
  case "$arg" in
    --fast) FAST=1 ;;
    -h|--help)
      echo "usage: $0 [--fast]"
      echo "  --fast  build and sign the app but skip DMG packaging"
      exit 0 ;;
    *) echo "error: unknown option: $arg (see --help)" >&2; exit 2 ;;
  esac
done

report_failure() {
  local exit_code="$1"
  local line_number="$2"
  local failed_command="$3"
  failed_command="${failed_command//'%'/'%25'}"
  failed_command="${failed_command//$'\r'/'%0D'}"
  failed_command="${failed_command//$'\n'/'%0A'}"
  echo "::error title=macOS packaging failed::line ${line_number}: ${failed_command} (exit ${exit_code})"
}
trap 'report_failure "$?" "$LINENO" "$BASH_COMMAND"' ERR

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="$(cd "$PROJECT_ROOT" && node -p "require('./frontend/package.json').version")"
ICON_SOURCE="$PROJECT_ROOT/desktop/assets/PaperReader-icon-source.png"
ICON_FILE="$PROJECT_ROOT/desktop/assets/PaperReader.icns"

STEP_AT=$SECONDS
step() { printf '\n==> %s\n' "$*"; STEP_AT=$SECONDS; }
step_done() { printf '    done in %ds\n' "$((SECONDS - STEP_AT))"; }

# cp -c uses APFS clonefile: the "copy" shares storage with the source, so
# moving the ~1 GB worker runtime costs seconds instead of minutes.
clone_copy() {
  local source="$1"
  local destination="$2"
  if ! cp -Rc "$source" "$destination" 2>/dev/null; then
    rm -rf "$destination"
    cp -R "$source" "$destination"
  fi
}

runtime_python() {
  local runtime="$1"
  if [[ -x "$runtime/bin/python3" ]]; then
    echo "$runtime/bin/python3"
  else
    echo "$runtime/bin/python"
  fi
}

# Slim a bundled worker-runtime copy. The worker only ever runs `python -m
# workers.pdfmathtranslate`, so this drops what that entry point never loads:
# pdf2zh_next's gradio GUI (which also pulls in pandas), pip and the console
# scripts, Tcl/Tk and tkinter, C headers and pkg-config files, bytecode
# caches, and test suites shipped inside packages. Mirrors the prune step in
# desktop/build_portable.ps1.
prune_worker_runtime() {
  local runtime="$1"
  local stdlib
  stdlib="$(ls -d "$runtime"/lib/python3.* | head -n 1)"
  if [[ ! -d "$stdlib/site-packages" ]]; then
    echo "::error title=unexpected bundle layout::no site-packages under $stdlib"
    exit 1
  fi
  local sp="$stdlib/site-packages"

  local name
  for name in gradio gradio_client gradio_pdf gradio_i18n pandas pip; do
    rm -rf "$sp/$name" "$sp/${name}-"*.dist-info
  done

  # Console scripts; keep only the python interpreters themselves.
  find "$runtime/bin" -mindepth 1 -maxdepth 1 ! -name 'python*' -exec rm -rf {} +

  # Tcl/Tk (used only by tkinter), development files, IDLE.
  rm -rf "$runtime"/lib/tcl* "$runtime"/lib/tk* "$runtime"/lib/itcl* \
         "$runtime"/lib/thread* "$runtime"/lib/libtcl* \
         "$runtime"/lib/pkgconfig "$runtime/include" "$runtime/share" \
         "$stdlib/tkinter" "$stdlib/idlelib" "$stdlib/turtledemo" "$stdlib/turtle.py"
  rm -f "$stdlib"/lib-dynload/_tkinter*.so

  # Bytecode caches and package test suites.
  find "$runtime" -type d -name '__pycache__' -prune -exec rm -rf {} +
  find "$sp" -depth -type d \( -name tests -o -name test \) -exec rm -rf {} +
}

step "Frontend install and build"
cd "$PROJECT_ROOT/frontend"
# npm ci wipes and reinstalls node_modules; skip it when the installed tree
# already matches the lockfile (the stamp is written after each install).
LOCK_SHA="$(shasum -a 256 package-lock.json | cut -d' ' -f1)"
LOCK_STAMP="node_modules/.paperreader-lock-sha256"
if [[ -d node_modules && -f "$LOCK_STAMP" && "$(cat "$LOCK_STAMP")" == "$LOCK_SHA" ]]; then
  echo "node_modules matches package-lock.json, skipping npm ci"
else
  npm ci
  echo "$LOCK_SHA" > "$LOCK_STAMP"
fi
npm run build
step_done

python -c 'from PIL import Image; import sys; Image.open(sys.argv[1]).convert("RGBA").save(sys.argv[2], format="ICNS")' "$ICON_SOURCE" "$ICON_FILE"

step "py2app bundle"
cd "$PROJECT_ROOT"
rm -rf "$PROJECT_ROOT"/build/bdist.macosx-* "$PROJECT_ROOT/dist/PaperReader.app"
mkdir -p "$PROJECT_ROOT/build"
PY2APP_LOG="$PROJECT_ROOT/build/macos-py2app.log"
if ! python desktop/setup_macos.py py2app >"$PY2APP_LOG" 2>&1; then
  tail -120 "$PY2APP_LOG"
  PY2APP_ERROR="$(tail -8 "$PY2APP_LOG")"
  PY2APP_ERROR="${PY2APP_ERROR//'%'/'%25'}"
  PY2APP_ERROR="${PY2APP_ERROR//$'\r'/'%0D'}"
  PY2APP_ERROR="${PY2APP_ERROR//$'\n'/'%0A'}"
  echo "::error title=py2app failed::${PY2APP_ERROR}"
  exit 1
fi
step_done
PYTHON_SITE="$(python -c 'import site; print(site.getsitepackages()[0])')"
PYTHON_PREFIX="$(python -c 'import sys; print(sys.prefix)')"
# Derive the version directory from the interpreter that ran py2app. py2app
# names it after its own Python, so a hard-coded version silently copies the
# runtime libraries nowhere and the app dies at launch with a dlopen error.
PYTHON_VERSION="$(python -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
PYTHON_LIB="$PROJECT_ROOT/dist/PaperReader.app/Contents/Resources/lib/python${PYTHON_VERSION}"
FRAMEWORKS="$PROJECT_ROOT/dist/PaperReader.app/Contents/Frameworks"
if [[ ! -d "$PYTHON_LIB" ]]; then
  echo "::error title=unexpected bundle layout::py2app produced no $PYTHON_LIB (python ${PYTHON_VERSION})"
  exit 1
fi
RUNTIME_PACKAGES=(pypdfium2 pypdfium2_raw)
for RUNTIME_PACKAGE in "${RUNTIME_PACKAGES[@]}"; do
  cp -R "$PYTHON_SITE/$RUNTIME_PACKAGE" "$PYTHON_LIB/$RUNTIME_PACKAGE"
done
RUNTIME_LIBS=(libffi.8.dylib libbz2.dylib libcrypto.3.dylib libexpat.1.dylib libncursesw.6.dylib libsqlite3.0.dylib libsqlite3.dylib libssl.3.dylib libz.1.dylib libicudata.78.dylib libicui18n.78.dylib libicuuc.78.dylib)
for RUNTIME_LIB in "${RUNTIME_LIBS[@]}"; do
  if [[ -f "$PYTHON_PREFIX/lib/$RUNTIME_LIB" ]]; then
    cp "$PYTHON_PREFIX/lib/$RUNTIME_LIB" "$FRAMEWORKS/$RUNTIME_LIB"
  fi
done
# A missing runtime library only surfaces as an opaque "Launch error" dialog,
# so fail the build here instead of shipping an app that cannot start.
MISSING_LIBS=()
for RUNTIME_LIB in "${RUNTIME_LIBS[@]}"; do
  if [[ -f "$PYTHON_PREFIX/lib/$RUNTIME_LIB" && ! -f "$FRAMEWORKS/$RUNTIME_LIB" ]]; then
    MISSING_LIBS+=("$RUNTIME_LIB")
  fi
done
if (( ${#MISSING_LIBS[@]} )); then
  echo "::error title=missing runtime libraries::${MISSING_LIBS[*]} were not copied into Contents/Frameworks"
  exit 1
fi
# The PDF translation worker's dependencies are kept out of the app bundle: a
# prepared runtime is copied in whole so the launcher can find it at
# Contents/Resources/worker-runtime. Copied before signing so the signature
# covers it.
step "Bundle worker runtime"
WORKER_RUNTIME="$PROJECT_ROOT/desktop/worker-runtime"
if [[ ! -d "$WORKER_RUNTIME" ]]; then
  echo "::error title=no worker runtime::desktop/worker-runtime is required for a portable release"
  exit 1
fi
if [[ -e "$WORKER_RUNTIME/pyvenv.cfg" ]]; then
  echo "::error title=non-portable worker runtime::desktop/worker-runtime is a venv; use a standalone Python distribution"
  exit 1
fi
if [[ ! -f "$WORKER_RUNTIME/runtime-manifest.json" ]] || ! grep -q '"runtime_type"[[:space:]]*:[[:space:]]*"python-standalone"' "$WORKER_RUNTIME/runtime-manifest.json"; then
  echo "::error title=invalid worker runtime::runtime-manifest.json must declare runtime_type=python-standalone"
  exit 1
fi
WORKER_PYTHON="$(runtime_python "$WORKER_RUNTIME")"
if [[ ! -x "$WORKER_PYTHON" ]]; then
  echo "::error title=invalid worker runtime::no standalone Python interpreter found"
  exit 1
fi

# Pruning scans the whole runtime tree (~34k files), so prune once into a
# cache and clone the pruned tree into the app. The cache is rebuilt only
# when some file in desktop/worker-runtime is newer than the stamp.
SLIM_CACHE="$PROJECT_ROOT/build/cache/worker-runtime-slim"
SLIM_STAMP="$PROJECT_ROOT/build/cache/worker-runtime-slim.stamp"
CACHE_STALE=0
if [[ ! -d "$SLIM_CACHE" || ! -f "$SLIM_STAMP" ]]; then
  CACHE_STALE=1
elif [[ -n "$(find "$WORKER_RUNTIME" -newer "$SLIM_STAMP" -print -quit)" ]]; then
  CACHE_STALE=1
fi
if (( CACHE_STALE )); then
  if ! "$WORKER_PYTHON" -c 'import pdf2zh_next, babeldoc'; then
    echo "::error title=invalid worker runtime::the standalone runtime cannot import pdf2zh_next and babeldoc"
    exit 1
  fi
  echo "Pruning worker runtime into cache (reruns only when the runtime changes)"
  rm -rf "$SLIM_CACHE"
  mkdir -p "$(dirname "$SLIM_CACHE")"
  clone_copy "$WORKER_RUNTIME" "$SLIM_CACHE"
  prune_worker_runtime "$SLIM_CACHE"
  # A pruned runtime must still import the translator; fail here instead of
  # shipping an app whose worker dies on first use.
  SLIM_PYTHON="$(runtime_python "$SLIM_CACHE")"
  if [[ ! -x "$SLIM_PYTHON" ]] || ! "$SLIM_PYTHON" -c 'import pdf2zh_next, babeldoc'; then
    echo "::error title=pruned worker runtime broken::pdf2zh_next/babeldoc no longer importable after pruning"
    exit 1
  fi
  touch "$SLIM_STAMP"
fi
BUNDLED_RUNTIME="$PROJECT_ROOT/dist/PaperReader.app/Contents/Resources/worker-runtime"
rm -rf "$BUNDLED_RUNTIME"
clone_copy "$SLIM_CACHE" "$BUNDLED_RUNTIME"
echo "Bundled pruned worker runtime: $(du -sh "$BUNDLED_RUNTIME" | cut -f1)"
step_done

step "codesign"
codesign --force --deep --sign - "$PROJECT_ROOT/dist/PaperReader.app"
step_done

if (( FAST )); then
  echo "fast mode: skipping DMG packaging"
else
  step "DMG package"
  mkdir -p "$PROJECT_ROOT/release"
  DMG="$PROJECT_ROOT/release/PaperReader-v${VERSION}-macOS-arm64.dmg"
  rm -f "$DMG"
  # hdiutil create is deprecated since macOS 26; diskutil image create is its
  # replacement but does not exist on older releases (CI runs macos-15).
  # Unlike hdiutil -srcfolder, diskutil images the source folder's *contents*,
  # so the app is staged in a parent directory to keep PaperReader.app at the
  # volume root.
  if diskutil image create from --help >/dev/null 2>&1; then
    STAGING="$PROJECT_ROOT/build/dmg-staging"
    rm -rf "$STAGING"
    mkdir -p "$STAGING"
    clone_copy "$PROJECT_ROOT/dist/PaperReader.app" "$STAGING/PaperReader.app"
    diskutil image create from --volumeName PaperReader --format ULMO "$STAGING" "$DMG"
    rm -rf "$STAGING"
  else
    hdiutil create -volname PaperReader -srcfolder "$PROJECT_ROOT/dist/PaperReader.app" -ov -format ULMO "$DMG"
  fi
  shasum -a 256 "$DMG" > "$DMG.sha256"
  echo "macOS package: $DMG"
  step_done
fi

echo "macOS build finished in ${SECONDS}s"
