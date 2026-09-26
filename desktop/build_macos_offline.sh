#!/usr/bin/env bash
# Offline variant of build_macos.sh: build the trimmed BabelDOC asset package
# first so the DMG ships it (release/offline_assets_*.zip) and a fresh install
# can translate without network. Regenerated on every run — the steps are
# idempotent (patch is a no-op when applied, downloads are hash-verified cache
# hits), so this costs seconds once the asset cache is warm.
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

python3 desktop/make_offline_assets.py
exec bash desktop/build_macos.sh "$@"
