"""The translated PDF uses the application's own bundled Chinese fonts."""

from pathlib import Path

import pytest
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfgen import canvas as pdf_canvas

from app.services import cjk_fonts
from app.services.cjk_fonts import (
    SANS_BOLD_NAME,
    SANS_MEDIUM_NAME,
    SERIF_BOLD_NAME,
    SERIF_REGULAR_NAME,
    require_cjk_font,
)


def test_fonts_come_from_repository_assets_not_the_host():
    fonts = require_cjk_font()
    asset_dir = cjk_fonts.font_asset_dir().resolve()
    assert len(fonts.asset_paths) == 4
    for path in fonts.asset_paths:
        asset = Path(path).resolve()
        assert asset.is_file()
        assert asset.parent == asset_dir
        assert asset.suffix == ".ttf"


def test_four_faces_are_registered_and_usable(tmp_path):
    fonts = require_cjk_font()
    registered = set(pdfmetrics.getRegisteredFontNames())
    assert {
        SERIF_REGULAR_NAME,
        SERIF_BOLD_NAME,
        SANS_MEDIUM_NAME,
        SANS_BOLD_NAME,
    } <= registered

    assert fonts.serif() == SERIF_REGULAR_NAME
    assert fonts.serif(bold=True) == SERIF_BOLD_NAME
    assert fonts.sans() == SANS_MEDIUM_NAME
    assert fonts.sans(bold=True) == SANS_BOLD_NAME
    # Body copy is the serif face and headings are the sans face.
    assert fonts.name(False) == SERIF_REGULAR_NAME
    assert fonts.name(True) == SERIF_BOLD_NAME
    assert fonts.regular == SERIF_REGULAR_NAME
    assert fonts.bold == SERIF_BOLD_NAME

    # Both families render Chinese, Latin and the punctuation the translation
    # mixes in, and the faces really differ from each other.
    target = tmp_path / "fonts.pdf"
    canvas = pdf_canvas.Canvas(str(target), pagesize=(400, 200))
    for name in (SERIF_REGULAR_NAME, SERIF_BOLD_NAME, SANS_MEDIUM_NAME, SANS_BOLD_NAME):
        assert pdfmetrics.stringWidth("中文排版测试 Abc 123，。", name, 10.0) > 0
        canvas.setFont(name, 12)
        canvas.drawString(20, 100, "中文排版测试 Abc 123，。")
    canvas.showPage()
    canvas.save()
    assert target.stat().st_size > 0

    assert pdfmetrics.stringWidth("W", SERIF_REGULAR_NAME, 12.0) != pytest.approx(
        pdfmetrics.stringWidth("W", SANS_BOLD_NAME, 12.0)
    )
