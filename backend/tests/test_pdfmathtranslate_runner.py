"""The worker's own run loop: what it publishes and what it cleans up.

The translator is stubbed here, so PDFMathTranslate-next and BabelDOC are never
imported and nothing reaches the network.
"""

import io
import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

from workers.pdfmathtranslate import runner
from workers.pdfmathtranslate.events import EventWriter
from workers.pdfmathtranslate.job import Job

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _job(tmp_path: Path, *, keep_debug: bool = False) -> Job:
    input_pdf = tmp_path / "paper.pdf"
    input_pdf.write_bytes(b"%PDF-1.7\n")
    output_dir = tmp_path / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    return Job(
        job_id="doc-1",
        input_pdf=input_pdf,
        output_dir=output_dir,
        work_dir=tmp_path / "work",
        api_key="sk-test",
        base_url="https://llm.example/v1",
        model="test-model",
        keep_debug=keep_debug,
    )


def _writer() -> EventWriter:
    return EventWriter(io.StringIO())


def _stub_translation(monkeypatch, debug_files: dict[str, str] | None = None):
    """Stand in for BabelDOC, leaving the layout dump it would have written."""

    def fake_translate(_job, _writer):
        translated = _job.work_dir / "out" / "mono.pdf"
        translated.parent.mkdir(parents=True, exist_ok=True)
        translated.write_bytes(b"%PDF-1.7 translated\n")
        debug_dir = _job.babeldoc_work_dir / _job.input_pdf.stem
        debug_dir.mkdir(parents=True, exist_ok=True)
        for name, text in (debug_files or {}).items():
            (debug_dir / name).write_text(text, encoding="utf-8")
        return types.SimpleNamespace(no_watermark_mono_pdf_path=translated), object()

    monkeypatch.setattr(runner, "_translate", fake_translate)
    monkeypatch.setattr(runner, "build_manifest", lambda *args, **kwargs: {"page_count": 7})

    def write_manifest(path, manifest):
        Path(path).write_text(json.dumps(manifest), encoding="utf-8")

    monkeypatch.setattr(runner, "write_manifest", write_manifest)


def test_a_successful_run_publishes_products_and_removes_the_scratch(
    tmp_path, monkeypatch
):
    job = _job(tmp_path)
    _stub_translation(monkeypatch)
    monkeypatch.setattr(
        runner, "_publish_debug", lambda _job, _debug_dir: {"debug_files": []}
    )

    payload = runner.run(job, _writer())

    assert Path(payload["translated_pdf"]) == job.translated_pdf
    assert job.translated_pdf.is_file()
    assert Path(payload["manifest_path"]) == job.manifest_path
    assert job.manifest_path.is_file()
    assert payload["page_count"] == 7
    assert not job.work_dir.exists()


def test_a_failed_run_still_removes_the_scratch(tmp_path, monkeypatch):
    job = _job(tmp_path)

    def failing_translate(_job, _writer):
        (_job.work_dir / "babeldoc" / "partial.bin").write_bytes(b"x" * 64)
        raise runner.WorkerError("the translator gave up", stage="translate")

    monkeypatch.setattr(runner, "_translate", failing_translate)

    with pytest.raises(runner.WorkerError) as excinfo:
        runner.run(job, _writer())

    assert excinfo.value.stage == "translate"
    assert not job.work_dir.exists()


def test_keep_debug_removes_the_scratch_and_keeps_the_published_copy(
    tmp_path, monkeypatch
):
    job = _job(tmp_path, keep_debug=True)
    _stub_translation(monkeypatch, {"il_translated.json": '{"pages": []}'})

    payload = runner.run(job, _writer())

    published = job.extraction_dir / "debug" / "il_translated.json"
    assert published.is_file()
    assert payload["debug_files"] == ["il_translated.json"]
    assert not job.work_dir.exists()


# Sends SIGTERM to itself with an active job registered, the way the backend's
# cancellation does; the handler has to remove the scratch and exit 143.
_TERMINATION_SCRIPT = """
import os
import signal
import sys
from pathlib import Path

from workers.pdfmathtranslate import runner
from workers.pdfmathtranslate.job import Job

scratch = Path(sys.argv[1])
output = Path(sys.argv[2])
layout = scratch / "babeldoc" / "layout"
layout.mkdir(parents=True, exist_ok=True)
(layout / "dump.json").write_text("{}", encoding="utf-8")

job = Job(
    job_id="doc-1",
    input_pdf=scratch / "paper.pdf",
    output_dir=output,
    work_dir=scratch,
    api_key="sk-test",
    base_url="",
    model="test-model",
)
runner._install_signal_handlers()
runner._ACTIVE_JOB = job
os.kill(os.getpid(), signal.SIGTERM)
raise SystemExit(0)
"""


def test_sigterm_cleans_the_active_jobs_scratch_and_exits_143(tmp_path):
    scratch = tmp_path / "worker" / "doc-1"

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            _TERMINATION_SCRIPT,
            str(scratch),
            str(tmp_path / "output"),
        ],
        cwd=str(_REPO_ROOT),
        env={**os.environ, "PYTHONPATH": str(_REPO_ROOT)},
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 143, result.stderr
    assert not scratch.exists()
