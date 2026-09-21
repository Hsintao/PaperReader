"""Fixed Chinese fonts bundled with the application.

Translated text is drawn with the fonts shipped in ``app/assets/fonts``, not
with whatever the host happens to have installed: every platform then renders
the same glyphs at the same metrics, and the faces travel inside the exported
PDF. The set holds a Song (serif) regular/bold pair, which the layout template
uses for body copy and headings alike, and a Hei (sans) medium/bold pair.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont, TTFError

SERIF_REGULAR_NAME = "PaperReaderSong"
SERIF_BOLD_NAME = "PaperReaderSong-Bold"
SANS_MEDIUM_NAME = "PaperReaderHei"
SANS_BOLD_NAME = "PaperReaderHei-Bold"

_FONT_ASSETS = {
    SERIF_REGULAR_NAME: "NotoSerifSC-Regular.ttf",
    SERIF_BOLD_NAME: "NotoSerifSC-Bold.ttf",
    SANS_MEDIUM_NAME: "NotoSansSC-Medium.ttf",
    SANS_BOLD_NAME: "NotoSansSC-Bold.ttf",
}

# Coverage probe: common Han characters, CJK punctuation, and the Latin/digit
# range the translated prose mixes in (acronyms, numbers, units).
_PROBE_CHARACTERS = (
    "中文排版测试论文摘要引言方法实验结论参考文献"
    "，。、；：？！（）《》〈〉“”‘’—…【】·"
    "ABCabc0123456789%$&+-*/()[]{}<>@#=_"
    "①②③④⑤±×÷≈≤≥→←"
)


@dataclass(frozen=True)
class CjkFontSet:
    """The bundled faces, plus the asset files they were loaded from."""

    serif_regular: str
    serif_bold: str
    sans_medium: str
    sans_bold: str
    asset_paths: tuple[str, ...]

    def serif(self, bold: bool = False) -> str:
        return self.serif_bold if bold else self.serif_regular

    def sans(self, bold: bool = False) -> str:
        return self.sans_bold if bold else self.sans_medium

    # Body copy and headings are both Song, so the unqualified family is the
    # serif one; `name()` keeps the two-face call sites reading naturally.
    @property
    def regular(self) -> str:
        return self.serif_regular

    @property
    def bold(self) -> str:
        return self.serif_bold

    def name(self, bold: bool = False) -> str:
        return self.serif(bold)

    @property
    def path(self) -> str:
        return self.asset_paths[0]


class CjkFontUnavailable(RuntimeError):
    """Raised when a bundled Chinese font cannot be loaded."""


_CACHE: CjkFontSet | None = None


def font_asset_dir() -> Path:
    """Directory holding the bundled font files, packaged or in a checkout."""
    return Path(__file__).resolve().parent.parent / "assets" / "fonts"


def _covers(file_path: Path) -> bool:
    try:
        face = TTFont("probe", str(file_path))
    except (TTFError, OSError, ValueError, TypeError, KeyError, IndexError):
        return False
    glyphs = getattr(face.face, "charToGlyph", None)
    if not glyphs:
        return False
    return all(ord(char) in glyphs for char in _PROBE_CHARACTERS)


def _register(file_path: Path, name: str) -> bool:
    try:
        pdfmetrics.registerFont(TTFont(name, str(file_path)))
    except (TTFError, OSError, ValueError, TypeError, KeyError, IndexError):
        return False
    return True


def load_cjk_fonts() -> CjkFontSet:
    """Register the bundled faces once and return the set."""
    global _CACHE
    if _CACHE is not None:
        return _CACHE

    directory = font_asset_dir()
    registered: dict[str, str] = {}
    paths: list[str] = []
    for name, filename in _FONT_ASSETS.items():
        file_path = directory / filename
        if not file_path.is_file() or not _covers(file_path):
            raise CjkFontUnavailable(
                f"Bundled Chinese font is missing or unusable: {file_path}"
            )
        if not _register(file_path, name):
            raise CjkFontUnavailable(
                f"Bundled Chinese font could not be registered: {file_path}"
            )
        registered[name] = str(file_path)
        paths.append(str(file_path))

    _CACHE = CjkFontSet(
        serif_regular=SERIF_REGULAR_NAME,
        serif_bold=SERIF_BOLD_NAME,
        sans_medium=SANS_MEDIUM_NAME,
        sans_bold=SANS_BOLD_NAME,
        asset_paths=tuple(paths),
    )
    return _CACHE


def find_cjk_font() -> CjkFontSet | None:
    """Return the bundled font set, or None when it cannot be loaded."""
    try:
        return load_cjk_fonts()
    except CjkFontUnavailable:
        return None


def require_cjk_font() -> CjkFontSet:
    return load_cjk_fonts()
