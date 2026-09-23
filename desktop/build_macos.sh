#!/usr/bin/env bash
set -euo pipefail

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

cd "$PROJECT_ROOT/frontend"
npm ci
npm run build

python -c 'from PIL import Image; import sys; Image.open(sys.argv[1]).convert("RGBA").save(sys.argv[2], format="ICNS")' "$ICON_SOURCE" "$ICON_FILE"

cd "$PROJECT_ROOT"
rm -rf "$PROJECT_ROOT/build/bdist.macosx-11.0-arm64" "$PROJECT_ROOT/dist/PaperReader.app"
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
RUNTIME_LIBS=(libffi.8.dylib libbz2.dylib libcrypto.3.dylib libexpat.1.dylib libncursesw.6.dylib libsqlite3.0.dylib libssl.3.dylib libz.1.dylib libicudata.78.dylib libicui18n.78.dylib libicuuc.78.dylib)
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
WORKER_PYTHON="$WORKER_RUNTIME/bin/python3"
if [[ ! -x "$WORKER_PYTHON" ]]; then
  WORKER_PYTHON="$WORKER_RUNTIME/bin/python"
fi
if [[ ! -x "$WORKER_PYTHON" ]]; then
  echo "::error title=invalid worker runtime::no standalone Python interpreter found"
  exit 1
fi
if ! "$WORKER_PYTHON" -c 'import pdf2zh_next, babeldoc'; then
  echo "::error title=invalid worker runtime::the standalone runtime cannot import pdf2zh_next and babeldoc"
  exit 1
fi
rm -rf "$PROJECT_ROOT/dist/PaperReader.app/Contents/Resources/worker-runtime"
cp -R "$WORKER_RUNTIME" "$PROJECT_ROOT/dist/PaperReader.app/Contents/Resources/worker-runtime"
echo "Bundled standalone worker runtime: $(du -sh "$PROJECT_ROOT/dist/PaperReader.app/Contents/Resources/worker-runtime" | cut -f1)"
codesign --force --deep --sign - "$PROJECT_ROOT/dist/PaperReader.app"

mkdir -p "$PROJECT_ROOT/release"
DMG="$PROJECT_ROOT/release/PaperReader-v${VERSION}-macOS-arm64.dmg"
rm -f "$DMG"
hdiutil create -volname PaperReader -srcfolder "$PROJECT_ROOT/dist/PaperReader.app" -ov -format UDZO "$DMG"
shasum -a 256 "$DMG" > "$DMG.sha256"
echo "macOS package: $DMG"
