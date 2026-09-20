"""Re-render already-parsed papers through the V2 layout pipeline.

Reuses each document's extraction checkpoint, so no MinerU call is needed. The
translation checkpoint is reused when its contract still matches, and any
segment the current contract cannot reuse is translated again. Writes the
translated PDF, the annotated source PDF, the versioned layout plan and a render
report per paper under ``data/translation-layout-v2-check/``.

Usage: conda run -n pt python scripts/reflow_real_paper_check.py [--app-data DIR]
"""

import argparse
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.services import app_settings, document_pipeline  # noqa: E402
from app.services.annotation_render import render_annotated_pdf  # noqa: E402
from app.services.cjk_fonts import require_cjk_font  # noqa: E402
from app.services.layout_fit import TextMeasurer, plan_document  # noqa: E402
from app.services.layout_model import (  # noqa: E402
    align_blocks_to_text_layer,
    synthesize_unclaimed_paragraphs,
)
from app.services.layout_render import prepare_formula_crops, render_document  # noqa: E402
from app.services.mineru_layout import (  # noqa: E402
    merge_continuation_groups,
    plan_continuation_groups,
    split_continuation_groups,
)
from app.services.translate_service import (  # noqa: E402
    _TRANSLATION_CONTRACT_VERSION,
    translate_concise,
    translate_ir,
)

DEFAULT_APP_OUTPUTS = Path(
    "~/Library/Application Support/PaperReader/data/outputs"
).expanduser()
OUT_DIR = Path(__file__).resolve().parents[1] / "data" / "translation-layout-v2-check"


def _provider() -> dict:
    """The configured translation provider, when the app has one."""
    current = app_settings.load_settings()
    if not current.api_key:
        return {}
    return {
        "override_api_key": current.api_key,
        "override_base_url": current.base_url,
        "override_model": current.model,
    }


def render_one(app_dir: Path, name: str) -> dict:
    provider = _provider()
    source_pdf = app_dir / "original.pdf"
    mineru_result = document_pipeline._load_extraction_checkpoint(
        app_dir / "extraction-checkpoint.json", source_pdf
    )
    if mineru_result is None:
        raise RuntimeError(f"[{name}] extraction checkpoint unusable")

    ir_blocks, frames, _notes = document_pipeline._build_ir_and_frames(
        mineru_result, source_pdf
    )
    groups, _align_notes = align_blocks_to_text_layer(frames, ir_blocks)
    synthesize_unclaimed_paragraphs(frames, ir_blocks)
    if groups:
        merge_continuation_groups(groups)
    extra = plan_continuation_groups(ir_blocks, frames)
    if extra:
        merge_continuation_groups(extra)
        groups.extend(extra)

    work_dir = OUT_DIR / name
    work_dir.mkdir(parents=True, exist_ok=True)
    # The translation cache is written under this run's own output directory:
    # the source parse may live in a read-only application data folder.
    checkpoint = work_dir / "translation-checkpoint.json"
    translate_notes, translation_issues = translate_ir(
        ir_blocks,
        checkpoint_path=checkpoint,
        **provider,
    )
    split_continuation_groups(groups)
    for note in translate_notes:
        print(f"  translation note: {note}")

    annotated = work_dir / f"{name}_原文标注.pdf"
    render_annotated_pdf(
        source_pdf=source_pdf, frames=frames, blocks=ir_blocks, output_pdf=annotated
    )

    crops = prepare_formula_crops(source_pdf, frames, ir_blocks, work_dir / "crops")
    try:
        measurer = TextMeasurer(
            require_cjk_font(), crops.paths, crops.aspects, crops.crop
        )
        plans = plan_document(frames, ir_blocks, measurer=measurer)
        refit = document_pipeline.refit_with_concise_translations(
            plans,
            translate_fn=lambda source: translate_concise(source, **provider),
        )
        if refit:
            print(f"  concise refit: {refit} block(s)")
            plans = plan_document(frames, ir_blocks, measurer=measurer)
        plan_path = work_dir / "layout-plan.json"
        document_pipeline._save_layout_plan(
            plan_path,
            plans,
            source={
                "document_id": app_dir.name,
                "source_filename": source_pdf.name,
                "page_count": len(frames),
            },
        )
        report = render_document(
            source_pdf=source_pdf,
            plans=plans,
            frames=frames,
            blocks=ir_blocks,
            output_pdf=work_dir / f"{name}_Chinese_ver.pdf",
            crops=crops,
        )
    finally:
        crops.close()

    statuses = [page.status for page in report.pages]
    print(f"[{name}] pages: {len(statuses)}")
    for page in report.pages:
        marker = "" if page.status == "ok" else f"  <- {page.reason}"
        print(f"  page {page.index + 1}: {page.status}{marker}")
    summary = {
        "name": name,
        "app_dir": str(app_dir),
        "contract": _TRANSLATION_CONTRACT_VERSION,
        "pages": [
            {"index": page.index, "status": page.status, "reason": page.reason}
            for page in report.pages
        ],
        "notes": report.notes,
        "translation_issues": translation_issues,
        "layout_plan": str(plan_path),
        "translation_checkpoint": str(checkpoint),
        "translated_pdf": str(work_dir / f"{name}_Chinese_ver.pdf"),
        "annotated_pdf": str(annotated),
    }
    (work_dir / "report.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--app-data",
        default=str(DEFAULT_APP_OUTPUTS),
        help="Directory holding the app's per-document output folders",
    )
    parser.add_argument(
        "--documents",
        nargs="*",
        default=[],
        help="document-id=name pairs to render; defaults to every folder",
    )
    args = parser.parse_args()
    root = Path(args.app_data)
    if args.documents:
        pairs = [item.split("=", 1) for item in args.documents]
    else:
        pairs = [
            (folder.name, folder.name)
            for folder in sorted(root.iterdir())
            if folder.is_dir()
        ]
    for doc_id, name in pairs:
        print(f"== {name} ({doc_id})")
        render_one(root / doc_id, name)


if __name__ == "__main__":
    main()
