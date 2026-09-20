import hashlib
import json
import re
import shutil
import uuid
from collections import Counter
from pathlib import Path

from app.core.config import settings
from app.models.store import (
    ArtifactEntry,
    DocumentRecord,
    FailureEntry,
    ReferenceEntry,
    annotated_pdf_filename,
    save_document,
    set_document_metadata,
    translated_pdf_filename,
)
from app.services.annotation_render import ANNOTATION_REVISION, render_annotated_pdf
from app.services.cjk_fonts import require_cjk_font
from app.services.layout_fit import TextMeasurer, plan_document
from app.services.layout_model import (
    PageFrame,
    align_blocks_to_text_layer,
    attach_inline_formula_boxes,
    attach_known_regions,
    measure_pages,
    merge_known_regions,
    pages_from_middle,
    synthesize_unclaimed_paragraphs,
)
from app.services.layout_render import prepare_formula_crops, render_document
from app.services.mineru_layout import (
    Image as IRImage,
    ListBlock as IRListBlock,
    Paragraph as IRParagraph,
    TextRun as IRTextRun,
    Title as IRTitle,
    blocks_to_ir,
    collect_translatable_strings,
    merge_continuation_groups,
    plan_continuation_groups,
    split_continuation_groups,
)
from app.services.alignment_service import save_exact_alignment
from app.services.mineru_service import (
    MinerUConfig,
    MinerUResult,
    extract_structured_from_pdf,
    extract_structured_from_pdf_local,
    extract_text_from_pdf_text_layer,
)
from app.services.stage_tracker import (
    init_stages,
    prepare_stages_for_retry,
    set_stage_progress,
    with_stage,
)
from app.services.translate_service import (
    extract_document_terms,
    translate_concise,
    translate_ir,
)
from app.services.glossary_service import (
    glossary_terms_for_prompt,
    record_candidate_terms,
)
from app.services.translation_prompts import (
    DOMAINS,
    format_glossary_context,
    merge_glossary_terms,
    normalize_domain,
)
from app.services.vision_check_service import run_vision_check_on_markdown
from app.services.app_settings import AppSettings

_REFERENCE_SPLIT_PATTERN = re.compile(r"(?im)^\s*(references|bibliography)\s*$")
_REFERENCE_ITEM_PATTERN = re.compile(r"^\s*(\[\d+\]|\d+\.|\d+\))\s+(.+)")
_NOUGAT_MISSING_PAGE_PATTERN = re.compile(r"^\s*\[MISSING_PAGE[^\]]*\]\s*$", re.MULTILINE)
_TITLE_H1_PATTERN = re.compile(r"(?m)^#\s+(.+)$")


def _normalize_for_alignment(text: str) -> tuple[str, list[int]]:
    normalized_chars: list[str] = []
    index_map: list[int] = []
    previous_was_space = True

    for idx, char in enumerate(text):
        if char.isalnum():
            normalized_chars.append(char.lower())
            index_map.append(idx)
            previous_was_space = False
            continue

        if char.isspace() and not previous_was_space and normalized_chars:
            normalized_chars.append(" ")
            index_map.append(idx)
            previous_was_space = True

    if normalized_chars and normalized_chars[-1] == " ":
        normalized_chars.pop()
        index_map.pop()

    return "".join(normalized_chars), index_map


def _recover_missing_leading_text(primary_text: str, fallback_text: str) -> tuple[str, bool]:
    if not primary_text.strip() or not fallback_text.strip():
        return primary_text, False

    normalized_primary, primary_map = _normalize_for_alignment(primary_text)
    normalized_fallback, fallback_map = _normalize_for_alignment(fallback_text)
    anchor_len = min(80, len(normalized_primary) // 2, len(normalized_fallback) // 2)
    anchor_len = max(anchor_len, 24)
    min_leading_chars = max(24, anchor_len // 2)
    if len(normalized_primary) < anchor_len or len(normalized_fallback) < anchor_len:
        return primary_text, False

    search_limit = min(len(normalized_primary) - anchor_len, 1200)
    for primary_offset in range(0, search_limit + 1, 60):
        anchor = normalized_primary[primary_offset : primary_offset + anchor_len].strip()
        if len(anchor) < anchor_len // 2:
            continue

        fallback_offset = normalized_fallback.find(anchor)
        if fallback_offset == -1:
            continue
        if fallback_offset < min_leading_chars:
            return primary_text, False

        raw_primary_start = primary_map[primary_offset]
        raw_fallback_end = fallback_map[fallback_offset]
        leading_prefix = fallback_text[:raw_fallback_end].strip()
        if len(leading_prefix) < min_leading_chars:
            return primary_text, False

        merged = f"{leading_prefix}\n\n{primary_text[raw_primary_start:].lstrip()}"
        return merged.strip(), True

    return primary_text, False


def _clean_nougat_text_with_metadata(text: str, leading_fallback_text: str = "") -> tuple[str, int, bool]:
    """Strip extraction artifacts that would corrupt downstream stages.

    Removes missing-page markers, recovers a leading section the primary text
    lost (matched against the PDF's embedded text layer), and collapses
    blank-line runs.
    """
    missing_page_count = len(_NOUGAT_MISSING_PAGE_PATTERN.findall(text))
    cleaned = _NOUGAT_MISSING_PAGE_PATTERN.sub("", text)
    cleaned, recovered_leading = _recover_missing_leading_text(cleaned, leading_fallback_text)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip(), missing_page_count, recovered_leading


def _derive_display_title(source_filename: str, extracted_text: str) -> tuple[str, bool]:
    matched = _TITLE_H1_PATTERN.search(extracted_text)
    if matched:
        return matched.group(1).strip(), False
    return Path(source_filename).stem.strip(), True


def _enrich_metadata(record: DocumentRecord, display_title: str) -> None:
    """Best-effort Semantic Scholar lookup for title/authors/year/venue.

    Failure or a lookup miss leaves the document untouched; the pipeline
    never depends on this succeeding.
    """
    if record.metadata:
        return
    try:
        from app.services.paper_metadata import fetch_paper_metadata

        metadata = fetch_paper_metadata(display_title)
    except Exception:
        metadata = {}
    if not metadata:
        return
    record.metadata = metadata
    set_document_metadata(record.document_id, metadata)
    record.logs.append(f"Metadata enriched: {str(metadata.get('title', ''))[:80]}")


def _ir_to_translated_markdown(ir_blocks: list) -> str:
    """Render a (translated) IR list back into a lightweight Markdown string
    so it can be used by chat / preview surfaces that expect plain text."""
    parts: list[str] = []
    for block in ir_blocks:
        if isinstance(block, IRTitle):
            hashes = "#" * max(1, min(block.level, 6))
            parts.append(f"{hashes} {block.text}")
        elif isinstance(block, IRParagraph):
            text_runs: list[str] = []
            for run in block.runs:
                if isinstance(run, IRTextRun):
                    text_runs.append(run.text)
                else:
                    latex = getattr(run, "latex", "")
                    if latex:
                        text_runs.append(f"${latex}$")
            joined = "".join(text_runs).strip()
            if joined:
                parts.append(joined)
        elif isinstance(block, IRListBlock):
            for item in block.items:
                item_parts: list[str] = []
                for run in item:
                    if isinstance(run, IRTextRun):
                        item_parts.append(run.text)
                    else:
                        latex = getattr(run, "latex", "")
                        if latex:
                            item_parts.append(f"${latex}$")
                joined = "".join(item_parts).strip()
                if joined:
                    parts.append(joined)
        elif isinstance(block, IRImage):
            parts.append(f"![]({block.rel_path})")
        else:
            latex = getattr(block, "latex", "")
            if latex:
                parts.append(f"$$\n{latex}\n$$")
    return "\n\n".join(parts).strip()




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


def _publish_annotated_pdf(
    record: DocumentRecord,
    ir_blocks: list,
    frames: list[PageFrame],
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
            frames=frames,
            blocks=ir_blocks,
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
    """Rebuild the annotated source PDF from the document's parse cache.

    Documents parsed before annotation existed have no such artifact; their
    extraction checkpoint still holds the parsed blocks, so the file can be
    produced without re-running the parser. An existing file is rebuilt as
    well, so a document annotated by an older revision picks up the current
    categories and caption boxes.
    """
    if not record.source_path.is_file():
        return None
    output_dir = settings.output_dir / record.document_id
    checkpoint = _load_extraction_checkpoint(
        output_dir / "extraction-checkpoint.json", record.source_path
    )
    if checkpoint is None:
        return None
    ir_blocks, frames, _notes = _build_ir_and_frames(checkpoint, record.source_path)
    if not ir_blocks:
        return None
    # Geometry the renderer translates: corrected regions plus paragraphs
    # recovered from the source text layer where the parser dropped them.
    align_blocks_to_text_layer(frames, ir_blocks)
    synthesize_unclaimed_paragraphs(frames, ir_blocks)
    _publish_annotated_pdf(record, ir_blocks, frames, output_dir)
    save_document(record)
    artifact = next(
        (item for item in record.artifacts if item.kind == "annotated_pdf"), None
    )
    return artifact.url if artifact else None


def _extract_references_from_text(text: str) -> list[ReferenceEntry]:
    lines = text.splitlines()
    start = None
    for idx, line in enumerate(lines):
        if _REFERENCE_SPLIT_PATTERN.match(line.strip()):
            start = idx + 1
            break
    if start is None:
        return []

    refs: list[ReferenceEntry] = []
    current: list[str] = []
    ref_idx = 0

    for raw in lines[start:]:
        line = raw.strip()
        if not line:
            if current:
                ref_idx += 1
                refs.append(ReferenceEntry(index=ref_idx, text=" ".join(current).strip()))
                current = []
            continue

        matched = _REFERENCE_ITEM_PATTERN.match(line)
        if matched:
            if current:
                ref_idx += 1
                refs.append(ReferenceEntry(index=ref_idx, text=" ".join(current).strip()))
            current = [matched.group(2).strip()]
        elif current:
            current.append(line)
        elif len(line) > 20:
            current = [line]

    if current:
        ref_idx += 1
        refs.append(ReferenceEntry(index=ref_idx, text=" ".join(current).strip()))

    return refs


_EXTRACTION_CHECKPOINT_VERSION = "pdf-extraction-v2"


def _source_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _save_extraction_checkpoint(
    path: Path, source_path: Path, result: MinerUResult
) -> None:
    payload = {
        "version": _EXTRACTION_CHECKPOINT_VERSION,
        "source_sha256": _source_digest(source_path),
        "markdown": result.markdown,
        "mode_label": result.mode_label,
        "extracted_files": [str(item) for item in result.extracted_files],
        "content_blocks": result.content_blocks,
        "layout_payload": result.layout_payload,
        "boxes_normalized": result.boxes_normalized,
        "images_dir": str(result.images_dir) if result.images_dir else None,
        "two_column": result.two_column,
    }
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def _load_extraction_checkpoint(path: Path, source_path: Path) -> MinerUResult | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return None
        if payload.get("version") != _EXTRACTION_CHECKPOINT_VERSION:
            return None
        if payload.get("source_sha256") != _source_digest(source_path):
            return None
        return MinerUResult(
            markdown=str(payload.get("markdown") or ""),
            mode_label=str(payload.get("mode_label") or "checkpoint"),
            extracted_files=[Path(item) for item in payload.get("extracted_files") or []],
            content_blocks=payload.get("content_blocks"),
            layout_payload=payload.get("layout_payload"),
            boxes_normalized=bool(payload.get("boxes_normalized", True)),
            images_dir=Path(payload["images_dir"]) if payload.get("images_dir") else None,
            two_column=bool(payload.get("two_column")),
        )
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _build_ir_and_frames(
    mineru_result: MinerUResult, source_path: Path
) -> tuple[list, list[PageFrame], list[str]]:
    """Materialise the translation IR and the measured page frames."""
    frames = measure_pages(source_path)
    notes: list[str] = []
    if mineru_result.layout_payload:
        blocks, parser_frames = pages_from_middle(mineru_result.layout_payload)
        merge_known_regions(frames, parser_frames)
        return blocks, frames, notes
    if mineru_result.content_blocks is not None:
        sizes = [(frame.width, frame.height) for frame in frames]
        blocks = blocks_to_ir(
            mineru_result.content_blocks,
            sizes,
            normalized_boxes=mineru_result.boxes_normalized,
            frames=frames,
        )
        attach_known_regions(
            frames,
            mineru_result.content_blocks,
            normalized_boxes=mineru_result.boxes_normalized,
        )
        # The VLM backend reports inline formulas without boxes in the content
        # list; its model output carries them.
        model_payload = _model_geometry(mineru_result)
        if model_payload is not None:
            notes.extend(attach_inline_formula_boxes(blocks, model_payload, frames))
        return blocks, frames, notes
    return [], frames, notes


def _model_geometry(mineru_result: MinerUResult):
    """Load MinerU's `*_model.json` when the parse produced one."""
    candidates = [
        path
        for path in mineru_result.extracted_files
        if path.name.endswith("_model.json")
    ]
    if not candidates and mineru_result.images_dir is not None:
        root = mineru_result.images_dir.parent
        if root.is_dir():
            candidates = sorted(root.glob("*_model.json"))
    for path in candidates:
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
        except (OSError, ValueError):
            continue
        if isinstance(payload, (list, dict)):
            return payload
    return None


def _save_layout_plan(path: Path, plans: list) -> None:
    payload = {
        "version": "layout-plan-v1",
        "pages": [
            {
                "index": plan.index,
                "status": plan.status,
                "reason": plan.reason,
                "body_size": round(plan.body_size, 2),
                "leading": round(plan.leading, 2),
                "blocks": [
                    {
                        "kind": block.kind,
                        "status": block.status,
                        "reason": block.reason,
                        "size": round(block.size, 2),
                        "baseline_size": round(block.baseline_size, 2),
                        "leading": round(block.leading, 2),
                        "source_rect": [round(value, 1) for value in block.source_rect],
                        "target": [round(value, 1) for value in block.target],
                    }
                    for block in plan.blocks
                ],
                "cells": [
                    {
                        "status": cell.status,
                        "reason": cell.reason,
                        "size": round(cell.size, 2),
                        "source_rect": [round(value, 1) for value in cell.source_rect],
                    }
                    for cell in plan.cells
                ],
            }
            for plan in plans
        ],
    }
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


_REFIT_MAX_BLOCKS = 30


def _plain_text_block(block) -> bool:
    """Titles and pure-prose paragraphs can be re-translated wholesale.

    Blocks with inline formulas keep their first translation: compressing the
    whole paragraph into one run would detach the formula from its position.
    """
    if isinstance(block, IRTitle):
        return bool(block.text.strip())
    if isinstance(block, IRParagraph):
        return bool(block.runs) and all(
            isinstance(run, IRTextRun) for run in block.runs
        )
    return False


def _replace_block_text(block, text: str) -> None:
    if isinstance(block, IRTitle):
        block.text = text
        return
    first = True
    for run in block.runs:
        if isinstance(run, IRTextRun):
            run.text = text if first else ""
            first = False


def refit_with_concise_translations(plans: list, *, translate_fn, max_blocks: int = _REFIT_MAX_BLOCKS) -> int:
    """Re-translate blocks that did not fit their box with a brevity budget.

    The layout search reports these blocks as ``original`` with a fit-related
    reason. A shorter translation usually lands inside the box on the second
    planning pass. Returns how many blocks received a new translation.
    """
    candidates: list = []
    for plan in plans:
        for block_plan in plan.blocks:
            if (
                block_plan.status == "original"
                and "fit" in block_plan.reason
                and block_plan.block is not None
                and _plain_text_block(block_plan.block)
            ):
                candidates.append(block_plan)
        for cell_plan in plan.cells:
            if (
                cell_plan.status == "original"
                and "fit" in cell_plan.reason
                and cell_plan.cell is not None
            ):
                candidates.append(cell_plan)
    retried = 0
    for candidate in candidates[:max_blocks]:
        try:
            concise = translate_fn(candidate.source_text)
        except Exception:
            continue
        if not concise or not concise.strip():
            continue
        block = getattr(candidate, "block", None)
        if isinstance(block, (IRTitle, IRParagraph)):
            _replace_block_text(block, concise.strip())
        elif getattr(candidate, "cell", None) is not None:
            candidate.cell.translated = concise.strip()
        else:
            continue
        retried += 1
    return retried


def _log_fallback_summary(record: DocumentRecord, plans: list) -> None:
    """Persist why blocks or cells were not translated, for tuning decisions."""
    counts: Counter = Counter()
    for plan in plans:
        for block in plan.blocks:
            if block.status != "translated":
                counts[f"{block.status}: {block.reason or 'unspecified'}"] += 1
        for cell in plan.cells:
            if cell.status != "translated":
                counts[f"cell {cell.status}: {cell.reason or 'unspecified'}"] += 1
    for reason, count in counts.most_common():
        record.logs.append(f"Layout fallback: {count} × {reason}")


def _render_translated_pdf(
    record: DocumentRecord,
    ir_blocks: list,
    frames: list[PageFrame],
    output_dir: Path,
    *,
    override_api_key: str | None = None,
    override_base_url: str | None = None,
    override_model: str | None = None,
    translation_context: str = "",
    translation_domain: str = "",
) -> Path:
    """Lay out every translated block on its source page and write the PDF."""
    crops = prepare_formula_crops(
        record.source_path, frames, ir_blocks, output_dir / "formula-crops"
    )
    try:
        measurer = TextMeasurer(require_cjk_font(), crops.paths, crops.aspects)
        plans = plan_document(frames, ir_blocks, measurer=measurer)
        refit = refit_with_concise_translations(
            plans,
            translate_fn=lambda source: translate_concise(
                source,
                override_api_key=override_api_key,
                override_base_url=override_base_url,
                override_model=override_model,
                translation_context=translation_context,
                domain=translation_domain,
            ),
        )
        if refit:
            record.logs.append(
                f"Layout: re-translated {refit} block(s) with a brevity budget "
                "after they did not fit their boxes"
            )
            plans = plan_document(frames, ir_blocks, measurer=measurer)
            record.translated_text = _ir_to_translated_markdown(ir_blocks)
        plan_path = output_dir / "layout-plan.json"
        _save_layout_plan(plan_path, plans)
        _append_artifact(record, plan_path.name, "layout_plan", plan_path)
        debug_pdf = output_dir / "layout-debug.pdf" if settings.layout_debug else None
        report = render_document(
            source_pdf=record.source_path,
            plans=plans,
            frames=frames,
            blocks=ir_blocks,
            output_pdf=output_dir / "rendered.pdf",
            crops=crops,
            debug_pdf=debug_pdf,
        )
    finally:
        crops.close()
    _log_fallback_summary(record, plans)
    for note in report.notes:
        record.logs.append(f"Layout: {note}")
    produced = len(report.pages) - len(report.failed)
    record.logs.append(
        f"Layout: {produced}/{len(report.pages)} page(s) translated on the source page"
    )
    if debug_pdf is not None and debug_pdf.is_file():
        _append_artifact(record, debug_pdf.name, "layout_debug", debug_pdf)
    return output_dir / "rendered.pdf"


def _translate_and_render(
    record: DocumentRecord,
    mineru_result: MinerUResult,
    output_dir: Path,
    *,
    display_title: str,
    override_api_key: str | None,
    override_base_url: str | None,
    override_model: str | None,
    translation_domain: str = "",
) -> None:
    """Translate the structured blocks and lay the result out on the source pages."""
    ir_blocks, frames, geometry_notes = _build_ir_and_frames(
        mineru_result, record.source_path
    )
    for note in geometry_notes:
        record.logs.append(f"Layout geometry: {note}")
    if not ir_blocks:
        raise RuntimeError(
            "No structured layout could be extracted from this PDF, so no "
            "positioned translation can be produced."
        )
    domain = normalize_domain(translation_domain)
    record.metadata["translation_domain"] = domain
    document_terms = extract_document_terms(
        display_title,
        record.extracted_text,
        override_api_key=override_api_key,
        override_base_url=override_base_url,
        override_model=override_model,
    )
    translation_context = format_glossary_context(
        display_title,
        merge_glossary_terms(glossary_terms_for_prompt(domain), document_terms),
    )
    with with_stage(record, "translate"):
        record.logs.append(f"Translation domain: {DOMAINS[domain].label}")
        record.logs.append(f"Parsed {len(ir_blocks)} structured block(s)")
        # A paragraph the parser reported as several adjacent regions is
        # translated once and distributed back over those regions. The text
        # layer first corrects where each block is actually drawn, which also
        # splits paragraphs the parser stored as a single (mis-sized) region.
        groups, align_notes = align_blocks_to_text_layer(frames, ir_blocks)
        for note in align_notes:
            record.logs.append(f"Layout geometry: {note}")
        for note in synthesize_unclaimed_paragraphs(frames, ir_blocks):
            record.logs.append(f"Layout geometry: {note}")
        if groups:
            merge_continuation_groups(groups)
        extra_groups = plan_continuation_groups(ir_blocks, frames)
        if extra_groups:
            merge_continuation_groups(extra_groups)
            groups.extend(extra_groups)
        if groups:
            record.logs.append(
                f"Merged {len(groups)} paragraph(s) that span several regions"
            )
        # Annotate the geometry the renderer will actually translate, including
        # paragraphs recovered from the source text layer after the parser
        # dropped them.
        _publish_annotated_pdf(record, ir_blocks, frames, output_dir)
        source_segments = collect_translatable_strings(ir_blocks)
        notes = translate_ir(
            ir_blocks,
            override_api_key=override_api_key,
            override_base_url=override_base_url,
            override_model=override_model,
            checkpoint_path=output_dir / "translation-checkpoint.json",
            progress_callback=lambda done, total: set_stage_progress(
                record,
                "translate",
                done / max(1, total),
                f"翻译 {done}/{total} 个片段",
            ),
            translation_context=translation_context,
            domain=domain,
            checkpoint_namespace=f"ir:{domain}",
        )
        for note in notes:
            record.logs.append(f"Translation: {note}")
        record_candidate_terms(domain, document_terms, record.document_id)
        translated_segments = collect_translatable_strings(ir_blocks)
        alignment_path = save_exact_alignment(record, source_segments, translated_segments)
        if alignment_path:
            _append_artifact(record, alignment_path.name, "alignment_index", alignment_path)
            record.logs.append(
                f"Saved {len(source_segments)} exact bilingual alignment segments"
            )
        for note in split_continuation_groups(groups):
            record.logs.append(f"Translation: {note}")
        record.translated_text = _ir_to_translated_markdown(ir_blocks)
        save_document(record)

    with with_stage(record, "render"):
        rendered = _render_translated_pdf(
            record,
            ir_blocks,
            frames,
            output_dir,
            override_api_key=override_api_key,
            override_base_url=override_base_url,
            override_model=override_model,
            translation_context=translation_context,
            translation_domain=domain,
        )
        _publish_translated_pdf(record, rendered, output_dir)


def cached_resume_stage(record: DocumentRecord) -> str:
    """Earliest stage a reprocess can start from while reusing cached work.

    A completed parse checkpoint makes the extraction (the slowest stage) free,
    so reprocessing starts at ``clean``. The translation checkpoint is applied
    inside the translate stage regardless, so cached segments are reused either
    way. An unusable checkpoint simply falls back to a full parse in
    :func:`process_document`.
    """
    checkpoint = settings.output_dir / record.document_id / "extraction-checkpoint.json"
    return "clean" if checkpoint.is_file() else "parse"


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
    parser = provider_settings.pdf_parser if provider_settings else settings.pdf_parser
    mineru_config = (
        MinerUConfig(
            api_key=provider_settings.mineru_api_key,
            base_url=provider_settings.mineru_base_url,
            model_version=provider_settings.mineru_model_version,
            language=provider_settings.mineru_language,
            enable_formula=provider_settings.mineru_enable_formula,
            enable_table=provider_settings.mineru_enable_table,
            is_ocr=provider_settings.mineru_is_ocr,
            poll_interval=settings.mineru_poll_interval,
            timeout=settings.mineru_timeout,
        )
        if provider_settings
        else None
    )
    vision_model = provider_settings.vision_model if provider_settings else settings.vision_model
    translation_domain = normalize_domain(
        provider_settings.translation_domain if provider_settings else None
    )
    record.status = "processing"
    record.logs.append(
        f"Retry processing started from {resume_from}" if resume_from else "Processing started"
    )
    if not record.stages:
        init_stages(record, vision_check_enabled=record.vision_check_enabled)
    elif resume_from:
        prepare_stages_for_retry(record, resume_from)
    else:
        init_stages(record, vision_check_enabled=record.vision_check_enabled)
    save_document(record)
    try:
        record.size_bytes = record.source_path.stat().st_size
    except OSError:
        record.size_bytes = 0

    output_dir = settings.output_dir / record.document_id
    output_dir.mkdir(parents=True, exist_ok=True)
    record.logs.append(f"Output dir: {output_dir}")
    resumed_extraction: MinerUResult | None = None

    try:
        if not resume_from:
            with with_stage(record, "upload"):
                pass

        if resume_from == "render":
            mineru_checkpoint = _load_extraction_checkpoint(
                output_dir / "extraction-checkpoint.json", record.source_path
            )
            if mineru_checkpoint is not None:
                display_title, _ = _derive_display_title(
                    record.source_filename, record.extracted_text
                )
                _translate_and_render(
                    record,
                    mineru_checkpoint,
                    output_dir,
                    display_title=display_title,
                    override_api_key=override_api_key,
                    override_base_url=override_base_url,
                    override_model=override_model,
                    translation_domain=translation_domain,
                )
                record.status = "done"
                record.failure = None
                record.logs.append("Processing done")
                return save_document(record)
            record.logs.append("Extraction checkpoint missing; falling back to parse")
            resume_from = "parse"

        if resume_from == "translate":
            mineru_checkpoint = _load_extraction_checkpoint(
                output_dir / "extraction-checkpoint.json", record.source_path
            )
            if mineru_checkpoint is not None and record.extracted_text:
                display_title, _ = _derive_display_title(
                    record.source_filename, record.extracted_text
                )
                _translate_and_render(
                    record,
                    mineru_checkpoint,
                    output_dir,
                    display_title=display_title,
                    override_api_key=override_api_key,
                    override_base_url=override_base_url,
                    override_model=override_model,
                    translation_domain=translation_domain,
                )
                record.status = "done"
                record.failure = None
                record.logs.append("Processing done")
                return save_document(record)
            if mineru_checkpoint is not None:
                resumed_extraction = mineru_checkpoint
                resume_from = "clean"
                record.logs.append("Extracted-text state missing; rebuilding it from checkpoint")
            else:
                record.logs.append("Extraction checkpoint missing or invalid; falling back to parse")

        if resume_from == "clean" and resumed_extraction is None:
            resumed_extraction = _load_extraction_checkpoint(
                output_dir / "extraction-checkpoint.json", record.source_path
            )
            if resumed_extraction is not None:
                record.logs.append("Reusing completed extraction checkpoint")
            else:
                record.logs.append("Extraction checkpoint missing or invalid; falling back to parse")

        if resumed_extraction is not None:
            mineru_result = resumed_extraction
            extract_dir = (
                mineru_result.images_dir.parent
                if mineru_result.images_dir is not None
                else output_dir
            )
        else:
            with with_stage(record, "parse"):
                record.logs.append("Handling source PDF")
                original_out = output_dir / "original.pdf"
                shutil.copyfile(record.source_path, original_out)
                record.original_pdf_url = f"/data/outputs/{record.document_id}/original.pdf"
                _append_artifact(record, "original.pdf", "original_pdf", original_out)
                _append_artifact(record, record.source_path.name, "source_pdf", record.source_path)

                if parser == "mineru":
                    extract_dir = output_dir / "mineru"
                    record.logs.append("Submitting PDF to MinerU")
                    try:
                        mineru_result = extract_structured_from_pdf(
                            str(record.source_path),
                            extract_dir,
                            log_sink=record.logs,
                            progress_cb=lambda frac, label: set_stage_progress(
                                record, "parse", frac, label
                            ),
                            config=mineru_config,
                        )
                    except Exception as mineru_exc:
                        # MinerU's result CDN can fail after cloud parsing has
                        # completed. Keep the website usable for text-layer PDFs
                        # by falling back locally instead of failing the task.
                        record.logs.append(
                            f"MinerU unavailable ({mineru_exc}); falling back to local PDF parsing"
                        )
                        extract_dir = output_dir / "local"
                        try:
                            mineru_result = extract_structured_from_pdf_local(
                                str(record.source_path), extract_dir, log_sink=record.logs
                            )
                        except Exception as local_exc:
                            raise RuntimeError(
                                f"MinerU parsing failed: {mineru_exc}; local fallback also failed: {local_exc}"
                            ) from local_exc
                else:
                    extract_dir = output_dir
                    record.logs.append("Extracting PDF locally (text layer + images)")
                    mineru_result = extract_structured_from_pdf_local(
                        str(record.source_path), extract_dir, log_sink=record.logs
                    )

            _save_extraction_checkpoint(
                output_dir / "extraction-checkpoint.json", record.source_path, mineru_result
            )

        with with_stage(record, "clean"):
            extracted_text = mineru_result.markdown
            device_or_mode = mineru_result.mode_label
            nougat_files = mineru_result.extracted_files
            fallback_text = extract_text_from_pdf_text_layer(str(record.source_path), max_pages=3)
            record.extracted_text, missing_page_count, recovered_leading = _clean_nougat_text_with_metadata(
                extracted_text,
                leading_fallback_text=fallback_text,
            )
            if not record.extracted_text:
                raise RuntimeError(
                    "No readable text could be extracted from this PDF. "
                    "It may be a scanned / image-only PDF with no embedded text layer "
                    "(the local parser has no OCR; set PDF_PARSER=mineru to use cloud OCR)."
                )
            if missing_page_count:
                record.logs.append(
                    f"MinerU warning: {missing_page_count} missing-page marker(s) stripped; content may be incomplete"
                )
            if recovered_leading:
                record.logs.append("Recovered leading PDF content from embedded text layer")
            record.logs.append("MinerU output cleaned")
            record.logs.append(f"Extraction model: {device_or_mode}")
            record.logs.append(f"Extraction dir: {extract_dir}")
            for generated in nougat_files:
                _append_artifact(record, generated.name, "mineru_output", generated)

            display_title, used_title_fallback = _derive_display_title(record.source_filename, record.extracted_text)
            if used_title_fallback:
                record.logs.append("Title fallback applied from source filename")

            record.references = _extract_references_from_text(record.extracted_text)
            record.logs.append(f"References extracted: {len(record.references)}")
            _enrich_metadata(record, display_title)

        if record.vision_check_enabled:
            with with_stage(record, "vision_check"):
                try:
                    record.extracted_text = run_vision_check_on_markdown(
                        record,
                        pdf_path=output_dir / "original.pdf",
                        text=record.extracted_text,
                        output_dir=output_dir,
                        api_key=override_api_key,
                        base_url=override_base_url,
                        model=vision_model,
                    )
                except Exception as exc:
                    record.logs.append(f"Vision check skipped: {exc}")

        _translate_and_render(
            record,
            mineru_result,
            output_dir,
            display_title=display_title,
            override_api_key=override_api_key,
            override_base_url=override_base_url,
            override_model=override_model,
            translation_domain=translation_domain,
        )

        record.status = "done"
        record.failure = None
        record.logs.append("Processing done")
    except Exception as exc:
        record.status = "failed"
        failure_stage = record.current_stage or resume_from or "upload"
        chunk_match = re.search(r"chunk\s+(\d+)", str(exc), re.IGNORECASE)
        record.failure = FailureEntry(
            stage=failure_stage,
            message=str(exc),
            retryable=record.source_path.is_file(),
            chunk=int(chunk_match.group(1)) if chunk_match else None,
            retry_count=record.retry_count,
        )
        record.logs.append(f"Error: {exc}")
    return save_document(record)
