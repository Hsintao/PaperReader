#!/usr/bin/env python3
"""Trim a prepared worker runtime's BabelDOC asset set to the CN font family.

PaperReader translates English to Chinese only, so the JP/KR/TW/HK/EN font
families (~154 MB) are dead weight in the offline assets package. This rewrites
``babeldoc/assets/embedding_assets_metadata.py`` inside a prepared
``desktop/worker-runtime`` so the CN family is the only one referenced; the
module's own cleanup then drops the other fonts from EMBEDDING_FONT_METADATA,
and ``babeldoc --generate-offline-assets`` produces the trimmed package.

``get_font_family`` is pinned to the CN family as well: the stock lookup
matches the target language code against substrings ("CN", "TW", ...), which
PaperReader's ``lang_out="zh"`` never satisfies, so it fell through to the EN
family and only rendered correctly thanks to the cross-family fallback this
patch removes.

Idempotent, and fails loudly when the pinned babeldoc source no longer matches
the blocks being replaced (e.g. after a version bump).

Usage: patch_worker_assets.py [runtime-root]   (default: desktop/worker-runtime)
"""

from __future__ import annotations

import sys
from pathlib import Path

_ALL_FONT_FAMILY_STOCK = """\
ALL_FONT_FAMILY = {
    "CN": CN_FONT_FAMILY,
    "TW": TW_FONT_FAMILY,
    "HK": HK_FONT_FAMILY,
    "KR": KR_FONT_FAMILY,
    "JP": JP_FONT_FAMILY,
    "EN": EN_FONT_FAMILY,
    "JA": JP_FONT_FAMILY,
}
"""

_ALL_FONT_FAMILY_TRIMMED = """\
# PaperReader ships zh-target translations only, so the runtime carries just
# the CN family; the other families stay defined above but unreferenced, and
# __cleanup_unused_font_metadata drops their fonts from the offline manifest.
ALL_FONT_FAMILY = {
    "CN": CN_FONT_FAMILY,
}
"""

_GET_FONT_FAMILY_STOCK = """\
def get_font_family(lang_code: str):
    lang_code = lang_code.upper()
    if "KR" in lang_code:
        font_family = KR_FONT_FAMILY
    elif "JP" in lang_code or "JA" in lang_code:
        font_family = JP_FONT_FAMILY
    elif "HK" in lang_code:
        font_family = HK_FONT_FAMILY
    elif "TW" in lang_code:
        font_family = TW_FONT_FAMILY
    elif "EN" in lang_code:
        font_family = EN_FONT_FAMILY
    elif "CN" in lang_code:
        font_family = CN_FONT_FAMILY
    else:
        font_family = EN_FONT_FAMILY
    verify_font_family(font_family)
    return font_family
"""

_GET_FONT_FAMILY_TRIMMED = """\
def get_font_family(lang_code: str):
    # PaperReader targets Chinese only, and the trimmed runtime ships just the
    # CN family, so every target language resolves to it. ("zh" does not match
    # the "CN" substring check below and would otherwise fall through to the
    # EN family, whose fonts are no longer in the metadata.)
    font_family = CN_FONT_FAMILY
    verify_font_family(font_family)
    return font_family
"""


def _metadata_path(runtime: Path) -> Path:
    matches = sorted(
        runtime.glob("lib/python3.*/site-packages/babeldoc/assets/embedding_assets_metadata.py")
    ) or sorted(
        runtime.glob("Lib/site-packages/babeldoc/assets/embedding_assets_metadata.py")
    )
    if not matches:
        sys.exit(f"no babeldoc embedding_assets_metadata.py under {runtime}")
    return matches[0]


def _replace_once(text: str, old: str, new: str, path: Path) -> str:
    if new in text:
        return text
    if old not in text:
        sys.exit(f"expected block not found in {path}; pinned babeldoc source changed?")
    return text.replace(old, new, 1)


def main() -> None:
    runtime = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent / "worker-runtime"
    path = _metadata_path(runtime)
    text = path.read_text(encoding="utf-8")
    text = _replace_once(text, _ALL_FONT_FAMILY_STOCK, _ALL_FONT_FAMILY_TRIMMED, path)
    text = _replace_once(text, _GET_FONT_FAMILY_STOCK, _GET_FONT_FAMILY_TRIMMED, path)
    path.write_text(text, encoding="utf-8")
    print(f"patched {path}")


if __name__ == "__main__":
    main()
