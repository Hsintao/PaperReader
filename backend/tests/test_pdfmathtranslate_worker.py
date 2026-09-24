"""The process boundary: how a worker run is started and what it reports back.

Every child process here is a throwaway script the test writes itself, so the
suite never runs PDFMathTranslate-next and never reaches the network.
"""

import io
import json
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import pytest

from app.core.config import Settings, settings
from app.services.pdf_translation_worker import (
    WorkerCancelled,
    WorkerError,
    WorkerRun,
    WorkerUnavailable,
    _job_payload,
    _products_from_finish,
    _read_events,
    cancel_worker,
    require_worker_ready,
    run_worker,
    worker_command,
)

_FAKE_WORKER = """
import json
import shutil
import sys
from pathlib import Path

from workers.pdfmathtranslate.events import EventWriter
from workers.pdfmathtranslate.job import load_job

job = load_job(sys.argv[1])
writer = EventWriter()
writer.emit("job_started", job=job.redacted())
writer.stage_summary([{"name": "Parse Page Layout", "percent": 45.0}])
writer.progress("progress_update", "Parse Page Layout", {"stage_progress": 100.0, "stage_total": 2, "stage_current": 2})
writer.progress("progress_update", "Translate Paragraphs", {"stage_progress": 50.0, "stage_total": 4, "stage_current": 2})
writer.progress("progress_end", "Typesetting", {"stage_progress": 100.0})

output = job.output_dir
extraction = output / "extraction"
extraction.mkdir(parents=True, exist_ok=True)
translated = output / "translated.pdf"
shutil.copyfile(job.input_pdf, translated)
manifest = extraction / "manifest.json"
manifest.write_text(
    json.dumps({"schema_version": "paperreader-manifest-v1", "pages": [{"index": 0}]}),
    encoding="utf-8",
)
writer.finish({
    "job_id": job.job_id,
    "translated_pdf": str(translated),
    "manifest_path": str(manifest),
    "extraction_dir": str(extraction),
    "page_count": 1,
    "mode_label": "fake 1.0 · mono",
    "glossary_path": "",
})
"""

_FAILING_WORKER = """
import sys
from workers.pdfmathtranslate.events import EventWriter

writer = EventWriter()
writer.error("no translation API key in the job", stage="translate")
raise SystemExit(1)
"""

_CRASHING_WORKER = """
import sys

sys.stderr.write("worker exploded: missing dependency\\n")
raise SystemExit(3)
"""

_SLEEPING_WORKER = """
import time

time.sleep(60)
"""

# Reports the proxy variables it inherited, so the test can check what the
# backend handed the translator's HTTP client.
_ENV_REPORTING_WORKER = """
import json
import os
import shutil
import sys
from pathlib import Path

from workers.pdfmathtranslate.events import EventWriter
from workers.pdfmathtranslate.job import load_job

job = load_job(sys.argv[1])
output = job.output_dir
extraction = output / "extraction"
extraction.mkdir(parents=True, exist_ok=True)
shutil.copyfile(job.input_pdf, output / "translated.pdf")
manifest = extraction / "manifest.json"
manifest.write_text(json.dumps({"schema_version": "paperreader-manifest-v1", "pages": []}), encoding="utf-8")
(job.work_dir / "env.json").write_text(json.dumps({
    "NO_PROXY": os.environ.get("NO_PROXY", ""),
    "no_proxy": os.environ.get("no_proxy", ""),
    "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
}), encoding="utf-8")
EventWriter().finish({
    "translated_pdf": str(output / "translated.pdf"),
    "manifest_path": str(manifest),
    "extraction_dir": str(extraction),
    "page_count": 0,
})
"""

# Reports the paths it was handed, so the test can check they mean the same
# thing to the backend and to the child.
_PATH_REPORTING_WORKER = """
import json
import os
import shutil
import sys
from pathlib import Path

from workers.pdfmathtranslate.events import EventWriter
from workers.pdfmathtranslate.job import load_job

job = load_job(sys.argv[1])
output = job.output_dir
extraction = output / "extraction"
extraction.mkdir(parents=True, exist_ok=True)
shutil.copyfile(job.input_pdf, output / "translated.pdf")
manifest = extraction / "manifest.json"
manifest.write_text(json.dumps({"schema_version": "paperreader-manifest-v1", "pages": []}), encoding="utf-8")
(job.work_dir / "paths.json").write_text(json.dumps({
    "argv": sys.argv[1],
    "cwd": os.getcwd(),
    "input_pdf": str(job.input_pdf),
    "output_dir": str(job.output_dir),
    "work_dir": str(job.work_dir),
}), encoding="utf-8")
EventWriter().finish({
    "translated_pdf": str(output / "translated.pdf"),
    "manifest_path": str(manifest),
    "extraction_dir": str(extraction),
    "page_count": 0,
})
"""


def _fake_worker(monkeypatch, tmp_path, source: str, name: str = "fake_worker.py") -> None:
    script = tmp_path / name
    script.write_text(source, encoding="utf-8")
    monkeypatch.setattr(settings, "pdfmathtranslate_worker", f"{sys.executable} {script}")


def _input_pdf(tmp_path):
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF-1.7\n")
    return source


def _run(tmp_path, **overrides):
    arguments = {
        "document_id": "doc-1",
        "input_pdf": _input_pdf(tmp_path),
        "output_dir": tmp_path / "output",
        "work_dir": tmp_path / "work",
        "api_key": "sk-secret-value",
        "base_url": "https://llm.example/v1",
        "model": "test-model",
        "timeout": 30,
    }
    arguments.update(overrides)
    return run_worker(**arguments)


def test_worker_command_runs_the_module_under_the_configured_interpreter(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(settings, "pdfmathtranslate_worker", "")
    monkeypatch.setattr(settings, "pdfmathtranslate_python", "/opt/python/bin/python3")
    job = tmp_path / "job.json"

    assert worker_command(job) == [
        "/opt/python/bin/python3",
        "-m",
        "workers.pdfmathtranslate",
        str(job),
    ]


def test_worker_command_uses_a_configured_command_verbatim(monkeypatch, tmp_path):
    monkeypatch.setattr(
        settings, "pdfmathtranslate_worker", "/Applications/PaperReader/worker --quiet"
    )
    job = tmp_path / "job.json"

    assert worker_command(job) == [
        "/Applications/PaperReader/worker",
        "--quiet",
        str(job),
    ]


def test_worker_command_does_not_repeat_a_job_path_it_already_names(
    monkeypatch, tmp_path
):
    job = tmp_path / "job.json"
    monkeypatch.setattr(settings, "pdfmathtranslate_worker", f"/usr/bin/worker {job}")

    assert worker_command(job) == ["/usr/bin/worker", str(job)]


def test_worker_command_without_a_job_has_no_path_to_append(monkeypatch):
    monkeypatch.setattr(settings, "pdfmathtranslate_worker", "")
    monkeypatch.setattr(settings, "pdfmathtranslate_python", "python3")

    assert worker_command() == ["python3", "-m", "workers.pdfmathtranslate"]


def test_a_missing_worker_interpreter_fails_before_a_job_starts(monkeypatch, tmp_path):
    monkeypatch.setattr(
        settings, "pdfmathtranslate_worker", str(tmp_path / "gone" / "worker")
    )

    with pytest.raises(WorkerUnavailable):
        require_worker_ready()


def test_a_worker_command_missing_from_the_path_fails_before_a_job_starts(monkeypatch):
    monkeypatch.setattr(
        settings, "pdfmathtranslate_worker", "paperreader-worker-that-does-not-exist"
    )

    with pytest.raises(WorkerUnavailable):
        require_worker_ready()


def test_a_configured_interpreter_passes_the_readiness_check(monkeypatch):
    monkeypatch.setattr(settings, "pdfmathtranslate_worker", "")
    monkeypatch.setattr(settings, "pdfmathtranslate_python", sys.executable)

    require_worker_ready()


def test_event_reading_keeps_json_objects_and_skips_everything_else():
    lines = [
        "BabelDOC: warming up\n",
        "\n",
        json.dumps({"type": "progress_update", "group": "translate"}) + "\n",
        "[1, 2, 3]\n",
        '{"type": "broken"\n',
        json.dumps({"type": "finish", "translated_pdf": "/tmp/out.pdf"}) + "\n",
    ]
    seen: list[dict] = []

    events = _read_events(lines, seen.append)

    assert [event["type"] for event in events] == ["progress_update", "finish"]
    assert seen == events


def test_event_reading_survives_a_reporter_that_raises():
    lines = [
        json.dumps({"type": "progress_update"}) + "\n",
        json.dumps({"type": "finish"}) + "\n",
    ]

    def explode(event: dict) -> None:
        raise RuntimeError("stage tracking is unavailable")

    events = _read_events(lines, explode)

    assert [event["type"] for event in events] == ["progress_update", "finish"]


def test_products_require_a_translated_pdf_that_exists(tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")

    with pytest.raises(WorkerError) as excinfo:
        _products_from_finish(
            {
                "translated_pdf": str(tmp_path / "missing.pdf"),
                "manifest_path": str(manifest),
            },
            tmp_path,
        )

    assert "translated PDF" in str(excinfo.value)
    assert excinfo.value.stage == "render"


def test_products_require_a_manifest_that_exists(tmp_path):
    translated = tmp_path / "translated.pdf"
    translated.write_bytes(b"%PDF-1.7\n")

    with pytest.raises(WorkerError) as excinfo:
        _products_from_finish(
            {
                "translated_pdf": str(translated),
                "manifest_path": str(tmp_path / "missing.json"),
            },
            tmp_path,
        )

    assert "manifest" in str(excinfo.value)
    assert excinfo.value.stage == "render"


def test_products_treat_an_emptied_or_vanished_glossary_as_absent(tmp_path):
    translated = tmp_path / "translated.pdf"
    translated.write_bytes(b"%PDF-1.7\n")
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    finish = {
        "translated_pdf": str(translated),
        "manifest_path": str(manifest),
    }

    assert _products_from_finish({**finish, "glossary_path": ""}, tmp_path).glossary_path is None
    assert (
        _products_from_finish(
            {**finish, "glossary_path": str(tmp_path / "gone.csv")}, tmp_path
        ).glossary_path
        is None
    )

    produced = tmp_path / "glossary.csv"
    produced.write_text("source,target\nattention,注意力\n", encoding="utf-8")
    products = _products_from_finish({**finish, "glossary_path": str(produced)}, tmp_path)

    assert products.glossary_path == produced
    assert products.extraction_dir == tmp_path / "extraction"


def test_a_job_file_carries_the_key_that_redaction_removes(tmp_path):
    from workers.pdfmathtranslate.job import load_job

    payload = _job_payload(
        document_id="doc-1",
        input_pdf=tmp_path / "paper.pdf",
        output_dir=tmp_path / "output",
        work_dir=tmp_path / "work",
        api_key="sk-secret-value",
        base_url="https://llm.example/v1",
        model="test-model",
        glossary_path=None,
    )
    job_path = tmp_path / "job.json"
    job_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    job = load_job(job_path)

    assert job.api_key == "sk-secret-value"
    assert job.output_mode == settings.pdfmathtranslate_output_mode
    assert "glossary" not in payload

    redacted = job.redacted()
    assert redacted["translation"]["api_key"] != "sk-secret-value"
    assert "sk-secret-value" not in json.dumps(redacted, ensure_ascii=False)

    with_glossary = _job_payload(
        document_id="doc-1",
        input_pdf=tmp_path / "paper.pdf",
        output_dir=tmp_path / "output",
        work_dir=tmp_path / "work",
        api_key="",
        base_url="",
        model="",
        glossary_path=tmp_path / "domain.csv",
    )
    assert with_glossary["glossary"] == str(tmp_path / "domain.csv")


def test_worker_events_are_one_json_line_each_without_credentials():
    from workers.pdfmathtranslate.events import EventWriter

    stream = io.StringIO()
    writer = EventWriter(stream)
    writer.stage_summary([{"name": "Translate Paragraphs", "percent": 100.0}])
    writer.progress(
        "progress_update",
        "Translate Paragraphs",
        {"stage_progress": 50.0, "stage_total": 4, "stage_current": 2},
    )
    writer.finish({"job_id": "doc-1", "translated_pdf": "/out/translated.pdf"})

    lines = [json.loads(line) for line in stream.getvalue().splitlines()]

    assert [line["type"] for line in lines] == ["stage_summary", "progress_update", "finish"]
    assert lines[1]["stage"] == "Translate Paragraphs"
    assert lines[1]["group"] == "translate"
    assert lines[1]["progress"] == 0.5
    assert "api_key" not in stream.getvalue()


def test_stage_groups_tell_the_pipeline_which_stage_a_worker_step_belongs_to():
    from workers.pdfmathtranslate.events import stage_group

    assert stage_group("Parse Page Layout") == "parse"
    assert stage_group("Translate Paragraphs") == "translate"
    assert stage_group("Typesetting") == "render"
    assert {stage_group(name) for name in ("Parse Table", "Add Fonts", "Save PDF")} <= {
        "parse",
        "translate",
        "render",
    }


def test_a_run_publishes_products_and_reports_progress(monkeypatch, tmp_path):
    _fake_worker(monkeypatch, tmp_path, _FAKE_WORKER)
    events: list[dict] = []

    products = _run(tmp_path, on_event=events.append)

    output_dir = tmp_path / "output"
    assert products.translated_pdf == output_dir / "translated.pdf"
    assert products.manifest_path == output_dir / "extraction" / "manifest.json"
    assert products.page_count == 1
    assert products.mode_label.startswith("fake 1.0 · mono")
    assert products.glossary_path is None
    assert [event["type"] for event in events] == [
        "job_started",
        "stage_summary",
        "progress_update",
        "progress_update",
        "progress_end",
        "finish",
    ]
    assert [event["group"] for event in events if "group" in event] == [
        "parse",
        "translate",
        "render",
    ]
    # The key has to reach the worker through its job file, and must not come
    # back out on the event stream.
    assert "sk-secret-value" in (tmp_path / "work" / "job.json").read_text(encoding="utf-8")
    assert "sk-secret-value" not in json.dumps(events, ensure_ascii=False)


def test_a_run_reports_the_stage_a_failing_worker_named(monkeypatch, tmp_path):
    _fake_worker(monkeypatch, tmp_path, _FAILING_WORKER)

    with pytest.raises(WorkerError) as excinfo:
        _run(tmp_path)

    assert str(excinfo.value) == "no translation API key in the job"
    assert excinfo.value.stage == "translate"


def test_a_run_reports_a_worker_that_exits_without_events(monkeypatch, tmp_path):
    _fake_worker(monkeypatch, tmp_path, _CRASHING_WORKER)

    with pytest.raises(WorkerError) as excinfo:
        _run(tmp_path)

    assert "exited with code 3" in str(excinfo.value)
    assert "worker exploded" in str(excinfo.value)
    assert excinfo.value.stage == "parse"


def test_a_run_stops_a_worker_that_overruns_its_budget(monkeypatch, tmp_path):
    monkeypatch.setattr(
        settings,
        "pdfmathtranslate_worker",
        f'{sys.executable} -c "import time; time.sleep(30)"',
    )
    started = time.monotonic()

    with pytest.raises(WorkerError) as excinfo:
        _run(tmp_path, timeout=1)

    assert "budget" in str(excinfo.value)
    assert "1s" in str(excinfo.value)
    assert time.monotonic() - started < 15
    # The handle is released, so the document can be processed again.
    assert cancel_worker("doc-1") is False


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def test_the_working_directory_is_absolute_whatever_the_configuration_says(tmp_path):
    project_root = Path(__file__).resolve().parents[2]
    data_dir = tmp_path / "data"
    cases = {
        "": data_dir / "worker",
        ".": data_dir / "worker",
        "scratch/worker": project_root / "scratch" / "worker",
        str(tmp_path / "explicit"): tmp_path / "explicit",
    }

    for configured, expected in cases.items():
        built = Settings(
            DATA_DIR=str(data_dir), PDFMATHTRANSLATE_WORKING_DIR=configured
        )
        assert built.pdfmathtranslate_working_dir == expected.resolve()
        assert built.pdfmathtranslate_working_dir.is_absolute()


def test_a_relative_worker_interpreter_is_resolved_against_the_project_root():
    """The documented dev runtime lives at <repo>/worker-runtime."""
    project_root = Path(__file__).resolve().parents[2]

    relative = Settings(
        DATA_DIR="/tmp/irrelevant", PDFMATHTRANSLATE_PYTHON="worker-runtime/bin/python3"
    )
    assert relative.pdfmathtranslate_python == str(
        project_root / "worker-runtime" / "bin" / "python3"
    )

    # A bare name is still a PATH lookup, not a path relative to the root.
    assert (
        Settings(
            DATA_DIR="/tmp/irrelevant", PDFMATHTRANSLATE_PYTHON="python3"
        ).pdfmathtranslate_python
        == "python3"
    )
    assert Settings(DATA_DIR="/tmp/irrelevant").pdfmathtranslate_python == "python3"
    assert (
        Settings(
            DATA_DIR="/tmp/irrelevant", PDFMATHTRANSLATE_PYTHON="/opt/worker/bin/python3"
        ).pdfmathtranslate_python
        == "/opt/worker/bin/python3"
    )


def test_the_job_file_and_the_paths_it_names_are_absolute(monkeypatch, tmp_path):
    _fake_worker(monkeypatch, tmp_path, _PATH_REPORTING_WORKER)
    work_dir = tmp_path / "worker-root" / "doc-1"

    products = _run(tmp_path, work_dir=work_dir)

    report = json.loads((work_dir / "paths.json").read_text(encoding="utf-8"))
    assert Path(report["argv"]).is_absolute()
    assert Path(report["argv"]) == work_dir / "job.json"
    assert Path(report["argv"]).is_file()
    assert Path(report["input_pdf"]).is_absolute()
    assert Path(report["output_dir"]).is_absolute()
    assert Path(report["work_dir"]).is_absolute()
    # The child runs from the bundle root, not from wherever the backend was
    # started, so relative job paths would resolve somewhere else entirely.
    assert Path(report["cwd"]) == Path(__file__).resolve().parents[2]
    assert products.translated_pdf == tmp_path / "output" / "translated.pdf"


def test_bracketed_ipv6_in_no_proxy_is_rewritten_for_the_http_client():
    from app.services.pdf_translation_worker import _unbracket_ipv6

    assert _unbracket_ipv6("localhost,127.0.0.1,::1,[::1]") == "localhost,127.0.0.1,::1"
    assert _unbracket_ipv6("[::1]") == "::1"
    assert _unbracket_ipv6("localhost, localhost ,[::1],::1") == "localhost,::1"
    # Anything httpx already parses is left exactly as configured.
    assert _unbracket_ipv6("localhost,127.0.0.1,::1") == "localhost,127.0.0.1,::1"
    assert _unbracket_ipv6("*") == "*"
    assert _unbracket_ipv6("") == ""


def test_the_worker_inherits_a_parsable_no_proxy(monkeypatch, tmp_path):
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1,::1,[::1]")
    monkeypatch.setenv("no_proxy", "localhost,127.0.0.1,::1,[::1]")
    _fake_worker(monkeypatch, tmp_path, _ENV_REPORTING_WORKER)
    work_dir = tmp_path / "worker-root" / "doc-1"

    _run(tmp_path, work_dir=work_dir)

    reported = json.loads((work_dir / "env.json").read_text(encoding="utf-8"))
    assert reported["NO_PROXY"] == "localhost,127.0.0.1,::1"
    assert reported["no_proxy"] == "localhost,127.0.0.1,::1"
    assert reported["PYTHONPATH"] == str(Path(__file__).resolve().parents[2])


# ---------------------------------------------------------------------------
# Stopping a worker
# ---------------------------------------------------------------------------


class _StubProcess:
    """A process handle exposing only the API every platform provides."""

    def __init__(self) -> None:
        self.killed = False
        self.terminated = False
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def wait(self, timeout: float | None = None) -> int | None:
        return self.returncode


def test_kill_forces_the_process_through_the_process_api_only():
    process = _StubProcess()
    run = WorkerRun(process)

    run.kill()

    assert process.killed
    assert not process.terminated


def test_cancel_asks_the_process_to_stop_and_remembers_it():
    process = _StubProcess()
    run = WorkerRun(process)

    run.cancel()

    assert process.terminated
    assert not process.killed
    assert run.cancelled


def test_stopping_a_process_that_already_exited_is_a_no_op():
    process = _StubProcess()
    process.returncode = 0
    run = WorkerRun(process)

    run.kill()
    run.cancel()

    assert not process.killed
    assert not process.terminated


def test_kill_ends_a_worker_that_ignores_termination():
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import signal, time\n"
            "if hasattr(signal, 'SIGTERM'):\n"
            "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "time.sleep(30)\n",
        ]
    )
    try:
        run = WorkerRun(process)

        run.kill()

        assert process.poll() is not None
    finally:
        process.kill()


def test_a_cancelled_run_reports_cancelled_and_publishes_nothing(monkeypatch, tmp_path):
    _fake_worker(monkeypatch, tmp_path, _SLEEPING_WORKER)
    started: list[WorkerRun] = []

    def on_start(run: WorkerRun) -> None:
        started.append(run)
        threading.Timer(0.3, lambda: cancel_worker("doc-1")).start()

    with pytest.raises(WorkerCancelled):
        _run(tmp_path, on_start=on_start)

    assert started and started[0].cancelled
    assert not (tmp_path / "output" / "translated.pdf").exists()
    assert cancel_worker("doc-1") is False


# ---------------------------------------------------------------------------
# Output modes
# ---------------------------------------------------------------------------


def _dual_job(tmp_path, mode: str):
    from workers.pdfmathtranslate.job import job_from_mapping

    return job_from_mapping(
        {
            "job_id": "doc-1",
            "input_pdf": str(tmp_path / "paper.pdf"),
            "output_dir": str(tmp_path / "output"),
            "translation": {"api_key": "k", "base_url": "", "model": "m"},
            "options": {"output": mode},
        }
    )


def _translator_result(tmp_path, *, mono: bool = True, dual: bool = False):
    (tmp_path / "output").mkdir(parents=True, exist_ok=True)
    result = types.SimpleNamespace(
        no_watermark_mono_pdf_path=None,
        mono_pdf_path=None,
        no_watermark_dual_pdf_path=None,
        dual_pdf_path=None,
    )
    if mono:
        path = tmp_path / "output" / "mono.pdf"
        path.write_bytes(b"%PDF-1.7 mono\n")
        result.no_watermark_mono_pdf_path = path
    if dual:
        path = tmp_path / "output" / "dual.pdf"
        path.write_bytes(b"%PDF-1.7 dual\n")
        result.no_watermark_dual_pdf_path = path
    return result


def test_a_dual_job_publishes_the_bilingual_pdf_beside_the_mono_one(tmp_path):
    from workers.pdfmathtranslate.runner import _publish_dual

    job = _dual_job(tmp_path, "dual")
    job.output_dir.mkdir(parents=True, exist_ok=True)

    published = _publish_dual(job, _translator_result(tmp_path, dual=True))

    assert published == job.dual_pdf
    assert published.read_bytes() == b"%PDF-1.7 dual\n"


def test_a_mono_job_publishes_no_bilingual_pdf(tmp_path):
    from workers.pdfmathtranslate.runner import _publish_dual

    job = _dual_job(tmp_path, "mono")
    job.output_dir.mkdir(parents=True, exist_ok=True)

    assert _publish_dual(job, _translator_result(tmp_path)) is None
    assert not job.dual_pdf.exists()


def test_every_mode_that_generates_a_bilingual_pdf_publishes_it(tmp_path):
    from workers.pdfmathtranslate.runner import _publish_dual

    for mode in ("dual", "both"):
        job = _dual_job(tmp_path / mode, mode)
        job.output_dir.mkdir(parents=True, exist_ok=True)

        assert _publish_dual(job, _translator_result(tmp_path / mode, dual=True)) == job.dual_pdf
        assert job.dual_pdf.is_file()


def test_a_dual_job_without_a_bilingual_pdf_fails_instead_of_publishing(tmp_path):
    from workers.pdfmathtranslate.runner import WorkerError as RunnerError
    from workers.pdfmathtranslate.runner import _publish_dual

    job = _dual_job(tmp_path, "dual")
    job.output_dir.mkdir(parents=True, exist_ok=True)

    with pytest.raises(RunnerError) as excinfo:
        _publish_dual(job, _translator_result(tmp_path))

    assert "bilingual" in str(excinfo.value)
    assert excinfo.value.stage == "render"


def _stub_translator_modules(monkeypatch) -> dict:
    """Stand in for the translator so the flags the worker sets can be read."""
    captured: dict = {}

    class _Recorder:
        def __init__(self, **kwargs) -> None:
            captured[self.name] = kwargs

        def validate_settings(self) -> None:
            captured["validated"] = True

    def factory(name: str):
        return type(name, (_Recorder,), {"name": name})

    model = types.ModuleType("pdf2zh_next.config.model")
    model.BasicSettings = factory("basic")
    model.PDFSettings = factory("pdf")
    model.TranslationSettings = factory("translation")
    model.SettingsModel = factory("settings")
    engine = types.ModuleType("pdf2zh_next.config.translate_engine_model")
    engine.OpenAISettings = factory("engine")
    config_pkg = types.ModuleType("pdf2zh_next.config")
    package = types.ModuleType("pdf2zh_next")
    for name, module in (
        ("pdf2zh_next", package),
        ("pdf2zh_next.config", config_pkg),
        ("pdf2zh_next.config.model", model),
        ("pdf2zh_next.config.translate_engine_model", engine),
    ):
        monkeypatch.setitem(sys.modules, name, module)
    return captured


def test_the_translator_is_never_told_to_skip_the_monolingual_pdf(tmp_path, monkeypatch):
    from workers.pdfmathtranslate.runner import _build_settings

    captured = _stub_translator_modules(monkeypatch)

    _build_settings(_dual_job(tmp_path, "mono"))
    assert captured["pdf"]["no_mono"] is False
    assert captured["pdf"]["no_dual"] is True
    assert captured["basic"] == {"debug": True}
    assert captured["validated"] is True

    captured.clear()
    _build_settings(_dual_job(tmp_path, "dual"))
    assert captured["pdf"]["no_mono"] is False
    assert captured["pdf"]["no_dual"] is False


def test_the_translator_does_not_extract_terms_before_translating(tmp_path, monkeypatch):
    """Terms are learned from the finished manifest, not in the wait path."""
    from workers.pdfmathtranslate.runner import _build_settings

    captured = _stub_translator_modules(monkeypatch)

    _build_settings(_dual_job(tmp_path, "mono"))

    assert captured["translation"]["no_auto_extract_glossary"] is True


def test_debug_annotations_are_turned_off_before_the_pdf_is_drawn(monkeypatch):
    from workers.pdfmathtranslate.runner import _suppress_debug_annotations

    drawn = []

    def recorder(name):
        def method(self, *args, **kwargs) -> None:
            drawn.append(name)

        return method

    class _AddDebugInformation:
        def __init__(self, translation_config) -> None:
            self.translation_config = translation_config

        def process(self, docs) -> None:
            drawn.append("add_debug_information")

    midend = "babeldoc.format.pdf.document_il.midend"
    stubs = {
        f"{midend}.add_debug_information": {
            "AddDebugInformation": _AddDebugInformation,
        },
        f"{midend}.layout_parser": {
            "LayoutParser": type(
                "LayoutParser", (), {"_save_debug_box_to_page": recorder("layout")}
            ),
        },
        f"{midend}.detect_scanned_file": {
            "DetectScannedFile": type(
                "DetectScannedFile", (), {"_save_debug_box_to_page": recorder("scanned")}
            ),
        },
        f"{midend}.table_parser": {
            "TableParser": type(
                "TableParser", (), {"_save_debug_box_to_page": recorder("table")}
            ),
        },
    }
    for name, attributes in stubs.items():
        module = types.ModuleType(name)
        for attribute, value in attributes.items():
            setattr(module, attribute, value)
        monkeypatch.setitem(sys.modules, name, module)

    _suppress_debug_annotations()
    _suppress_debug_annotations()

    for attributes in stubs.values():
        for cls in attributes.values():
            if hasattr(cls, "_save_debug_box_to_page"):
                cls._save_debug_box_to_page(types.SimpleNamespace(), object())
    assert drawn == []

    config = types.SimpleNamespace(debug=True)
    _AddDebugInformation(config).process(object())
    assert drawn == []
    assert config.debug is False


def test_the_job_file_carries_the_configured_output_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "pdfmathtranslate_output_mode", "dual")

    payload = _job_payload(
        document_id="doc-1",
        input_pdf=tmp_path / "paper.pdf",
        output_dir=tmp_path / "output",
        work_dir=tmp_path / "work",
        api_key="k",
        base_url="",
        model="m",
        glossary_path=None,
    )

    assert payload["options"]["output"] == "dual"
    assert payload["options"]["no_watermark"] is True


def test_a_dual_product_is_read_from_the_finish_event(tmp_path):
    translated = tmp_path / "translated.pdf"
    translated.write_bytes(b"%PDF-1.7\n")
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    dual = tmp_path / "translated.dual.pdf"
    dual.write_bytes(b"%PDF-1.7\n")
    finish = {
        "translated_pdf": str(translated),
        "manifest_path": str(manifest),
    }

    assert _products_from_finish(finish, tmp_path).dual_pdf is None
    assert _products_from_finish({**finish, "dual_pdf": ""}, tmp_path).dual_pdf is None
    assert _products_from_finish({**finish, "dual_pdf": str(dual)}, tmp_path).dual_pdf == dual

    with pytest.raises(WorkerError) as excinfo:
        _products_from_finish(
            {**finish, "dual_pdf": str(tmp_path / "gone.pdf")}, tmp_path
        )

    assert "bilingual" in str(excinfo.value)

