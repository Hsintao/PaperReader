"""Re-render the two already-processed papers from their checkpoints.

Reuses the extraction and translation checkpoints under the desktop app's
data directory, so no MinerU or LLM calls are needed. Runs the layout planner
and renderer (with the reflow fallback) and writes the resulting PDFs plus
per-page render reports to data/reflow-check/.

Usage: conda run -n pt python scripts/reflow_real_paper_check.py
"""

import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.services import document_pipeline  # noqa: E402
from app.services.cjk_fonts import require_cjk_font  # noqa: E402
from app.services.layout_fit import TextMeasurer, plan_document  # noqa: E402
from app.services.layout_model import (  # noqa: E402
    align_blocks_to_text_layer,
    measure_pages,
    synthesize_unclaimed_paragraphs,
)
from app.services.layout_render import prepare_formula_crops, render_document  # noqa: E402
from app.services.mineru_layout import (  # noqa: E402
    merge_continuation_groups,
    plan_continuation_groups,
    split_continuation_groups,
)
from app.services.translate_service import translate_concise, translate_ir  # noqa: E402

APP_OUTPUTS = Path(
    "/Users/guoxintao/Library/Application Support/PaperReader/data/outputs"
)
DOCUMENTS = {
    "farbman-edge-preserving": "dfc20348-27ab-4dcf-b1d0-33da8371b9d1",
    "liang-hybrid-l1-l0": "7c6feb76-1086-448f-8f65-579c26e93921",
}
OUT_DIR = Path(__file__).resolve().parents[1] / "data" / "reflow-check"


def render_one(doc_id: str, name: str) -> None:
    app_dir = APP_OUTPUTS / doc_id
    source_pdf = app_dir / "original.pdf"
    mineru_result = document_pipeline._load_extraction_checkpoint(
        app_dir / "extraction-checkpoint.json", source_pdf
    )
    if mineru_result is None:
        print(f"[{name}] extraction checkpoint unusable")
        return

    ir_blocks, frames, notes = document_pipeline._build_ir_and_frames(
        mineru_result, source_pdf
    )
    groups, align_notes = align_blocks_to_text_layer(frames, ir_blocks)
    synthesize_unclaimed_paragraphs(frames, ir_blocks)
    if groups:
        merge_continuation_groups(groups)
    extra = plan_continuation_groups(ir_blocks, frames)
    if extra:
        merge_continuation_groups(extra)
        groups.extend(extra)

    translate_notes = translate_ir(
        ir_blocks,
        checkpoint_path=app_dir / "translation-checkpoint.json",
    )
    split_continuation_groups(groups)

    work_dir = OUT_DIR / name
    work_dir.mkdir(parents=True, exist_ok=True)
    crops = prepare_formula_crops(source_pdf, frames, ir_blocks, work_dir / "crops")
    try:
        measurer = TextMeasurer(require_cjk_font(), crops.paths, crops.aspects)
        plans = plan_document(frames, ir_blocks, measurer=measurer)
        # Mirror the app's render stage: blocks that do not fit get one
        # brevity-constrained re-translation, then everything is re-planned.
        # Offline (no API key) this is a no-op.
        refit = document_pipeline.refit_with_concise_translations(
            plans, translate_fn=lambda source: translate_concise(source)
        )
        if refit:
            print(f"  concise refit: {refit} block(s)")
            plans = plan_document(frames, ir_blocks, measurer=measurer)
        report = render_document(
            source_pdf=source_pdf,
            plans=plans,
            frames=frames,
            blocks=ir_blocks,
            output_pdf=work_dir / f"{name}.pdf",
            crops=crops,
        )
    finally:
        crops.close()

    statuses = [page.status for page in report.pages]
    print(f"[{name}] pages: {len(statuses)}")
    for page in report.pages:
        marker = "" if page.status == "ok" else f"  <- {page.reason}"
        print(f"  page {page.index + 1}: {page.status}{marker}")
    for note in translate_notes:
        print(f"  translation note: {note}")
    (work_dir / "report.json").write_text(
        json.dumps(
            {
                "pages": [
                    {"index": p.index, "status": p.status, "reason": p.reason}
                    for p in report.pages
                ],
                "notes": report.notes,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"  -> {work_dir / (name + '.pdf')}")


def main() -> None:
    for name, doc_id in DOCUMENTS.items():
        render_one(doc_id, name)


if __name__ == "__main__":
    main()
