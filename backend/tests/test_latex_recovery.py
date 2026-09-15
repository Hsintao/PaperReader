import copy
import json
from pathlib import Path

import pytest

from app.models.store import LatexRecoveryEntry
from app.models import store
from app.services import document_pipeline, latex_recovery
from app.services.latex_service import LatexCompileResult


def _fixture_tex() -> str:
    return (
        "\\documentclass{article}\n"
        "\\begin{document}\n"
        "Safe before.\n"
        "Another safe line.\n"
        "Big & Tall\n"
        "Safe after.\n"
        "\\end{document}\n"
    )


def _fixture_log() -> str:
    return "! Misplaced alignment tab character &.\nl.5 Big & Tall\n"


def test_recovery_without_line_anchors_still_diagnoses_and_repairs(tmp_path, monkeypatch):
    tex_path = tmp_path / "translated.tex"
    log_path = tmp_path / "translated.log"
    tex_path.write_text(_fixture_tex().replace("Big & Tall", r"\textbfbroken{Tall}"), encoding="utf-8")
    log_path.write_text("! Emergency stop.\n*** (job aborted, no legal \\end found)\n", encoding="utf-8")
    responses = iter([
        json.dumps({"summary": "Misspelled formatting command.", "error_lines": [5]}),
        json.dumps({"patches": [{"start_line": 5, "end_line": 5,
            "original": r"\textbfbroken{Tall}", "replacement": r"\textbf{Tall}", "reason": "correct command"}]}),
    ])
    monkeypatch.setattr(latex_recovery.llm_client, "chat", lambda **kwargs: next(responses))

    def compile_ok(path, output_dir, compiler=None):
        assert r"\textbf{Tall}" in path.read_text(encoding="utf-8")
        return LatexCompileResult(output_dir / "translated.pdf")

    outcome = latex_recovery.recover_latex_document(
        tex_path, log_path, provider_settings=None, compile_func=compile_ok,
    )
    assert outcome.result is not None, outcome.report.last_error


@pytest.mark.parametrize("bad_response", ["not JSON", '{"patches":[]}'])
def test_recovery_retries_invalid_model_output_with_feedback(tmp_path, monkeypatch, bad_response):
    tex_path = tmp_path / "translated.tex"
    log_path = tmp_path / "translated.log"
    tex_path.write_text(_fixture_tex(), encoding="utf-8")
    log_path.write_text(_fixture_log(), encoding="utf-8")
    diagnosis = json.dumps({"summary": "Escape the prose ampersand.", "error_lines": [5]})
    patch = json.dumps({"patches": [{"start_line": 5, "end_line": 5,
        "original": "Big & Tall", "replacement": r"Big \& Tall", "reason": "escape"}]})
    responses = iter(
        [bad_response, diagnosis, f"```json\n{patch}\n```"] if bad_response == "not JSON"
        else [diagnosis, bad_response, diagnosis, patch]
    )
    messages = []

    def chat(**kwargs):
        messages.append(kwargs["message"])
        return next(responses)

    monkeypatch.setattr(latex_recovery.llm_client, "chat", chat)
    outcome = latex_recovery.recover_latex_document(
        tex_path, log_path, provider_settings=None,
        initial_error="latexmk process failed before producing a PDF",
        compile_func=lambda path, output_dir, **kwargs: LatexCompileResult(output_dir / "translated.pdf"),
    )
    assert outcome.result is not None, outcome.report.last_error
    assert outcome.report.rounds == 2
    assert "latexmk process failed" in messages[0]
    assert "latexmk process failed" in messages[-1]
    assert "Previous repair attempt was rejected" in messages[-1]
    assert "<diagnosis>\nEscape the prose ampersand." in messages[-1]


def test_recovery_uses_compiler_despite_preflight_advisory_and_continues_past_two_rounds(tmp_path, monkeypatch):
    tex_path = tmp_path / "translated.tex"
    log_path = tmp_path / "translated.log"
    tex_path.write_text(_fixture_tex(), encoding="utf-8")
    log_path.write_text(_fixture_log(), encoding="utf-8")
    responses = []
    before = "Big & Tall"
    for attempt in range(1, 4):
        after = r"Big \& Tall" + "." * attempt
        responses.extend([
            json.dumps({"summary": f"Diagnosis {attempt}", "error_lines": [5]}),
            json.dumps({"patches": [{"start_line": 5, "end_line": 5,
                "original": before, "replacement": after, "reason": f"Repair {attempt}"}]}),
        ])
        before = after
    replies = iter(responses)
    messages = []

    def chat(**kwargs):
        messages.append(kwargs["message"])
        return next(replies)

    monkeypatch.setattr(latex_recovery.llm_client, "chat", chat)
    monkeypatch.setattr(latex_recovery, "validate_latex_structure", lambda tex: [(5, "advisory")])
    compile_calls = []

    def compile_eventually_ok(path, output_dir, **kwargs):
        compile_calls.append(path.read_text(encoding="utf-8"))
        if len(compile_calls) < 3:
            log_path.write_text(f"! Remaining issue {len(compile_calls)}.\nl.5 source\n", encoding="utf-8")
            raise RuntimeError(f"Compiler feedback {len(compile_calls)}")
        return LatexCompileResult(output_dir / "translated.pdf")

    outcome = latex_recovery.recover_latex_document(
        tex_path, log_path, provider_settings=None, compile_func=compile_eventually_ok,
    )
    assert outcome.result is not None, outcome.report.last_error
    assert outcome.report.rounds == 3
    assert len(compile_calls) == 3
    assert "Compiler feedback 2" in messages[-1]
    assert "Remaining issue 2" in messages[-1]


def test_patch_can_fix_preamble_outside_compiler_window(tmp_path):
    tex_path = tmp_path / "translated.tex"
    tex_path.write_text(_fixture_tex(), encoding="utf-8")
    latex_recovery._validate_and_apply_patches(tex_path, {"patches": [{
        "start_line": 2, "end_line": 2, "original": r"\begin{document}",
        "replacement": "\\usepackage{amsmath}\n\\newcommand{\\papername}{Paper}\n\\begin{document}",
        "reason": "Supply required math commands and macro definition",
    }]}, {5}, 1)
    assert r"\usepackage{amsmath}" in tex_path.read_text(encoding="utf-8")


def test_patch_can_repair_a_large_environment_while_preserving_content(tmp_path):
    tex_path = tmp_path / "translated.tex"
    before = "\n".join([r"\begin{itemize}"] + [f"\\item Entry {index}" for index in range(100)] + [r"\end{enumerate}"])
    after = before.replace(r"\end{enumerate}", r"\end{itemize}")
    tex_path.write_text("\\documentclass{article}\n\\begin{document}\n" + before + "\n\\end{document}\n", encoding="utf-8")
    changes = latex_recovery._validate_and_apply_patches(tex_path, {"patches": [{
        "start_line": 3, "end_line": 104, "original": before, "replacement": after,
        "reason": "Match environment delimiters",
    }]}, {104}, 1)
    assert changes
    assert after in tex_path.read_text(encoding="utf-8")


def test_recovery_persists_analysis_before_applying_bounded_patch(tmp_path, monkeypatch):
    tex_path = tmp_path / "translated.tex"
    log_path = tmp_path / "translated.log"
    tex_path.write_text(_fixture_tex(), encoding="utf-8")
    log_path.write_text(_fixture_log(), encoding="utf-8")
    original = tex_path.read_bytes()
    responses = iter([
        '{"summary":"An unescaped ampersand is used in prose.","error_lines":[5]}',
        '{"patches":[{"start_line":5,"end_line":5,"original":"Big & Tall","replacement":"Big \\\\& Tall","reason":"escape prose ampersand"}]}',
    ])
    monkeypatch.setattr(latex_recovery.llm_client, "chat", lambda **kwargs: next(responses))
    updates: list[LatexRecoveryEntry] = []

    def on_update(report: LatexRecoveryEntry) -> None:
        updates.append(copy.deepcopy(report))
        if report.status == "analyzing" and report.diagnosis:
            assert tex_path.read_bytes() == original

    def compile_ok(path, output_dir, compiler=None):
        assert "Big \\& Tall" in path.read_text(encoding="utf-8")
        pdf = output_dir / "translated.pdf"
        pdf.write_bytes(b"pdf")
        return LatexCompileResult(pdf)

    outcome = latex_recovery.recover_latex_document(
        tex_path,
        log_path,
        provider_settings=None,
        compile_func=compile_ok,
        on_update=on_update,
    )

    assert outcome.result is not None
    assert outcome.report.status == "succeeded"
    assert outcome.report.rounds == 1
    assert outcome.report.diagnosis == "An unescaped ampersand is used in prose."
    assert (tmp_path / "translated.before-repair-1.tex").read_bytes() == original
    assert updates[0].status == "analyzing"
    assert any(update.status == "repairing" for update in updates)
    assert any(update.status == "recompiling" for update in updates)


def test_recovery_rejects_dangerous_or_out_of_window_patch_without_writing(tmp_path, monkeypatch):
    tex_path = tmp_path / "translated.tex"
    log_path = tmp_path / "translated.log"
    tex_path.write_text(_fixture_tex(), encoding="utf-8")
    log_path.write_text(_fixture_log(), encoding="utf-8")
    original = tex_path.read_bytes()
    responses = iter([
        '{"summary":"Bad ampersand.","error_lines":[5]}',
        '{"patches":[{"start_line":5,"end_line":5,"original":"Big & Tall","replacement":"\\\\input{secret}","reason":"unsafe"}]}',
    ])
    monkeypatch.setattr(latex_recovery.llm_client, "chat", lambda **kwargs: next(responses))

    outcome = latex_recovery.recover_latex_document(
        tex_path,
        log_path,
        provider_settings=None,
        compile_func=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not compile")),
        max_rounds=1,
    )

    assert outcome.result is None
    assert outcome.report.status == "failed"
    assert "unsafe" in (outcome.report.last_error or "").lower()
    assert tex_path.read_bytes() == original
    assert not (tmp_path / "translated.before-repair-1.tex").exists()


def test_recovery_rejects_new_file_paths_without_writing(tmp_path, monkeypatch):
    tex_path = tmp_path / "translated.tex"
    log_path = tmp_path / "translated.log"
    tex_path.write_text(_fixture_tex(), encoding="utf-8")
    log_path.write_text(_fixture_log(), encoding="utf-8")
    original = tex_path.read_bytes()
    responses = iter([
        '{"summary":"Bad ampersand.","error_lines":[5]}',
        '{"patches":[{"start_line":5,"end_line":5,"original":"Big & Tall",'
        '"replacement":"Read C:\\\\private\\\\secret.tex","reason":"unsafe path"}]}',
    ])
    monkeypatch.setattr(latex_recovery.llm_client, "chat", lambda **kwargs: next(responses))

    outcome = latex_recovery.recover_latex_document(
        tex_path,
        log_path,
        provider_settings=None,
        compile_func=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not compile")),
        max_rounds=1,
    )

    assert outcome.result is None
    assert "file path" in (outcome.report.last_error or "").lower()
    assert tex_path.read_bytes() == original
    assert not (tmp_path / "translated.before-repair-1.tex").exists()


def test_recovery_rejects_new_file_io_command_and_bare_filename(tmp_path):
    tex_path = tmp_path / "translated.tex"
    tex_path.write_text(_fixture_tex(), encoding="utf-8")

    for replacement, expected in (
        (r"\InputIfFileExists{secret.tex}{}{}", "unsafe latex"),
        ("Read secret.tex", "file path"),
    ):
        payload = {
            "patches": [{
                "start_line": 5,
                "end_line": 5,
                "original": "Big & Tall",
                "replacement": replacement,
                "reason": "unsafe",
            }]
        }
        try:
            latex_recovery._validate_and_apply_patches(tex_path, payload, {5}, 1)
        except ValueError as exc:
            assert expected in str(exc).lower()
        else:
            raise AssertionError("unsafe patch must be rejected")


def test_patch_validator_relocates_unique_original_inside_error_window(tmp_path):
    tex_path = tmp_path / "translated.tex"
    tex_path.write_text(_fixture_tex(), encoding="utf-8")

    changes = latex_recovery._validate_and_apply_patches(
        tex_path,
        {
            "patches": [{
                "start_line": 4,
                "end_line": 4,
                "original": "Big & Tall",
                "replacement": r"Big \& Tall",
                "reason": "escape",
            }]
        },
        {3, 4, 5, 6},
        1,
    )

    assert changes[0]["start_line"] == 5
    assert changes[0]["end_line"] == 5
    assert "Big \\& Tall" in tex_path.read_text(encoding="utf-8")


def test_repair_write_is_atomic_and_existing_backup_is_not_overwritten(tmp_path, monkeypatch):
    tex_path = tmp_path / "translated.tex"
    tex_path.write_text(_fixture_tex(), encoding="utf-8")
    original = tex_path.read_bytes()
    old_backup = tmp_path / "translated.before-repair-1.tex"
    old_backup.write_bytes(b"immutable older recovery")
    replace_calls: list[tuple[Path, Path]] = []
    real_replace = latex_recovery.os.replace

    def observed_replace(source, target):
        replace_calls.append((Path(source), Path(target)))
        real_replace(source, target)

    monkeypatch.setattr(latex_recovery.os, "replace", observed_replace)
    changes = latex_recovery._validate_and_apply_patches(
        tex_path,
        {
            "patches": [{
                "start_line": 5,
                "end_line": 5,
                "original": "Big & Tall",
                "replacement": r"Big \& Tall",
                "reason": "escape",
            }]
        },
        {5},
        1,
    )

    assert replace_calls and replace_calls[-1][1] == tex_path
    assert old_backup.read_bytes() == b"immutable older recovery"
    new_backup = Path(changes[0]["backup"])
    assert new_backup != old_backup
    assert new_backup.read_bytes() == original


def test_patch_validator_rejects_whole_document_rewrite(tmp_path):
    tex_path = tmp_path / "translated.tex"
    lines = ["\\begin{document}"] + [f"line {index}" for index in range(1, 70)] + ["\\end{document}"]
    tex_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    payload = {
        "patches": [{
            "start_line": 2,
            "end_line": 66,
            "original": "\n".join(lines[1:66]),
            "replacement": "small replacement",
            "reason": "rewrite almost everything",
        }]
    }

    try:
        latex_recovery._validate_and_apply_patches(
            tex_path, payload, set(range(1, len(lines) + 1)), 1
        )
    except ValueError as exc:
        assert "removes too much source content" in str(exc).lower()
    else:
        raise AssertionError("whole-document rewrite must be rejected")

    assert not (tmp_path / "translated.before-repair-1.tex").exists()


def test_recovery_stops_after_two_rounds(tmp_path, monkeypatch):
    tex_path = tmp_path / "translated.tex"
    log_path = tmp_path / "translated.log"
    tex_path.write_text(_fixture_tex(), encoding="utf-8")
    log_path.write_text(_fixture_log(), encoding="utf-8")
    responses = iter([
        '{"summary":"round one","error_lines":[5]}',
        '{"patches":[{"start_line":5,"end_line":5,"original":"Big & Tall","replacement":"Big \\\\& Tall","reason":"round one"}]}',
        '{"summary":"round two","error_lines":[5]}',
        '{"patches":[{"start_line":5,"end_line":5,"original":"Big \\\\& Tall","replacement":"Big \\\\& Tall.","reason":"round two"}]}',
    ])
    monkeypatch.setattr(latex_recovery.llm_client, "chat", lambda **kwargs: next(responses))

    def compile_fail(*args, **kwargs):
        raise RuntimeError("still broken")

    outcome = latex_recovery.recover_latex_document(
        tex_path,
        log_path,
        provider_settings=None,
        compile_func=compile_fail,
        max_rounds=2,
    )
    assert outcome.result is None
    assert outcome.report.status == "failed"
    assert outcome.report.rounds == 2
    assert (tmp_path / "translated.before-repair-1.tex").exists()
    assert (tmp_path / "translated.before-repair-2.tex").exists()


def test_pipeline_treats_lenient_result_as_recovery_input(
    isolated_storage, monkeypatch
):
    source = isolated_storage / "source.pdf"
    source.write_bytes(b"pdf")
    record = store.DocumentRecord("strict-gate", 1, "pdf", source)
    tex_path = isolated_storage / "translated.tex"
    tex_path.write_text(_fixture_tex().replace("Big & Tall", "Safe text"), encoding="utf-8")
    lenient_pdf = isolated_storage / "lenient.pdf"
    lenient_pdf.write_bytes(b"pdf")
    recovered_pdf = isolated_storage / "recovered.pdf"
    recovered_pdf.write_bytes(b"pdf")

    monkeypatch.setattr(
        document_pipeline,
        "compile_tex_project_with_fallback",
        lambda *a, **k: LatexCompileResult(
            lenient_pdf,
            warning="strict compile failed",
            errors=[{"line": 3, "message": "strict error"}],
        ),
    )
    recovery_calls: list[int] = []

    def recover(*args, **kwargs):
        recovery_calls.append(1)
        return latex_recovery.LatexRecoveryOutcome(
            LatexCompileResult(recovered_pdf),
            LatexRecoveryEntry(status="succeeded", diagnosis="fixed", rounds=1),
        )

    monkeypatch.setattr(document_pipeline, "recover_latex_document", recover)

    result = document_pipeline._compile_translated_tex(
        record, tex_path, isolated_storage, provider_settings=None
    )

    assert result.pdf_path == recovered_pdf
    assert recovery_calls == [1]


def test_pipeline_accepts_strict_compile_with_missing_glyph_warning(
    isolated_storage, monkeypatch
):
    source = isolated_storage / "source.pdf"
    source.write_bytes(b"pdf")
    record = store.DocumentRecord("glyph-warning", 1, "pdf", source)
    tex_path = isolated_storage / "translated.tex"
    tex_path.write_text(_fixture_tex().replace("Big & Tall", "Safe text"), encoding="utf-8")
    pdf = isolated_storage / "translated.pdf"
    pdf.write_bytes(b"pdf")
    warning = "PDF compiled, but one character is missing from the font"

    monkeypatch.setattr(
        document_pipeline,
        "compile_tex_project_with_fallback",
        lambda *a, **k: LatexCompileResult(
            pdf,
            warning=warning,
            missing_chars=[{"char": "∷", "codepoint": "U+2237", "count": 1}],
        ),
    )
    monkeypatch.setattr(
        document_pipeline,
        "recover_latex_document",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not recover")),
    )

    result = document_pipeline._compile_translated_tex(
        record, tex_path, isolated_storage, provider_settings=None
    )

    assert result.pdf_path == pdf
    assert record.last_compile_warning == warning


def test_clean_recompile_clears_stale_recovery_failure(isolated_storage, monkeypatch):
    source = isolated_storage / "source.pdf"
    source.write_bytes(b"pdf")
    record = store.DocumentRecord(
        "stale-recovery",
        1,
        "pdf",
        source,
        latex_recovery=LatexRecoveryEntry(
            status="failed", diagnosis="old failure", last_error="old error"
        ),
    )
    tex_path = isolated_storage / "translated.tex"
    tex_path.write_text(_fixture_tex().replace("Big & Tall", "Safe text"), encoding="utf-8")
    pdf = isolated_storage / "translated.pdf"
    pdf.write_bytes(b"pdf")
    monkeypatch.setattr(
        document_pipeline,
        "compile_tex_project_with_fallback",
        lambda *a, **k: LatexCompileResult(pdf),
    )

    document_pipeline._compile_translated_tex(
        record, tex_path, isolated_storage, provider_settings=None
    )

    assert record.latex_recovery is not None
    assert record.latex_recovery.status == "succeeded"
    assert record.latex_recovery.last_error is None


def test_allowed_lines_ignore_spoofed_foreign_file_anchors(tmp_path):
    tex_lines = ["\\documentclass{article}", "\\begin{document}"]
    tex_lines += [f"safe prose {index}" for index in range(3, 55)]
    tex_lines += ["Big & Tall", "safe after.", "\\end{document}"]
    tex = "\n".join(tex_lines) + "\n"
    tex_path = tmp_path / "translated.tex"
    tex_path.write_text(tex, encoding="utf-8")
    log_path = tmp_path / "translated.log"
    # A planted foreign-file anchor at line 5 would open a window far from the
    # real error at line 55.
    log_path.write_text(
        "evil.tex:5: planted anchor\n! Misplaced alignment tab character &.\nl.55 Big & Tall\n",
        encoding="utf-8",
    )

    allowed = latex_recovery._allowed_lines(log_path, tex, tex_name="translated.tex")

    assert 20 not in allowed  # inside 5 +/- 20 when the decoy anchor counts
    assert 40 in allowed  # inside the genuine 55 +/- 20 window


def test_patch_line_accounting_ignores_unicode_line_separators(tmp_path):
    # U+2028 inside prose is one character, not a line break: TeX numbers lines
    # by "\n" only and the validator must agree, or patches land on shifted
    # lines and the whole document drifts.
    tex = (
        "\\documentclass{article}\n"
        "\\begin{document}\n"
        "price \u2028 ten\n"
        "Big & Tall\n"
        "safe after.\n"
        "\\end{document}\n"
    )
    tex_path = tmp_path / "translated.tex"
    tex_path.write_text(tex, encoding="utf-8", newline="")
    original_bytes = tex_path.read_bytes()

    changes = latex_recovery._validate_and_apply_patches(
        tex_path,
        {
            "patches": [
                {
                    "start_line": 4,
                    "end_line": 4,
                    "original": "Big & Tall",
                    "replacement": "Big \\& Tall",
                    "reason": "escape prose ampersand",
                }
            ]
        },
        allowed={4},
        round_number=1,
    )

    assert changes and changes[0]["start_line"] == 4
    updated_bytes = tex_path.read_bytes()
    assert b"Big \\& Tall" in updated_bytes
    assert "\u2028".encode("utf-8") in updated_bytes
    # Every other line stays byte-identical.
    original_lines = original_bytes.decode("utf-8").split("\n")
    updated_lines = updated_bytes.decode("utf-8").split("\n")
    assert updated_lines[:3] == original_lines[:3]
    assert updated_lines[4:] == original_lines[4:]


def test_patch_write_preserves_crlf_endings(tmp_path):
    tex = "\\documentclass{article}\r\n\\begin{document}\r\nBig & Tall\r\n\\end{document}\r\n"
    tex_path = tmp_path / "translated.tex"
    tex_path.write_bytes(tex.encode("utf-8"))
    original = tex_path.read_bytes()

    latex_recovery._validate_and_apply_patches(
        tex_path,
        {
            "patches": [
                {
                    "start_line": 3,
                    "end_line": 3,
                    "original": "Big & Tall",
                    "replacement": "Big \\& Tall",
                    "reason": "escape prose ampersand",
                }
            ]
        },
        allowed={3},
        round_number=1,
    )

    assert tex_path.read_bytes() == original.replace(b"Big & Tall", b"Big \\& Tall")
