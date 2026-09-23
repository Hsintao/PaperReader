"""Convert PDFMathTranslate-next (BabelDOC) debug output into the reader manifest.

BabelDOC only writes its intermediate representation when debug is on, and that
representation is tied to the library version. The worker converts it once into
the manifest documented here; every PaperReader component reads that manifest
and never BabelDOC's internal JSON.

Inputs, all inside the BabelDOC working directory:

* paragraph_finder.json      source paragraphs, with their layout labels
* add_debug_information.json the same paragraphs after translation
* il_translated.json         translated paragraph text, keyed by debug_id

Paragraphs that survived layout analysis carry a debug_id; the rest are
measurement artifacts and are dropped. Every box is in page points in PDF user
space — the origin at the page's bottom-left corner — which is what
``boxes_normalized: false`` declares to the reader.

Each block is one physical fragment (``fragment_id``). The layout region it came
from is the logical object (``logical_id``, listed with all its fragments in
``logical_objects``); a region can hold several paragraphs, and BabelDOC numbers
its regions from one on every page, so a logical id is scoped by page.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from workers.pdfmathtranslate import MANIFEST_FILENAME, MANIFEST_SCHEMA_VERSION

__all__ = [
    "MANIFEST_FILENAME",
    "MANIFEST_SCHEMA_VERSION",
    "build_manifest",
    "label_counts",
    "write_manifest",
]

# BabelDOC's per-paragraph layout label mapped onto a PaperReader block type.
# Labels absent from this table carry no reader value: abandon is discarded
# artwork or page furniture, header/footer repeat the page frame, and tiny text
# duplicates a region already covered by another block.
_LABEL_KINDS = {
    "title": "title",
    "paragraph_title": "title",
    "abstract_title": "title",
    "doc_title": "title",
    "reference_heading": "title",
    "plain text": "paragraph",
    "abstract": "paragraph",
    "footnote": "paragraph",
    "algorithm": "paragraph",
    "keywords": "paragraph",
    "reference": "paragraph",
    "reference_content": "paragraph",
    "list_item": "list",
    "isolate_formula": "formula",
    "formula": "formula",
}

# Caption labels are folded into the artwork they describe instead of becoming
# body blocks, so a figure's caption is never translated twice.
_CAPTION_LABELS = {
    "figure_caption": "figure",
    "table_caption": "table",
    "table_footnote": "table",
    "formula_caption": "formula",
}

_TABLE_LABELS = {"table_cell", "table_text"}

# `fallback_line` is what the paragraph finder emits for lines it could not
# group into a paragraph: axis ticks, figure panel labels, stray table rows.
# They are averaged five characters long and there are hundreds of them, so
# only the ones that fall inside a table region carry content worth keeping.
_LOOSE_LINE_LABEL = "fallback_line"

# Region classes BabelDOC's layout model assigns to artwork, keyed by the block
# they produce. Caption classes are deliberately absent.
_REGION_CLASSES = {
    "figure": "figure",
    "image": "figure",
    "table": "table",
    "isolate_formula": "formula",
}

_REFERENCE_LABELS = {"reference", "reference_content"}

_URL_PATTERN = re.compile(r"(?:https?://|www\.)[^\s<>()\[\],;]+")
_CITATION_PATTERN = re.compile(r"\[\d+(?:\s*[,–-]\s*\d+)*\]")
_NUMBER_PATTERN = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?(?:\s*%)?")
_INLINE_MATH_PATTERN = re.compile(r"\$[^$\n]{1,120}\$")

# BabelDOC marks rich-text runs with `{vN}` placeholders and wraps styled runs in
# `<style id='N'>` tags. Neither survives into the translated PDF, so both are
# stripped before the text reaches the reader.
_RICH_TEXT_PATTERN = re.compile(r"</?style[^>]{0,40}>|\{v\d+\}")

_REFERENCE_HEADING_PATTERN = re.compile(r"^\s*(?:references|bibliography)\s*$", re.I)
_REFERENCE_ENTRY_PATTERN = re.compile(r"^\s*(?:\[\d{1,4}\]|\d{1,4}[.)])\s*\S")
# A whole column of entries arrives as one block, so each entry starts at its
# own marker. The marker may sit right against the text it precedes.
_REFERENCE_SPLIT_PATTERN = re.compile(r"(?=\[\d{1,4}\]\s?)")

# Block kinds that are never bibliography text; a figure in the facing column
# must not be read as a continuation of the entry beside it.
_ARTWORK_KINDS = {"figure", "table", "formula"}
_SECTION_NUMBER_PATTERN = re.compile(r"^\s*(\d{1,2})(?:\.(\d{1,2}))?(?:\.(\d{1,2}))?\s*[.、]?\s+\S")


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _box(raw: Any) -> list[float] | None:
    """Normalise BabelDOC's x/y/x2/y2 box to an [x0,y0,x1,y1] list."""
    if isinstance(raw, dict):
        values = (
            _number(raw.get("x")),
            _number(raw.get("y")),
            _number(raw.get("x2")),
            _number(raw.get("y2")),
        )
    elif isinstance(raw, (list, tuple)) and len(raw) == 4:
        values = tuple(_number(item) for item in raw)
    else:
        return None
    x0, y0, x1, y1 = values
    if x1 < x0:
        x0, x1 = x1, x0
    if y1 < y0:
        y0, y1 = y1, y0
    return [round(x0, 2), round(y0, 2), round(x1, 2), round(y1, 2)]


def _read_json(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _composition_text(paragraph: dict) -> str:
    """Recover text from a paragraph whose top-level unicode is empty."""
    parts: list[str] = []
    for item in paragraph.get("pdf_paragraph_composition") or []:
        if not isinstance(item, dict):
            continue
        unicode_item = item.get("pdf_same_style_unicode_characters")
        if isinstance(unicode_item, dict) and unicode_item.get("unicode"):
            parts.append(str(unicode_item["unicode"]))
            continue
        for key in ("pdf_same_style_characters", "pdf_line"):
            line = item.get(key)
            if not isinstance(line, dict):
                continue
            for char in line.get("pdf_character") or []:
                if isinstance(char, dict) and char.get("char_unicode"):
                    parts.append(str(char["char_unicode"]))
    return "".join(parts).strip()


def _paragraph_text(paragraph: dict) -> str:
    text = paragraph.get("unicode")
    if not isinstance(text, str) or not text.strip():
        text = _composition_text(paragraph)
    return _RICH_TEXT_PATTERN.sub("", text).strip()


def _has_formula(paragraph: dict) -> bool:
    for item in paragraph.get("pdf_paragraph_composition") or []:
        if isinstance(item, dict) and item.get("pdf_formula"):
            return True
    return False


def _protected_spans(text: str) -> list[dict]:
    """Spans the reader must keep verbatim when it indexes or searches text."""
    spans: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for kind, pattern in (
        ("url", _URL_PATTERN),
        ("citation", _CITATION_PATTERN),
        ("formula", _INLINE_MATH_PATTERN),
        ("number", _NUMBER_PATTERN),
    ):
        for match in pattern.finditer(text or ""):
            value = match.group(0).strip()
            if not value or (kind, value) in seen:
                continue
            seen.add((kind, value))
            spans.append({"kind": kind, "text": value})
    return spans


def _page_size(page: dict) -> tuple[float, float]:
    box = _box((page.get("mediabox") or {}).get("box"))
    if box is None:
        return 0.0, 0.0
    return round(box[2] - box[0], 2), round(box[3] - box[1], 2)


def _regions(page: dict) -> list[dict]:
    regions: list[dict] = []
    for item in page.get("page_layout") or []:
        if not isinstance(item, dict):
            continue
        kind = _REGION_CLASSES.get(str(item.get("class_name") or "").strip())
        box = _box(item.get("box"))
        if kind is None or box is None:
            continue
        if box[2] - box[0] < 4 or box[3] - box[1] < 4:
            continue
        regions.append({
            "id": item.get("id"),
            "kind": kind,
            "bbox": box,
            "confidence": round(_number(item.get("conf")), 4),
        })
    return regions


def _vertical_gap(region: list[float], box: list[float]) -> float:
    return max(0.0, region[1] - box[3], box[1] - region[3])


def _horizontal_overlap(left: list[float], right: list[float]) -> float:
    return min(left[2], right[2]) - max(left[0], right[0])


def _nearest_region(regions: list[dict], box: list[float], *, kind: str) -> dict | None:
    candidates = [item for item in regions if item["kind"] == kind]
    if not candidates:
        return None
    contained = [
        item
        for item in candidates
        if item["bbox"][0] - 2 <= box[0]
        and box[2] <= item["bbox"][2] + 2
        and item["bbox"][1] - 2 <= box[1]
        and box[3] <= item["bbox"][3] + 2
    ]
    pool = contained or [
        item
        for item in candidates
        if _horizontal_overlap(item["bbox"], box) > 0
        or _vertical_gap(item["bbox"], box) <= 72
    ]
    if not pool:
        return None
    return min(pool, key=lambda item: _vertical_gap(item["bbox"], box))


def _cluster_rows(cells: list[dict]) -> list[list[dict]]:
    """Group cells into rows, top row first.

    Boxes are in PDF user space, where y grows upwards, so the row nearest the
    top of the page has the largest top edge.
    """
    rows: list[list[dict]] = []
    for cell in sorted(cells, key=lambda item: (-item["bbox"][3], item["bbox"][0])):
        centre = (cell["bbox"][1] + cell["bbox"][3]) / 2
        height = max(1.0, cell["bbox"][3] - cell["bbox"][1])
        for row in rows:
            existing = (row[0]["bbox"][1] + row[0]["bbox"][3]) / 2
            if abs(existing - centre) <= height * 0.6:
                row.append(cell)
                break
        else:
            rows.append([cell])
    return rows


def _table_html(rows: list[list[dict]]) -> str:
    lines = ["<table>"]
    for row in rows:
        rendered = "".join(
            f"<td>{(cell['source_text'] or '').strip()}</td>"
            for cell in sorted(row, key=lambda item: item["bbox"][0])
        )
        lines.append(f"<tr>{rendered}</tr>")
    lines.append("</table>")
    return "".join(lines)


def _table_structure(rows: list[list[dict]]) -> dict:
    return {
        "rows": [
            [
                {
                    "bbox": cell["bbox"],
                    "text": cell["source_text"],
                    "translated_text": cell["translated_text"],
                }
                for cell in sorted(row, key=lambda item: item["bbox"][0])
            ]
            for row in rows
        ]
    }


def _paragraph_translations(path: Path) -> dict[tuple[int, str], str]:
    payload = _read_json(path)
    if payload is None:
        return {}
    translations: dict[tuple[int, str], str] = {}
    for page_index, page in enumerate(payload.get("page") or []):
        if not isinstance(page, dict):
            continue
        for paragraph in page.get("pdf_paragraph") or []:
            if not isinstance(paragraph, dict):
                continue
            debug_id = paragraph.get("debug_id")
            if not debug_id:
                continue
            text = _paragraph_text(paragraph)
            if text:
                translations[(page_index, str(debug_id))] = text
    return translations


def _title_level(entry: dict) -> int:
    """Heading depth, from the paper title down to numbered subsections.

    The layout label separates the document title from section headings;
    within the section level, the depth of the section number does the rest
    ("3. Method" is level 2, "3.1. Loss" is level 3).
    """
    if str(entry.get("layout_label") or "") == "doc_title":
        return 1
    match = _SECTION_NUMBER_PATTERN.match(entry.get("source_text") or "")
    if not match:
        return 2
    depth = sum(1 for group in match.groups() if group)
    return min(6, max(2, depth + 1))


def _reading_order_key(block: dict, width: float) -> tuple[int, float, float]:
    x0, _y0, _x1, y1 = block["bbox"]
    return (0 if x0 < (width / 2 if width else 0.0) else 1, -y1, x0)


def _reading_order(blocks: list[dict], width: float) -> list[dict]:
    """Order a page's blocks the way they are read.

    Two-column papers interleave columns when sorted by vertical position
    alone, so blocks are grouped by the column their left edge falls in first
    and then by their top edge. Boxes are in PDF user space, where y grows
    upwards, so "top edge" is the larger y.
    """
    return sorted(blocks, key=lambda block: _reading_order_key(block, width))


def _logical_key(page_index: int, layout_id: Any) -> str | None:
    """Name the layout region a block came from.

    BabelDOC numbers its layout regions from one on every page, so a bare
    ``layout_id`` repeats across pages and cannot identify an object on its
    own. The page index scopes it.
    """
    if layout_id is None:
        return None
    return f"p{page_index}-l{layout_id}"


def _text_block(entry: dict, kind: str, *, level: int | None = None) -> dict:
    block = {
        "bbox": entry["bbox"],
        "layout_label": entry["layout_label"],
        "kind": kind,
        "source_text": entry["source_text"],
        "translated_text": entry["translated_text"],
        "has_formula": entry["has_formula"],
        "protected_spans": _protected_spans(entry["source_text"]),
    }
    if entry.get("layout_id") is not None:
        block["layout_id"] = entry["layout_id"]
    if entry.get("render_order") is not None:
        block["render_order"] = entry["render_order"]
    if level is not None:
        block["level"] = level
    return block


def build_manifest(
    debug_dir: Path,
    *,
    source_pdf: Path,
    source_sha256: str = "",
    mode_label: str = "",
    generator: dict | None = None,
    glossary: list[dict] | None = None,
) -> dict:
    """Assemble the stable manifest from a finished BabelDOC working directory."""
    source_pages = _read_json(debug_dir / "paragraph_finder.json")
    if source_pages is None:
        source_pages = _read_json(debug_dir / "add_debug_information.json")
    if source_pages is None:
        source_pages = _read_json(debug_dir / "layout_generator.json")
    if source_pages is None:
        raise RuntimeError(
            f"no BabelDOC debug output in {debug_dir}; the worker requires debug=True"
        )
    translations = _paragraph_translations(debug_dir / "il_translated.json")

    pages_out: list[dict] = []
    figures: list[dict] = []
    tables: list[dict] = []
    references: list[dict] = []
    logical_objects: list[dict] = []

    for page_index, page in enumerate(source_pages.get("page") or []):
        if not isinstance(page, dict):
            page = {}
        width, height = _page_size(page)
        regions = _regions(page)
        blocks: list[dict] = []
        caption_blocks: list[dict] = []
        cell_blocks: list[dict] = []
        loose_lines: list[dict] = []

        for paragraph in page.get("pdf_paragraph") or []:
            if not isinstance(paragraph, dict):
                continue
            debug_id = paragraph.get("debug_id")
            if not debug_id:
                continue
            box = _box(paragraph.get("box"))
            if box is None:
                continue
            label = str(paragraph.get("layout_label") or "").strip()
            entry = {
                "bbox": box,
                "layout_label": label,
                "source_text": _paragraph_text(paragraph),
                "translated_text": translations.get((page_index, str(debug_id)), ""),
                "has_formula": _has_formula(paragraph),
                "layout_id": paragraph.get("layout_id"),
                "render_order": paragraph.get("render_order"),
            }
            if label in _CAPTION_LABELS:
                entry["kind"] = _CAPTION_LABELS[label]
                caption_blocks.append(entry)
            elif label in _TABLE_LABELS:
                entry["kind"] = "table"
                cell_blocks.append(entry)
            elif label == _LOOSE_LINE_LABEL:
                loose_lines.append(entry)
            else:
                entry["kind"] = _LABEL_KINDS.get(label)
                if entry["kind"] is not None:
                    blocks.append(entry)

        # Captions and cells belong to the artwork region they sit in or beside,
        # so they never re-enter the body text.
        for kind in ("figure", "table", "formula"):
            for caption in [item for item in caption_blocks if item["kind"] == kind]:
                region = _nearest_region(regions, caption["bbox"], kind=kind)
                if region is None:
                    blocks.append(caption)
                    continue
                region.setdefault("captions", []).append(caption)

        grouped_cells: dict[int, list[dict]] = {}
        for cell in cell_blocks:
            region = _nearest_region(regions, cell["bbox"], kind="table")
            if region is None:
                blocks.append(cell)
                continue
            grouped_cells.setdefault(id(region), []).append(cell)

        # An ungrouped line is measurement noise — an axis tick or a panel
        # label — but the same line inside a table region is a cell the table
        # parser missed.
        for line in loose_lines:
            region = _nearest_region(regions, line["bbox"], kind="table")
            if region is None:
                continue
            line["kind"] = "table"
            grouped_cells.setdefault(id(region), []).append(line)

        merged: list[dict] = []
        for entry in _reading_order(blocks, width):
            if not entry["source_text"] and not entry["has_formula"]:
                continue
            if entry["kind"] == "title":
                merged.append(_text_block(entry, "title", level=_title_level(entry)))
            elif entry["kind"] == "table":
                merged.append(_text_block(entry, "paragraph"))
            else:
                merged.append(_text_block(entry, entry["kind"]))

        for region in regions:
            captions = region.get("captions", [])
            cells = grouped_cells.get(id(region))
            # A formula region holds no text of its own: the formula reaches the
            # reader as the paragraph block inside the region. Emitting the
            # region as well would add a blank display formula to the body.
            if region["kind"] == "formula" and not captions:
                continue
            caption_text = " ".join(
                item["source_text"] for item in captions if item["source_text"]
            ).strip()
            caption_translated = " ".join(
                (item["translated_text"] or item["source_text"])
                for item in captions
                if item["translated_text"] or item["source_text"]
            ).strip()
            block = {
                "bbox": region["bbox"],
                "layout_label": region["kind"],
                "kind": region["kind"],
                "layout_id": region["id"],
                "source_text": caption_text,
                "translated_text": caption_translated,
                "captions": [
                    {
                        "bbox": item["bbox"],
                        "source_text": item["source_text"],
                        "translated_text": item["translated_text"],
                    }
                    for item in captions
                ],
                "caption_bbox": (
                    [
                        min(item["bbox"][0] for item in captions),
                        min(item["bbox"][1] for item in captions),
                        max(item["bbox"][2] for item in captions),
                        max(item["bbox"][3] for item in captions),
                    ]
                    if captions
                    else None
                ),
                "has_formula": False,
                "protected_spans": _protected_spans(caption_text),
            }
            if cells:
                rows = _cluster_rows(cells)
                block["table"] = _table_structure(rows)
                block["table_html"] = _table_html(rows)
            merged.append(block)

        merged.sort(key=lambda item: _reading_order_key(item, width))
        # Each block is one physical fragment. The layout region it came from is
        # the logical object that groups its fragments, in reading order.
        page_objects: dict[str, list[str]] = {}
        for position, block in enumerate(merged):
            fragment_id = f"p{page_index}-b{position}"
            block["fragment_id"] = fragment_id
            logical_key = _logical_key(page_index, block.pop("layout_id", None))
            block.pop("render_order", None)
            if logical_key is None:
                block["logical_id"] = fragment_id
                block["fragments"] = [fragment_id]
            else:
                block["logical_id"] = logical_key
                page_objects.setdefault(logical_key, []).append(fragment_id)

            if block["kind"] == "figure":
                record = {
                    "page_index": page_index,
                    "bbox": block["bbox"],
                    "label": block["kind"],
                    "caption": block["source_text"],
                    "translated_caption": block["translated_text"],
                    "logical_id": block["logical_id"],
                }
                figures.append(record)
            elif block["kind"] == "table":
                tables.append({
                    "page_index": page_index,
                    "bbox": block["bbox"],
                    "label": block["kind"],
                    "caption": block["source_text"],
                    "translated_caption": block["translated_text"],
                    "logical_id": block["logical_id"],
                })
            if block["layout_label"] in _REFERENCE_LABELS and block["source_text"]:
                references.append({
                    "page_index": page_index,
                    "bbox": block["bbox"],
                    "text": block["source_text"],
                    "translated": block["translated_text"],
                })

        for block in merged:
            if block["logical_id"] in page_objects:
                block["fragments"] = list(page_objects[block["logical_id"]])
        logical_objects.extend(
            {"id": key, "fragments": fragments}
            for key, fragments in page_objects.items()
        )

        pages_out.append({
            "index": page_index,
            "width": width,
            "height": height,
            "blocks": merged,
        })

    if not references:
        references = _references_after_heading(pages_out)

    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "generator": generator or {},
        "source_pdf": str(source_pdf),
        "source_sha256": source_sha256,
        "mode_label": mode_label,
        "page_count": len(pages_out),
        "unit": "point",
        "boxes_normalized": False,
        "pages": pages_out,
        "figures": figures,
        "tables": tables,
        "references": references,
        "logical_objects": logical_objects,
        "glossary": glossary or [],
    }


def label_counts(manifest: dict) -> dict[str, int]:
    """Diagnostics helper: how many blocks of each label the manifest holds."""
    counter: Counter[str] = Counter()
    for page in manifest.get("pages") or []:
        for block in page.get("blocks") or []:
            counter[str(block.get("layout_label") or "")] += 1
    return dict(counter)


def _references_after_heading(pages_out: list[dict]) -> list[dict]:
    """Bibliography entries are the numbered blocks that follow the heading.

    The layout model does not label bibliography paragraphs on every paper, so
    the section is found by its heading and its entries by their `[n]` prefix.
    A block often holds a whole column of entries, which is why each block is
    split into one reference per marker.
    """
    references: list[dict] = []
    inside = False
    for page in pages_out:
        for block in page.get("blocks") or []:
            text = str(block.get("source_text") or "")
            if block.get("kind") == "title":
                if _REFERENCE_HEADING_PATTERN.match(text):
                    inside = True
                    continue
                if inside:
                    # A further heading ends the bibliography.
                    return references
                continue
            if not inside:
                continue
            if block.get("kind") in _ARTWORK_KINDS:
                continue
            translated = str(block.get("translated_text") or "")
            leading, sources = _split_references(text)
            lead_translated, translations = _split_references(translated)
            if leading and references:
                # A wrapped line continues the entry above it.
                references[-1]["text"] = f"{references[-1]['text']} {leading}".strip()
            if lead_translated and references:
                references[-1]["translated"] = (
                    f"{references[-1]['translated']} {lead_translated}"
                ).strip()
            if not sources:
                continue
            if len(translations) != len(sources):
                translations = [translated] + [""] * (len(sources) - 1)
            for index, source in enumerate(sources):
                references.append({
                    "page_index": page.get("index", 0),
                    "bbox": block.get("bbox"),
                    "text": source,
                    "translated": translations[index] if index < len(translations) else "",
                })
    return references


def _split_references(text: str) -> tuple[str, list[str]]:
    """Split one block into the text before its first marker and its entries.

    A bibliography column arrives as one block per column, and an entry can
    straddle a block boundary, so the text ahead of the first marker belongs to
    the entry that started in the previous block.
    """
    pieces = [part.strip() for part in _REFERENCE_SPLIT_PATTERN.split(text or "")]
    pieces = [part for part in pieces if part]
    if not pieces:
        return "", []
    if pieces[0].startswith("["):
        return "", pieces
    return pieces[0], pieces[1:]


def write_manifest(path: Path, manifest: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)
