"""Document pipeline: upload → PDFMathTranslate-next worker → reader artifacts.

The translation itself happens in the worker process (see
:mod:`app.services.pdf_translation_worker`). This module owns everything around
it: document state, stage reporting, artifact registration, the reader's
structures (outline, figures, references, bilingual alignment, annotated PDF)
and reading progress.
"""

import csv
import re
import shutil
import uuid
from pathlib import Path

from app.core.config import settings
from app.models.store import (
    ArtifactEntry,
    DocumentRecord,
    FailureEntry,
    annotated_pdf_filename,
    dual_pdf_filename,
    merged_pdf_filename,
    save_document,
    set_document_metadata,
    translated_pdf_filename,
)
from app.services.alignment_service import save_exact_alignment
from app.services.annotation_render import ANNOTATION_REVISION, render_annotated_pdf
from app.services.app_settings import AppSettings
from app.services.document_ir import Block, Title
from app.services.document_manifest import (
    DocumentManifest,
    PageGeometry,
    load_document_manifest,
)
from app.services.glossary_service import glossary_terms_for_prompt
from app.services.pdf_extraction import PdfTranslationResult
from app.services.pdf_ops import build_side_by_side_pdf
from app.services.pdf_translation_worker import WorkerCancelled, WorkerError, run_worker
from app.services.stage_tracker import (
    init_stages,
    prepare_stages_for_retry,
    set_stage_progress,
    with_stage,
)
from app.services.term_extraction import schedule_extraction
from app.services.translation_prompts import DOMAINS, normalize_domain

_TITLE_H1_PATTERN = re.compile(r"(?m)^#\s+(.+)$")
_NOUGAT_MISSING_PAGE_PATTERN = re.compile(r"^\s*\[MISSING_PAGE[^\]]*\]\s*$", re.MULTILINE)

MANIFEST_KIND = "manifest"
GLOSSARY_KIND = "glossary"


# ---------------------------------------------------------------------------
# Artifact helpers
# ---------------------------------------------------------------------------


def _to_data_url(path: Path) -> str | None:
    try:
        rel = path.resolve().relative_to(settings.data_dir.resolve())
    except ValueError:
        return None
    return "/data/" + str(rel).replace("\\", "/")


def _append_artifact(
    record: DocumentRecord, name: str, kind: str, path: Path, revision: int = 0
) -> None:
    for artifact in record.artifacts:
        if artifact.kind == kind and Path(artifact.path) == path:
            artifact.name = name
            artifact.url = _to_data_url(path)
            artifact.revision = revision
            return
    record.artifacts.append(
        ArtifactEntry(
            name=name,
            kind=kind,
            path=str(path),
            url=_to_data_url(path),
            revision=revision,
        )
    )


def _drop_artifacts(record: DocumentRecord, *kinds: str) -> None:
    record.artifacts = [
        artifact for artifact in record.artifacts if artifact.kind not in kinds
    ]


def _publish_translated_pdf(
    record: DocumentRecord, compiled_pdf: Path, output_dir: Path
) -> Path:
    """Publish a translated PDF using the source-derived download name."""
    name = translated_pdf_filename(record.source_filename)
    output = output_dir / name
    output.parent.mkdir(parents=True, exist_ok=True)
    if compiled_pdf.resolve() != output.resolve():
        shutil.copyfile(compiled_pdf, output)
        compiled_pdf.unlink(missing_ok=True)
    record.translated_pdf_url = _to_data_url(output)
    _append_artifact(record, name, "translated_pdf", output)
    return output


def _publish_dual_pdf(
    record: DocumentRecord, dual_pdf: Path, output_dir: Path
) -> None:
    """Publish the bilingual PDF a dual-mode run produced alongside the mono one."""
    name = dual_pdf_filename(record.source_filename)
    output = output_dir / name
    output.parent.mkdir(parents=True, exist_ok=True)
    if dual_pdf.resolve() != output.resolve():
        shutil.copyfile(dual_pdf, output)
        dual_pdf.unlink(missing_ok=True)
    _append_artifact(record, name, "dual_pdf", output)


def _publish_merged_pdf(
    record: DocumentRecord, translated_pdf: Path, output_dir: Path
) -> None:
    """Stitch source and translated pages into one side-by-side PDF.

    The merged file is what the reader displays, but a failure here must not
    fail an otherwise translated document: a missing merge is rebuilt on
    demand when the document opens.
    """
    target = output_dir / merged_pdf_filename(record.source_filename)
    try:
        build_side_by_side_pdf(record.source_path, translated_pdf, target)
    except Exception as exc:  # noqa: BLE001 - never block the pipeline on this
        record.logs.append(f"Merged PDF skipped: {exc}")
        return
    _append_artifact(record, target.name, "merged_pdf", target)


def build_merged_pdf(record: DocumentRecord) -> str | None:
    """Build the side-by-side PDF for a document that predates the feature."""
    if not record.source_path.is_file():
        return None
    translated = next(
        (item for item in record.artifacts if item.kind == "translated_pdf"), None
    )
    if translated is None or not Path(translated.path).is_file():
        return None
    output_dir = settings.output_dir / record.document_id
    output_dir.mkdir(parents=True, exist_ok=True)
    _publish_merged_pdf(record, Path(translated.path), output_dir)
    save_document(record)
    artifact = next(
        (item for item in record.artifacts if item.kind == "merged_pdf"), None
    )
    return artifact.url if artifact else None


def _publish_annotated_pdf(
    record: DocumentRecord,
    blocks: list[Block],
    pages: list[PageGeometry],
    output_dir: Path,
) -> None:
    """Box every parsed region on the source pages and publish the result.

    Annotation is a side artifact: a failure here is logged and never fails the
    document.
    """
    target = output_dir / annotated_pdf_filename(record.source_filename)
    try:
        drawn = render_annotated_pdf(
            source_pdf=record.source_path,
            pages=pages,
            blocks=blocks,
            output_pdf=target,
        )
    except Exception as exc:  # noqa: BLE001 - never block the pipeline on this
        record.logs.append(f"Annotated PDF skipped: {exc}")
        return
    _append_artifact(
        record, target.name, "annotated_pdf", target, revision=ANNOTATION_REVISION
    )
    record.logs.append(f"Annotated {drawn} parsed region(s) on the source pages")


def build_annotated_pdf(record: DocumentRecord) -> str | None:
    """Rebuild the annotated source PDF from the document's manifest."""
    if not record.source_path.is_file():
        return None
    manifest = _load_manifest(record)
    if manifest is None or not manifest.blocks:
        return None
    output_dir = settings.output_dir / record.document_id
    _publish_annotated_pdf(record, manifest.blocks, manifest.pages, output_dir)
    save_document(record)
    artifact = next(
        (item for item in record.artifacts if item.kind == "annotated_pdf"), None
    )
    return artifact.url if artifact else None


def _load_manifest(record: DocumentRecord) -> DocumentManifest | None:
    path = settings.output_dir / record.document_id / "extraction" / "manifest.json"
    if not path.is_file():
        return None
    try:
        return load_document_manifest(path)
    except Exception as exc:  # noqa: BLE001 - a broken manifest is a missing one
        record.logs.append(f"Manifest unreadable: {exc}")
        return None


# ---------------------------------------------------------------------------
# Document metadata
# ---------------------------------------------------------------------------


def _clean_extracted_text(text: str) -> str:
    cleaned = _NOUGAT_MISSING_PAGE_PATTERN.sub("", text or "")
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def _display_title(manifest: DocumentManifest, source_filename: str) -> str:
    for block in manifest.blocks:
        if isinstance(block, Title):
            text = (block.source_text or block.text).strip()
            if text:
                return text
    matched = _TITLE_H1_PATTERN.search(manifest.markdown("source"))
    if matched:
        return matched.group(1).strip()
    return Path(source_filename).stem.strip()


def _enrich_metadata(record: DocumentRecord, display_title: str) -> None:
    """Best-effort Semantic Scholar lookup for title/authors/year/venue.

    Failure or a lookup miss leaves the document untouched; the pipeline never
    depends on this succeeding.
    """
    # `record.metadata` also carries pipeline bookkeeping, so it is the paper
    # fields that decide whether a lookup already happened.
    if record.metadata.get("title") or record.metadata.get("authors"):
        return
    try:
        from app.services.paper_metadata import fetch_paper_metadata

        metadata = fetch_paper_metadata(display_title)
    except Exception:
        metadata = {}
    if not metadata:
        return
    record.metadata.update(metadata)
    set_document_metadata(record.document_id, record.metadata)
    record.logs.append(f"Metadata enriched: {str(metadata.get('title', ''))[:80]}")


# ---------------------------------------------------------------------------
# Glossary
# ---------------------------------------------------------------------------


def _write_domain_glossary(domain: str, work_dir: Path) -> Path | None:
    """Render the operator's domain glossary in the worker's CSV format.

    The terms the operator curated are what the translator should honour, so
    they travel with the job instead of being applied after the fact. The
    worker matches them against each paragraph itself, so the whole library
    goes in — not just the most frequent few.
    """
    terms = glossary_terms_for_prompt(domain, limit=None)
    if not terms:
        return None
    work_dir.mkdir(parents=True, exist_ok=True)
    target = work_dir / f"domain-{domain}.csv"
    with target.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["source", "target"])
        for source, translated in terms:
            writer.writerow([source, translated])
    return target


# ---------------------------------------------------------------------------
# Stage reporting across the worker's own stages
# ---------------------------------------------------------------------------


class _StageSwitcher:
    """Open one pipeline stage at a time, closing the previous one first.

    The worker reports its own stages (parse, translate, render) while it runs,
    so the pipeline follows them instead of holding a single stage open for the
    whole translation.
    """

    def __init__(self, record: DocumentRecord) -> None:
        self._record = record
        self._cm = None

    def switch(self, key: str) -> None:
        if self._cm is not None:
            self._cm.__exit__(None, None, None)
        self._cm = with_stage(self._record, key)
        self._cm.__enter__()

    def close(self) -> None:
        if self._cm is not None:
            self._cm.__exit__(None, None, None)
            self._cm = None

    def fail(self) -> None:
        if self._cm is not None:
            self._cm.__exit__(RuntimeError, RuntimeError("stage failed"), None)
            self._cm = None


def _event_reporter(record: DocumentRecord, switcher: _StageSwitcher):
    """Map worker events onto the document's stage list."""
    state = {"stage": "", "fraction": -1.0}

    def report(event: dict) -> None:
        kind = str(event.get("type") or "")
        if kind == "stage_summary":
            return
        # The worker names the BabelDOC stage it is in through ``stage`` and the
        # pipeline stage that belongs to through ``group``.
        group = str(event.get("group") or "")
        stage = _stage_key(group or str(event.get("stage") or ""))
        if stage != state["stage"]:
            state["stage"] = stage
            state["fraction"] = -1.0
            switcher.switch(stage)
        if kind == "progress_start":
            return
        if kind in {"progress_update", "progress_end"}:
            fraction = float(event.get("progress") or 0.0)
            # The worker reports every 0.1s; a write per event would serialize
            # the pipeline on SQLite.
            if kind == "progress_end" or fraction - state["fraction"] >= 0.01:
                state["fraction"] = fraction
                set_stage_progress(record, stage, fraction, _stage_label(stage, event))

    return report


def _stage_key(group: str) -> str:
    return group if group in {"parse", "translate", "render"} else "parse"


# User-facing names for the worker's internal (BabelDOC) progress stages.
_SUB_STAGE_LABELS = {
    "Parse PDF and Create Intermediate Representation": "读取文档结构",
    "DetectScannedFile": "检测扫描件",
    "Parse Page Layout": "分析页面布局",
    "Parse Table": "识别表格",
    "Parse Paragraphs": "解析段落",
    "Parse Formulas and Styles": "解析公式与样式",
    "Remove Char Descent": "清理字符",
    "Automatic Term Extraction": "提取术语",
    "Translate Paragraphs": "翻译段落",
    "Typesetting": "合成版式",
    "Add Fonts": "嵌入字体",
    "Generate drawing instructions": "生成绘图指令",
    "Subset font": "子集化字体",
    "Save PDF": "保存文件",
}


def _stage_label(group: str, event: dict) -> str:
    name = str(event.get("stage") or "")
    label = _SUB_STAGE_LABELS.get(name) or {
        "parse": "解析",
        "translate": "翻译",
        "render": "排版",
    }.get(group, "处理")
    total = int(event.get("total") or 0)
    current = int(event.get("current") or 0)
    if total:
        return f"{label} {current}/{total}"
    return label


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


def create_document_record(source_path: Path, source_type: str = "pdf") -> DocumentRecord:
    document_id = str(uuid.uuid4())
    source_filename = source_path.name.split("_", 1)[-1] if "_" in source_path.name else source_path.name
    record = DocumentRecord(
        document_id=document_id,
        source_type=source_type,
        source_path=source_path,
        source_filename=source_filename,
    )
    return save_document(record)


def _register_source_artifacts(record: DocumentRecord, output_dir: Path) -> None:
    original_out = output_dir / "original.pdf"
    shutil.copyfile(record.source_path, original_out)
    record.original_pdf_url = f"/data/outputs/{record.document_id}/original.pdf"
    _append_artifact(record, "original.pdf", "original_pdf", original_out)
    _append_artifact(record, record.source_path.name, "source_pdf", record.source_path)


def _register_extraction_artifacts(
    record: DocumentRecord, result: PdfTranslationResult
) -> None:
    _append_artifact(record, result.manifest_path.name, MANIFEST_KIND, result.manifest_path)
    if result.glossary_path is not None:
        _append_artifact(record, result.glossary_path.name, GLOSSARY_KIND, result.glossary_path)


def process_document(
    record: DocumentRecord,
    override_api_key: str | None = None,
    override_base_url: str | None = None,
    override_model: str | None = None,
    provider_settings: AppSettings | None = None,
    resume_from: str | None = None,
) -> DocumentRecord:
    if provider_settings is not None:
        override_api_key = provider_settings.api_key
        override_base_url = provider_settings.base_url
        override_model = provider_settings.model
    translation_domain = normalize_domain(
        provider_settings.translation_domain if provider_settings else None
    )
    record.status = "processing"
    record.logs.append(
        f"Retry processing started from {resume_from}" if resume_from else "Processing started"
    )
    # The worker translates the whole document in one go, so a retry always
    # starts over: there is no partial parse or translation to reuse.
    if record.stages and resume_from:
        prepare_stages_for_retry(record, "parse")
    else:
        init_stages(record)
    save_document(record)
    try:
        record.size_bytes = record.source_path.stat().st_size
    except OSError:
        record.size_bytes = 0

    output_dir = settings.output_dir / record.document_id
    output_dir.mkdir(parents=True, exist_ok=True)
    record.logs.append(f"Output dir: {output_dir}")
    work_dir = settings.pdfmathtranslate_working_dir / record.document_id

    switcher = _StageSwitcher(record)
    try:
        if not resume_from:
            with with_stage(record, "upload"):
                pass
        _drop_artifacts(
            record, MANIFEST_KIND, GLOSSARY_KIND, "annotated_pdf", "dual_pdf", "merged_pdf"
        )

        glossary_csv = _write_domain_glossary(translation_domain, work_dir)
        try:
            products = run_worker(
                document_id=record.document_id,
                input_pdf=record.source_path,
                output_dir=output_dir,
                work_dir=work_dir,
                api_key=override_api_key or "",
                base_url=override_base_url or settings.openai_base_url,
                model=override_model or settings.openai_model,
                glossary_path=glossary_csv,
                on_event=_event_reporter(record, switcher),
            )
        except Exception:
            switcher.fail()
            raise
        switcher.close()
        result = PdfTranslationResult.from_worker(record.source_path, products)
        record.translated_pdf_url = None
        translated_output = _publish_translated_pdf(record, result.translated_pdf, output_dir)
        if result.dual_pdf is not None:
            _publish_dual_pdf(record, result.dual_pdf, output_dir)
        _publish_merged_pdf(record, translated_output, output_dir)
        _register_source_artifacts(record, output_dir)
        _register_extraction_artifacts(record, result)
        record.logs.append(f"Extraction model: {result.mode_label}")
        record.logs.append(f"Extraction dir: {result.extraction_dir}")

        manifest = result.manifest()
        with with_stage(record, "clean"):
            _build_reader_state(record, manifest, translation_domain, output_dir)

        record.status = "done"
        record.failure = None
        record.logs.append("Processing done")
        # Terminology is learned off the wait path: the worker already
        # translated with the curated glossary, and a background pass now
        # extracts candidates from the manifest into the pending pool.
        schedule_extraction(
            document_id=record.document_id,
            manifest=manifest,
            domain=translation_domain,
            api_key=override_api_key or "",
            base_url=override_base_url or settings.openai_base_url,
            model=override_model or settings.openai_model,
        )
    except WorkerCancelled as exc:
        switcher.fail()
        _cancel(record, str(exc))
    except WorkerError as exc:
        switcher.fail()
        _fail(record, exc.stage, str(exc), resume_from)
    except Exception as exc:  # noqa: BLE001 - every failure becomes document state
        switcher.fail()
        stage = record.current_stage or resume_from or "upload"
        _fail(record, stage, str(exc), resume_from)
    return save_document(record)


def _fail(
    record: DocumentRecord, stage: str, message: str, resume_from: str | None
) -> None:
    chunk_match = re.search(r"chunk\s+(\d+)", message, re.IGNORECASE)
    record.status = "failed"
    record.failure = FailureEntry(
        stage=stage or resume_from or "upload",
        message=message,
        retryable=record.source_path.is_file(),
        chunk=int(chunk_match.group(1)) if chunk_match else None,
        retry_count=record.retry_count,
    )
    record.logs.append(f"Error: {message}")


def _cancel(record: DocumentRecord, message: str) -> None:
    record.status = "cancelled"
    record.failure = None
    record.current_stage = None
    record.current_stage_label = None
    record.logs.append(f"Cancelled: {message}")


def _build_reader_state(
    record: DocumentRecord,
    manifest: DocumentManifest,
    domain: str,
    output_dir: Path,
) -> None:
    """Turn the manifest into everything the reader reads."""
    record.logs.append(
        f"Parsed {len(manifest.blocks)} block(s) across {manifest.page_count} page(s)"
    )
    if not manifest.blocks:
        raise RuntimeError(
            "The translator returned an empty structure, so no readable document "
            "could be built."
        )
    source_markdown = _clean_extracted_text(manifest.markdown("source"))
    if not source_markdown:
        raise RuntimeError(
            "No readable text could be extracted from this PDF. It may be an "
            "image-only PDF the translator could not read."
        )
    record.extracted_text = source_markdown
    record.translated_text = _clean_extracted_text(manifest.markdown("translated"))
    record.logs.append(f"Translation domain: {DOMAINS[domain].label}")

    display_title = _display_title(manifest, record.source_filename)
    if display_title == Path(record.source_filename).stem.strip():
        record.logs.append("Title fallback applied from source filename")

    record.references = manifest.references
    record.logs.append(f"References extracted: {len(record.references)}")
    _enrich_metadata(record, display_title)

    pairs = manifest.alignment_pairs()
    if pairs:
        sources = [source for source, _ in pairs]
        translated = [text for _, text in pairs]
        alignment_path = save_exact_alignment(record, sources, translated)
        if alignment_path:
            _append_artifact(record, alignment_path.name, "alignment_index", alignment_path)
            record.logs.append(
                f"Saved {len(pairs)} exact bilingual alignment segments"
            )

    _publish_annotated_pdf(record, manifest.blocks, manifest.pages, output_dir)
    save_document(record)
