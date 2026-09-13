import shutil

import pytest

from app.core.config import settings
from app.services.latex_service import (
    TRANSLATED_LATEX_COMPILER,
    _run_latexmk,
    compile_tex_project_with_fallback,
    create_translated_tex_from_ir,
    parse_latex_log_issues,
)
from app.services.mineru_layout import Paragraph, TextRun


@pytest.mark.skipif(shutil.which(settings.latexmk_path) is None, reason="latexmk is not installed")
def test_real_xelatex_compiles_currency_ampersand_and_math_star(tmp_path):
    tex_path = tmp_path / "translated.tex"
    create_translated_tex_from_ir(
        [
            Paragraph(runs=[TextRun(text=r"\$10.99 in Big & Tall, then \$3.99.")]),
            Paragraph(runs=[TextRun(text="GiGPO ⋆ is highlighted.")]),
        ],
        tex_path,
        title="Regression",
    )

    result = compile_tex_project_with_fallback(
        tex_path, tmp_path, compiler=TRANSLATED_LATEX_COMPILER
    )

    assert result.pdf_path.is_file()
    assert result.pdf_path.stat().st_size > 0
    assert result.missing_chars == []
    generated = tex_path.read_text(encoding="utf-8")
    assert r"Big \& Tall" in generated
    assert r"$\star$" in generated


@pytest.mark.skipif(shutil.which(settings.latexmk_path) is None, reason="latexmk is not installed")
def test_real_xelatex_reproduces_the_three_original_fatal_errors(tmp_path):
    tex_path = tmp_path / "broken.tex"
    tex_path.write_text(
        "\\documentclass{article}\n"
        "\\begin{document}\n"
        "\\subsection*{leaked model reasoning\n\n"
        "more leaked reasoning}\n"
        "}\n"
        "Big & Tall\n"
        "\\end{document}\n",
        encoding="utf-8",
    )

    result = _run_latexmk(
        tex_path, tmp_path, force=True, compiler=TRANSLATED_LATEX_COMPILER
    )
    errors, _ = parse_latex_log_issues(tmp_path / "broken.log")
    messages = "\n".join(str(error["message"]) for error in errors)

    assert result.returncode != 0
    assert "Paragraph ended before \\@ssect was complete" in messages
    assert "Too many }'s" in messages
    assert "Misplaced alignment tab character &" in messages


_MICROTYPE_TRIGGER_SOURCE = """\\documentclass{article}
\\renewcommand{\\rmdefault}{ptm}
\\usepackage[utf8]{inputenc}
\\usepackage[T1]{fontenc}
\\usepackage{xcolor}
\\usepackage{microtype}
\\begin{document}
\\begin{itemize}
\\item {\\color{green!60!black} 对齐驱动力：} sample text.
\\end{itemize}
\\end{document}
"""


def _translate_with_production_injections(source_text: str) -> str:
    """Run the translated-document prefix pipeline without the LLM."""
    from app.services import translate_service

    prefix, body, suffix = translate_service._split_latex_document(source_text)
    prefix = translate_service._ensure_xelatex_compatibility(prefix)
    prefix = translate_service._ensure_cjk_support(prefix)
    return f"{prefix}\n{body}\n{suffix}"


@pytest.mark.skipif(shutil.which(settings.latexmk_path) is None, reason="latexmk is not installed")
def test_real_xelatex_microtype_protrusion_guard_prevents_ptmr8c_abort(tmp_path):
    # NeurIPS-style font setup: Type1 Times as \rmdefault plus microtype. With
    # xeCJK loaded ahead of fontenc (the translated-preamble order), the
    # itemize bullet substitutes TS1/ptm and microtype's protrusion setup
    # aborts the run on the Type1 ptmr8c. The injected guard must keep the
    # strict (non-fallback) compile green.
    tex_path = tmp_path / "translated.tex"
    tex_path.write_text(
        _translate_with_production_injections(_MICROTYPE_TRIGGER_SOURCE),
        encoding="utf-8",
    )

    result = compile_tex_project_with_fallback(
        tex_path, tmp_path, compiler=TRANSLATED_LATEX_COMPILER
    )

    assert result.pdf_path.is_file()
    assert result.pdf_path.stat().st_size > 0
    assert not result.used_fallback


@pytest.mark.skipif(shutil.which(settings.latexmk_path) is None, reason="latexmk is not installed")
def test_real_xelatex_microtype_protrusion_trigger_still_exists(tmp_path):
    # Canary for the engine failure the protrusion guard neutralizes: xeCJK
    # loaded right after \documentclass (but before fontenc) leaves the text
    # encoding in the state where the TS1/ptm substitution aborts. If a future
    # TeX Live stops failing here, the guard can be retired.
    tex_path = tmp_path / "trigger.tex"
    tex_path.write_text(
        _MICROTYPE_TRIGGER_SOURCE.replace(
            "\\documentclass{article}\n",
            "\\documentclass{article}\n\\usepackage{xeCJK}\n",
            1,
        ),
        encoding="utf-8",
    )

    outcome = _run_latexmk(
        tex_path, tmp_path, force=True, compiler=TRANSLATED_LATEX_COMPILER
    )
    errors, _ = parse_latex_log_issues(tmp_path / "trigger.log")
    messages = "\n".join(str(error["message"]) for error in errors)

    assert outcome.returncode != 0
    assert "Cannot use XeTeXglyph with ptmr8c; not a native platform font." in messages
