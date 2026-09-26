#!/usr/bin/env python3
"""One-shot offline assets package builder for the translation worker.

Runs the whole chain with the worker runtime's own interpreter:

1. patch the runtime's BabelDOC metadata down to the CN font family
   (idempotent, see desktop/patch_worker_assets.py);
2. download the trimmed asset set into the BabelDOC cache — after the patch
   this is 7 fonts + the layout model + cmaps + tiktoken (~180 MB) instead of
   the full ~330 MB;
3. pack the cache into release/offline_assets_<tag>.zip, which the desktop
   build scripts pick up automatically.

Any Python 3 can launch this script; the real work is re-run under the worker
runtime's interpreter. Usage:

    python desktop/make_offline_assets.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RUNTIME = PROJECT_ROOT / "desktop" / "worker-runtime"
RELEASE_DIR = PROJECT_ROOT / "release"


def worker_python() -> Path:
    for candidate in (
        RUNTIME / "bin" / "python3",
        RUNTIME / "bin" / "python",
        RUNTIME / "python.exe",
        RUNTIME / "Scripts" / "python.exe",
    ):
        if candidate.is_file():
            return candidate
    sys.exit(
        f"no worker runtime interpreter under {RUNTIME} "
        "(run scripts/setup_build_env first)"
    )


def run(python: Path, step: str, args: list[str]) -> None:
    print(f"\n==> {step}", flush=True)
    result = subprocess.run([str(python), *args], cwd=PROJECT_ROOT)
    if result.returncode != 0:
        sys.exit(f"step failed ({result.returncode}): {step}")


def main() -> None:
    python = worker_python()

    run(python, "patch worker runtime assets (CN fonts only)",
        [str(PROJECT_ROOT / "desktop" / "patch_worker_assets.py"), str(RUNTIME)])

    run(python, "download the trimmed asset set",
        ["-c", "from babeldoc.assets.assets import warmup; warmup()"])

    RELEASE_DIR.mkdir(exist_ok=True)
    run(python, "pack release/offline_assets_*.zip",
        ["-c",
         "from pathlib import Path; "
         "from babeldoc.assets.assets import generate_offline_assets_package; "
         f"generate_offline_assets_package(Path({str(RELEASE_DIR)!r}))"])

    packages = sorted(RELEASE_DIR.glob("offline_assets_*.zip"))
    if packages:
        package = packages[-1]
        size_mb = package.stat().st_size / 1024 / 1024
        print(f"\noffline assets package: {package} ({size_mb:.0f} MB)")
        print("the desktop build scripts bundle it automatically")


if __name__ == "__main__":
    main()
