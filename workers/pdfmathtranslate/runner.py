"""Run one PDFMathTranslate-next job and publish its products.

Flow: build a BabelDOC configuration from the job, translate while forwarding
BabelDOC's progress events, then convert the debug layout it leaves behind into
the stable manifest PaperReader reads, and publish:

* ``<output_dir>/translated.pdf``                     the translated PDF
* ``<output_dir>/translated.dual.pdf``                the bilingual PDF, in dual mode
* ``<output_dir>/extraction/manifest.json``           the stable manifest
* ``<output_dir>/extraction/debug/*.json``            the parse output it came from

Terminology is not extracted here: BabelDOC's automatic pass runs its LLM
sweep before translating and costs more than the translation itself, so the
worker translates with the operator's glossary only, and the backend learns
terms from the finished manifest in the background instead.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from workers.pdfmathtranslate import (
    BABELDOC_VERSION,
    PDFMATHTRANSLATE_VERSION,
)
from workers.pdfmathtranslate.events import EventWriter, stage_group
from workers.pdfmathtranslate.job import Job, JobError, load_job
from workers.pdfmathtranslate.manifest import build_manifest, write_manifest

logger = logging.getLogger("pdfmathtranslate.worker")

_VALID_STAGES = {"parse", "translate", "render"}


class WorkerError(RuntimeError):
    """A failure the worker can describe to the backend in one sentence."""

    def __init__(self, message: str, *, stage: str = "parse") -> None:
        super().__init__(message)
        self.stage = stage if stage in _VALID_STAGES else "parse"


def _sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _python_version_of(module_name: str) -> str:
    try:
        from importlib.metadata import version

        return version(module_name)
    except Exception:  # noqa: BLE001 - the pin is informational only
        return ""


def _build_settings(job: Job):
    """Translate the job into the library's settings model."""
    from pdf2zh_next.config.model import (
        BasicSettings,
        PDFSettings,
        SettingsModel,
        TranslationSettings,
    )
    from pdf2zh_next.config.translate_engine_model import OpenAISettings

    if not job.api_key:
        raise WorkerError("no translation API key in the job", stage="translate")
    if not job.model:
        raise WorkerError("no translation model in the job", stage="translate")

    # PaperReader always publishes the no-watermark monolingual PDF as its
    # translated artifact. A dual request may additionally produce a dual PDF,
    # but it must not disable the mono output that the publish contract reads.
    no_mono = False
    no_dual = job.output_mode == "mono"
    engine = OpenAISettings(
        openai_model=job.model,
        openai_base_url=job.base_url or None,
        openai_api_key=job.api_key,
    )
    if urlsplit(job.base_url).hostname == "api.deepseek.com":
        engine._openai_extra_body = {"thinking": {"type": "disabled"}}
    settings = SettingsModel(
        # The manifest is converted from BabelDOC's debug layout, so debug mode
        # is always on. Whether the worker *keeps* that output is a separate
        # decision, controlled by the job's keep_debug flag.
        basic=BasicSettings(debug=True),
        translation=TranslationSettings(
            lang_in=job.source_lang,
            lang_out=job.target_lang,
            output=str(job.output_dir),
            qps=job.qps,
            glossaries=str(job.glossary_path) if job.glossary_path else None,
            custom_system_prompt=job.custom_system_prompt,
            # The extractor's LLM sweep runs before translating and costs more
            # than the translation itself; the backend learns terms from the
            # finished manifest instead, off the operator's wait path.
            no_auto_extract_glossary=True,
        ),
        pdf=PDFSettings(
            no_mono=no_mono,
            no_dual=no_dual,
            watermark_output_mode="no_watermark" if job.no_watermark else "watermarked",
        ),
        translate_engine_settings=engine,
    )
    settings.validate_settings()
    return settings


def _build_config(job: Job):
    """The BabelDOC configuration for this job, pinned to our own directories."""
    from pdf2zh_next.high_level import create_babeldoc_config

    settings = _build_settings(job)
    config = create_babeldoc_config(settings, job.input_pdf)
    # BabelDOC defaults the working directory to a shared cache location; a job
    # must not depend on, or leak into, another job's state. Its own output goes
    # to scratch as well: only the products the worker publishes are kept.
    # The library only creates the directory when it builds the config itself,
    # so an override has to create it here.
    config.working_dir = job.babeldoc_work_dir / job.input_pdf.stem
    Path(config.working_dir).mkdir(parents=True, exist_ok=True)
    config.output_dir = job.work_dir / "out"
    Path(config.output_dir).mkdir(parents=True, exist_ok=True)
    config.use_rich_pbar = False
    config.progress_monitor = None
    return config


def _translate(job: Job, writer: EventWriter):
    """Run BabelDOC to completion, forwarding its progress.

    BabelDOC's async wrapper ends on a handshake between the progress monitor
    and the event loop that never resolves when the monitor belongs to the
    caller, so the worker drives the synchronous entry point with its own
    monitor instead.
    """
    from babeldoc.format.pdf.high_level import do_translate, get_translation_stage
    from babeldoc.progress_monitor import ProgressMonitor

    config = _build_config(job)
    _suppress_debug_annotations()

    def on_progress(**event: Any) -> None:
        kind = str(event.get("type") or "")
        if kind == "stage_summary":
            writer.stage_summary(list(event.get("stages") or []))
        elif kind in {"progress_start", "progress_update", "progress_end"}:
            writer.progress(kind, str(event.get("stage") or ""), event)
        elif kind == "error":
            error = event.get("error")
            raise WorkerError(
                f"{type(error).__name__}: {error}"
                if isinstance(error, BaseException)
                else str(error or "translation failed"),
                stage=_error_stage(event),
            )

    with ProgressMonitor(
        get_translation_stage(config),
        progress_change_callback=on_progress,
    ) as monitor:
        result = do_translate(monitor, config)
    if result is None:
        raise WorkerError("translation finished without a result", stage="render")
    return result, config


def _error_stage(event: dict) -> str:
    stage = str(event.get("stage") or "")
    return stage_group(stage) if stage else "translate"


def _suppress_debug_annotations() -> None:
    """Keep BabelDOC's debug drawing out of the published PDF.

    The worker runs BabelDOC in debug mode because the manifest is converted
    from the debug layout dumps, but the same flag also makes several midend
    passes draw into the document itself: the layout, table and scanned-page
    passes each stamp green class-name labels onto the page, and
    ``AddDebugInformation`` frames every paragraph and curve. The labels
    render unconditionally, so those passes are patched to no-ops.
    ``AddDebugInformation`` is the last midend pass — all the debug JSON has
    been written by the time it runs — so it is replaced with one that only
    turns debug mode off, and the typesetting and PDF rendering that follow
    add no rectangles, labels or curves.
    """
    from babeldoc.format.pdf.document_il.midend.add_debug_information import (
        AddDebugInformation,
    )
    from babeldoc.format.pdf.document_il.midend.detect_scanned_file import (
        DetectScannedFile,
    )
    from babeldoc.format.pdf.document_il.midend.layout_parser import LayoutParser
    from babeldoc.format.pdf.document_il.midend.table_parser import TableParser

    def draw_nothing(self, *args, **kwargs) -> None:
        return None

    LayoutParser._save_debug_box_to_page = draw_nothing
    DetectScannedFile._save_debug_box_to_page = draw_nothing
    TableParser._save_debug_box_to_page = draw_nothing

    def turn_debug_off(self, docs) -> None:
        self.translation_config.debug = False

    AddDebugInformation.process = turn_debug_off


def _mono_pdf(result) -> Path:
    """The translated PDF to publish, preferring the watermark-free output."""
    for attribute in ("no_watermark_mono_pdf_path", "mono_pdf_path"):
        candidate = getattr(result, attribute, None)
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    raise WorkerError(
        "the translator produced no monolingual PDF", stage="render"
    )


def _publish_dual(job: Job, result) -> Path | None:
    """Publish the bilingual PDF when the job asked for one.

    The monolingual PDF is always the document's translated artifact; a job that
    also asked for the bilingual one additionally registers this, so a requested
    output mode never ends up as work nobody can read.
    """
    if job.output_mode == "mono":
        return None
    source = None
    for attribute in ("no_watermark_dual_pdf_path", "dual_pdf_path"):
        candidate = getattr(result, attribute, None)
        if candidate and Path(candidate).is_file():
            source = Path(candidate)
            break
    if source is None:
        raise WorkerError(
            "the translator produced no bilingual PDF for a dual job", stage="render"
        )
    target = job.dual_pdf
    if source.resolve() != target.resolve():
        shutil.copyfile(source, target)
    return target


def _publish_debug(job: Job, debug_dir: Path) -> dict:
    """Keep the translator's own layout output when the job asks for it.

    It is what the manifest was converted from, and it is large — hundreds of
    megabytes for a long paper — so it is off by default and the manifest is
    the thing that persists.
    """
    if not job.keep_debug:
        return {"debug_files": []}
    debug_out = job.extraction_dir / "debug"
    debug_out.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for source in sorted(debug_dir.glob("*.json")):
        if source.is_file():
            shutil.copyfile(source, debug_out / source.name)
            copied.append(source.name)
    return {"debug_files": copied}


def run(job: Job, writer: EventWriter) -> dict:
    """Execute the job and return the payload of the ``finish`` event."""
    if not job.input_pdf.is_file():
        raise WorkerError(f"input PDF not found: {job.input_pdf}", stage="parse")

    job.output_dir.mkdir(parents=True, exist_ok=True)
    job.extraction_dir.mkdir(parents=True, exist_ok=True)
    job.babeldoc_work_dir.mkdir(parents=True, exist_ok=True)

    logger.info("worker job %s: %s", job.job_id, job.input_pdf.name)
    try:
        result, _config = _translate(job, writer)

        translated = _mono_pdf(result)
        target = job.translated_pdf
        if translated.resolve() != target.resolve():
            shutil.copyfile(translated, target)

        dual_target = _publish_dual(job, result)

        debug_dir = job.babeldoc_work_dir / job.input_pdf.stem

        mode_label = (
            f"PDFMathTranslate-next {PDFMATHTRANSLATE_VERSION} · "
            f"{job.output_mode}"
            f"{' · 无水印' if job.no_watermark else ''}"
        )
        manifest = build_manifest(
            debug_dir,
            source_pdf=job.input_pdf,
            source_sha256=_sha256(job.input_pdf),
            mode_label=mode_label,
            generator={
                "worker": "pdfmathtranslate",
                "pdfmathtranslate_version": PDFMATHTRANSLATE_VERSION,
                "babeldoc_version": BABELDOC_VERSION,
                "installed_babeldoc_version": _python_version_of("babeldoc"),
                "source_lang": job.source_lang,
                "target_lang": job.target_lang,
                "output_mode": job.output_mode,
                "no_watermark": job.no_watermark,
            },
        )
        write_manifest(job.manifest_path, manifest)
        debug_info = _publish_debug(job, debug_dir)
    finally:
        _clean_work_dir(job)

    payload = {
        "job_id": job.job_id,
        "translated_pdf": str(target),
        "mono_pdf_path": str(getattr(result, "mono_pdf_path", "") or ""),
        "no_watermark_mono_pdf_path": str(
            getattr(result, "no_watermark_mono_pdf_path", "") or ""
        ),
        "dual_pdf": str(dual_target) if dual_target else "",
        "manifest_path": str(job.manifest_path),
        "debug_dir": str(job.extraction_dir / "debug"),
        "extraction_dir": str(job.extraction_dir),
        "mode_label": mode_label,
        "page_count": int(manifest.get("page_count") or 0),
        **debug_info,
    }
    return payload


def _clean_work_dir(job: Job) -> None:
    """The scratch directory holds hundreds of megabytes of debug output."""
    if job.keep_debug or job.work_dir == job.output_dir:
        return
    shutil.rmtree(job.work_dir, ignore_errors=True)


def _configure_logging() -> None:
    """Logs go to stderr; stdout carries events only."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)
    for noisy in ("httpx", "httpcore", "openai", "urllib3", "pdfminer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _run_job(job_path: str) -> int:
    """Run one job file, reporting on stdout. Returns a process-style code."""
    writer = EventWriter()
    try:
        job = load_job(job_path)
    except JobError as exc:
        writer.error(str(exc))
        return 2

    try:
        payload = run(job, writer)
    except WorkerError as exc:
        logger.error("worker job %s failed: %s", job.job_id, exc)
        writer.error(str(exc), stage=exc.stage)
        return 1
    except KeyboardInterrupt:
        logger.warning("worker job %s cancelled", job.job_id)
        writer.error("worker job cancelled", stage="parse")
        return 130
    except BaseException as exc:  # noqa: BLE001 - reported, never swallowed
        logger.error("worker job %s crashed: %s", job.job_id, exc)
        logger.debug("%s", traceback.format_exc())
        writer.error(f"{type(exc).__name__}: {exc}", stage="parse")
        return 1

    writer.finish(payload)
    return 0


# An idle worker exits on its own, so an unused one does not hold the models
# resident forever; the backend simply starts a new one when a job arrives.
_SERVE_IDLE_SECONDS = 900


def _serve() -> int:
    """Run jobs arriving on stdin, one job-file path per line, until EOF.

    Staying alive between jobs skips the interpreter and model startup on
    every run after the first — a sizable fraction of a short paper's wait.
    """
    state = {"busy": False, "touched": time.monotonic()}

    def exit_when_idle() -> None:
        while True:
            time.sleep(30)
            idle_for = time.monotonic() - state["touched"]
            if not state["busy"] and idle_for > _SERVE_IDLE_SECONDS:
                os._exit(0)

    threading.Thread(target=exit_when_idle, daemon=True).start()

    logger.info("worker ready (serve mode)")
    for line in sys.stdin:
        job_path = line.strip()
        if not job_path:
            continue
        state["busy"] = True
        try:
            _run_job(job_path)
        finally:
            state["busy"] = False
            state["touched"] = time.monotonic()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pdfmathtranslate-worker",
        description="Translate one PDF and publish a PaperReader manifest.",
    )
    parser.add_argument("job", nargs="?", help="path to the job JSON file")
    parser.add_argument(
        "--serve",
        action="store_true",
        help="keep running and read job JSON paths from stdin, one per line",
    )
    args = parser.parse_args(argv)

    _configure_logging()
    if args.serve:
        return _serve()
    if not args.job:
        parser.error("a job file is required unless --serve is given")
    return _run_job(args.job)


if __name__ == "__main__":
    raise SystemExit(main())
