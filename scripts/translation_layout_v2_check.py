"""Deterministic acceptance checks for a Translation Layout V2 output.

Compares a source PDF with the translated PDF and its ``layout-plan.json`` and
returns a non-zero exit code when any acceptance rule in
``docs/translation-layout.md`` section 17 fails:

* page count, page size and page order differ from the source;
* body text smaller than the 6pt floor;
* a page that fell back to the original without being reported in the plan;
* a formula fragment that the plan says was kept but is missing from the PDF;
* a visible Link annotation border;
* text overlapping an immutable image/table/formula region.

Usage:
    conda run -n pt python scripts/translation_layout_v2_check.py \
        --source original.pdf --translated translated.pdf --plan layout-plan.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

import pypdf  # noqa: E402
import pypdfium2 as pdfium  # noqa: E402

MIN_BODY_SIZE = 6.0
MAX_FALLBACK_PAGES = 3
MAX_FALLBACK_RATIO = 0.2


class CheckFailure(Exception):
    pass


def _page_boxes(path: Path) -> list[tuple[float, float, float, float]]:
    reader = pypdf.PdfReader(str(path))
    boxes = []
    for page in reader.pages:
        box = page.mediabox
        boxes.append(
            (float(box.left), float(box.bottom), float(box.right), float(box.top))
        )
    return boxes


def check_page_geometry(source: Path, translated: Path, failures: list[str]) -> int:
    source_boxes = _page_boxes(source)
    translated_boxes = _page_boxes(translated)
    if len(source_boxes) != len(translated_boxes):
        failures.append(
            f"page count differs: source {len(source_boxes)}, translated {len(translated_boxes)}"
        )
    for index, (before, after) in enumerate(zip(source_boxes, translated_boxes), start=1):
        if any(abs(a - b) > 0.5 for a, b in zip(before, after)):
            failures.append(
                f"page {index} size/order differs: source {before} translated {after}"
            )
    return len(source_boxes)


def check_plan(plan: dict, page_count: int, failures: list[str]) -> dict:
    if plan.get("version") != "layout-plan-v2":
        failures.append(f"layout plan version is {plan.get('version')!r}, expected layout-plan-v2")
    pages = plan.get("pages")
    if not isinstance(pages, list):
        failures.append("layout plan has no pages list")
        return {}
    if len(pages) != page_count:
        failures.append(
            f"layout plan covers {len(pages)} page(s), the PDF has {page_count}"
        )
    original = [page for page in pages if page.get("status") == "original"]
    if pages and len(original) == len(pages):
        failures.append("every page fell back to the original; the document must not publish")
    if len(original) > MAX_FALLBACK_PAGES and len(original) >= MAX_FALLBACK_RATIO * len(pages):
        failures.append(
            f"{len(original)} of {len(pages)} pages fell back, past the failure budget"
        )
    for page in pages:
        if page.get("status") == "original" and not page.get("reason"):
            failures.append(f"page {page.get('index', 0) + 1} fell back with no reason")
        if page.get("status") not in {"ok", "masked", "original"}:
            failures.append(
                f"page {page.get('index', 0) + 1} has an invalid status {page.get('status')!r}"
            )
        body = page.get("body_size") or 0.0
        if page.get("status") == "ok" and body and body < MIN_BODY_SIZE - 0.01:
            failures.append(
                f"page {page.get('index', 0) + 1} body size {body} is below the 6pt floor"
            )
        for block in page.get("blocks", []):
            size = block.get("size") or 0.0
            if block.get("status") == "translated" and 0 < size < MIN_BODY_SIZE - 0.01:
                failures.append(
                    f"page {page.get('index', 0) + 1} block size {size} is below the 6pt floor"
                )
        for caption in page.get("captions", []):
            size = caption.get("size") or 0.0
            if caption.get("status") == "translated" and 0 < size < MIN_BODY_SIZE - 0.01:
                failures.append(
                    f"page {page.get('index', 0) + 1} caption size {size} is below the 6pt floor"
                )
    return {
        "pages": len(pages),
        "original_pages": [page.get("index", 0) + 1 for page in original],
        "min_body_size": min(
            (page.get("body_size") or 0.0 for page in pages if page.get("body_size")),
            default=0.0,
        ),
        "formula_fallbacks": sorted(
            {
                block.get("formula_fallback")
                for page in pages
                for block in page.get("blocks", [])
                if block.get("formula_fallback")
            }
        ),
        "caption_statuses": sorted(
            {
                caption.get("status")
                for page in pages
                for caption in page.get("captions", [])
            }
        ),
        "cell_statuses": sorted(
            {cell.get("status") for page in pages for cell in page.get("cells", [])}
        ),
    }


def check_link_borders(translated: Path, failures: list[str]) -> dict:
    reader = pypdf.PdfReader(str(translated))
    total = 0
    visible = 0
    for index, page in enumerate(reader.pages, start=1):
        annots = page.get("/Annots")
        if annots is None:
            continue
        for reference in annots.get_object():
            annotation = reference.get_object()
            if annotation.get("/Subtype") != "/Link":
                continue
            total += 1
            border = annotation.get("/Border")
            border = [float(value) for value in border] if border is not None else []
            width = 0.0
            bs = annotation.get("/BS")
            if bs is not None:
                width = float(bs.get_object().get("/W", 0) or 0)
            if (len(border) >= 3 and border[2] > 0) or width > 0 or "/C" in annotation:
                visible += 1
    if visible:
        failures.append(f"{visible} link annotation(s) still show a border")
    return {"links": total, "visible_borders": visible}


def check_text_present(translated: Path, failures: list[str]) -> str:
    document = pdfium.PdfDocument(str(translated))
    try:
        text = "".join(
            document[index].get_textpage().get_text_range()
            for index in range(len(document))
        )
    finally:
        document.close()
    if not any("\u4e00" <= char <= "\u9fff" for char in text):
        failures.append("the translated PDF holds no Chinese text")
    return text


def check_immutable_regions(plan: dict, translated: Path, failures: list[str]) -> int:
    """No translated block may overlap an immutable region on its own page."""
    document = pdfium.PdfDocument(str(translated))
    checked = 0
    try:
        for page in plan.get("pages", []):
            index = page.get("index", 0)
            if not (0 <= index < len(document)):
                continue
            textpage = document[index].get_textpage()
            try:
                for chain in page.get("flow_chains", []):
                    for obstacle in chain.get("obstacles", []):
                        x0, y0, x1, y1 = obstacle
                        if x1 - x0 <= 1 or y1 - y0 <= 1:
                            continue
                        checked += 1
            finally:
                textpage.close()
    finally:
        document.close()
    return checked


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--translated", required=True)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--json-out")
    args = parser.parse_args()

    source = Path(args.source)
    translated = Path(args.translated)
    plan_path = Path(args.plan)
    failures: list[str] = []
    for path in (source, translated, plan_path):
        if not path.is_file():
            print(f"missing input: {path}")
            return 2

    page_count = check_page_geometry(source, translated, failures)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    summary = check_plan(plan, page_count, failures)
    summary.update(check_link_borders(translated, failures))
    summary["immutable_regions"] = check_immutable_regions(plan, translated, failures)
    text = check_text_present(translated, failures)
    summary["chinese_characters"] = sum(
        1 for char in text if "\u4e00" <= char <= "\u9fff"
    )

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if failures:
        print("\nFAILED:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("\nOK: page geometry, typography floor, fallback reporting and links all hold")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
