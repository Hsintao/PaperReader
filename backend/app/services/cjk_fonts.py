"""System CJK font discovery for the layout renderer.

Translated text is drawn with a Chinese font already installed on the host;
blocks keep the original page's geometry, so the only font the renderer needs
is one serif CJK family with a bold face. The family is registered once per
process and reused for every page.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont, TTFError

REGULAR_NAME = "PaperReaderCJK"
BOLD_NAME = "PaperReaderCJK-Bold"

# Coverage probe: common Han characters, CJK punctuation, and the Latin/digit
# range the translated prose mixes in (acronyms, numbers, units).
_PROBE_CHARACTERS = (
    "中文排版测试论文摘要引言方法实验结论参考文献"
    "，。、；：？！（）《》〈〉“”‘’—…【】·"
    "ABCabc0123456789%$&+-*/()[]{}<>@#=_"
    "①②③④⑤±×÷≈≤≥→←"
)


@dataclass(frozen=True)
class CjkFontFamily:
    regular: str
    bold: str
    path: str

    def name(self, bold: bool = False) -> str:
        return self.bold if bold else self.regular


# Candidate font files per platform. Serif families come first so translated
# pages match the printed look of the source paper. Each entry maps the
# TrueType collection index of the regular and bold faces; `None` means the
# file has no bold face and the renderer strokes the regular one instead.
_CANDIDATES: dict[str, list[tuple[str, int, int | None]]] = {
    "darwin": [
        ("/System/Library/Fonts/Supplemental/Songti.ttc", 6, 1),
        ("/Library/Fonts/Songti.ttc", 6, 1),
        ("/System/Library/Fonts/Supplemental/STSong.ttf", 0, None),
        ("/System/Library/Fonts/STHeiti Medium.ttc", 1, 1),
    ],
    "win32": [
        (r"C:\Windows\Fonts\simsun.ttc", 0, None),
        (r"C:\Windows\Fonts\msyh.ttc", 0, None),
        (r"C:\Windows\Fonts\simhei.ttf", 0, None),
    ],
    "linux": [
        ("/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc", 0, None),
        ("/usr/share/fonts/truetype/noto/NotoSerifCJK-Regular.ttc", 0, None),
        ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", 0, None),
        ("/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf", 0, None),
        ("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc", 0, None),
    ],
}

_EXTRA_DIRECTORIES = (
    "/System/Library/Fonts/Supplemental",
    "/Library/Fonts",
    r"C:\Windows\Fonts",
    "/usr/share/fonts",
    str(Path.home() / "Library" / "Fonts"),
)

_CACHE: CjkFontFamily | None = None


class CjkFontUnavailable(RuntimeError):
    """Raised when no usable Chinese font can be found on this host."""


def _covers(file_path: str, index: int) -> bool:
    try:
        face = TTFont("probe", file_path, subfontIndex=index)
    except (TTFError, OSError, ValueError, TypeError, KeyError, IndexError):
        return False
    glyphs = getattr(face.face, "charToGlyph", None)
    if not glyphs:
        return False
    return all(ord(char) in glyphs for char in _PROBE_CHARACTERS)


def _register(file_path: str, index: int, name: str) -> bool:
    try:
        pdfmetrics.registerFont(TTFont(name, file_path, subfontIndex=index))
    except (TTFError, OSError, ValueError, TypeError, KeyError, IndexError):
        return False
    return True


def _extra_candidates(platform: str) -> list[tuple[str, int, int | None]]:
    """Scan font directories for other TrueType CJK files as a last resort."""
    found: list[tuple[str, int, int | None]] = []
    preferred = (
        ("song", "noto serif cjk", "sourcehanserif", "stsong")
        if platform == "darwin"
        else ("song", "serif cjk", "simsun", "wqy", "droidsansfallback")
    )
    seen = 0
    for directory in _EXTRA_DIRECTORIES:
        root = Path(directory)
        if not root.is_dir():
            continue
        for entry in sorted(root.rglob("*")):
            seen += 1
            if seen > 20000:
                return found
            if not entry.is_file() or entry.suffix.lower() not in {".ttc", ".ttf"}:
                continue
            lowered = entry.name.lower()
            if not any(token in lowered for token in preferred):
                continue
            found.append((str(entry), 0, None))
    return found


def _activate(
    file_path: str, regular_index: int, bold_index: int | None
) -> CjkFontFamily | None:
    if not Path(file_path).is_file() or not _covers(file_path, regular_index):
        return None
    if not _register(file_path, regular_index, REGULAR_NAME):
        return None
    bold_name = REGULAR_NAME
    if bold_index is not None and bold_index != regular_index:
        if _covers(file_path, bold_index) and _register(
            file_path, bold_index, BOLD_NAME
        ):
            bold_name = BOLD_NAME
    return CjkFontFamily(regular=REGULAR_NAME, bold=bold_name, path=file_path)


def find_cjk_font() -> CjkFontFamily | None:
    """Return the first CJK family that covers the probe string, or None."""
    global _CACHE
    if _CACHE is not None:
        return _CACHE

    import sys

    platform = sys.platform
    platform_key = (
        "darwin"
        if platform == "darwin"
        else "win32"
        if platform.startswith("win")
        else "linux"
    )
    for file_path, regular_index, bold_index in _CANDIDATES.get(platform_key, []):
        family = _activate(file_path, regular_index, bold_index)
        if family is not None:
            _CACHE = family
            return _CACHE
    # Nothing in the known locations: scan the font directories once.
    for file_path, regular_index, bold_index in _extra_candidates(platform_key):
        family = _activate(file_path, regular_index, bold_index)
        if family is not None:
            _CACHE = family
            return _CACHE
    return None


def require_cjk_font() -> CjkFontFamily:
    family = find_cjk_font()
    if family is None:
        raise CjkFontUnavailable(
            "No Chinese font with the required coverage was found. Install a "
            "serif CJK font (macOS: Songti SC; Windows: SimSun; Linux: "
            "fonts-noto-cjk) and retry."
        )
    return family

