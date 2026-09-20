"""Inline tags kept by the translation contract must render as typography.

The translation pipeline protects ``<sub>``/``<sup>``/``<br>`` as placeholders
and restores them in the translation, so the renderer receives them as literal
text. Escaping them the way ordinary prose is escaped printed the tags
themselves into the translated PDF.
"""

import pytest

from app.services.cjk_fonts import find_cjk_font, require_cjk_font
from app.services.layout_fit import Fragment, TextMeasurer, _inline_markup

pytestmark = pytest.mark.skipif(
    find_cjk_font() is None, reason="requires an installed CJK font"
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ℓ<sub>1</sub>-ℓ<sub>0</sub>", "ℓ<sub>1</sub>-ℓ<sub>0</sub>"),
        ("λ<sub>max</sub>", "λ<sub>max</sub>"),
        ("x<sup>2</sup>", "x<super>2</super>"),
        ("<SUP>1</SUP>", "<super>1</super>"),
        ("a<br>b", "a<br/>b"),
        ("a<br />b", "a<br/>b"),
        # Everything that is not one of the preserved tags stays escaped.
        ("5 < 6 > 4", "5 &lt; 6 &gt; 4"),
        ("a&b", "a&amp;b"),
        ("<b>bold</b>", "&lt;b&gt;bold&lt;/b&gt;"),
        # The model sometimes drags a tag pair onto a whole clause; the pair is
        # dropped so the clause stays plain text instead of becoming subscript.
        ("当<sub> 固定时，</sub>对细节层的影响", "当 固定时，对细节层的影响"),
        # Unbalanced tags never reach reportlab, which rejects them and would
        # cost the block its translation.
        ("a<sub>1 b", "a1 b"),
        ("a</sub> b", "a b"),
        ("a<sub>1</sub></sub>", "a<sub>1</sub>"),
    ],
)
def test_inline_markup(text, expected):
    assert _inline_markup(text) == expected


def test_measurer_draws_subscripts_and_escapes_other_tags():
    measurer = TextMeasurer(require_cjk_font())
    markup = measurer.markup(
        [Fragment(kind="text", text="混合ℓ<sub>1</sub>-ℓ<sub>0</sub>模型 <b>x</b>")],
        10.5,
    )

    assert "ℓ<sub>1</sub>-ℓ<sub>0</sub>" in markup
    assert "<b>" not in markup
