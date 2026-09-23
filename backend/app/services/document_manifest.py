"""Read the worker's manifest into everything the reader needs.

The manifest is the only contract between the PDFMathTranslate-next worker and
PaperReader: page geometry, typed blocks with their source and translated text,
figures, tables, references and protected spans. Nothing downstream reads the
translator's internal representation.

Coordinates are page points in PDF user space — the origin at the page's
bottom-left corner — which the manifest declares through
``boxes_normalized: false``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.models.store import ReferenceEntry
from app.services.document_ir import (
    Author,
    Block,
    DisplayMath,
    Image,
    ListBlock,
    Paragraph,
    Table,
    TableCell,
    TextRun,
    Title,
    classify_roles,
)

MANIFEST_SCHEMA_VERSION = "paperreader-manifest-v1"

Rect = tuple[float, float, float, float]

class ManifestError(ValueError):
    """The manifest is missing, unreadable, or of an unknown version."""


@dataclass
class PageGeometry:
    """One page's box, in the manifest's coordinate system."""

    index: int
    width: float
    height: float

    @property
    def rect(self) -> Rect:
        return (0.0, 0.0, self.width, self.height)


@dataclass
class Figure:
    """A figure or table the reader lists and previews."""

    kind: str
    caption: str
    translated_caption: str
    page_index: int
    bbox: Rect | None = None
    logical_id: str = ""
    url: str = ""
    page: int | None = None
    label: str = ""
    locate_text: str = ""

    def as_dict(self) -> dict:
        payload = {
            "kind": self.kind,
            "caption": self.caption,
            "page": (self.page_index + 1) if self.page_index >= 0 else None,
            "url": self.url,
        }
        if self.label:
            payload["label"] = self.label
        return payload


@dataclass
class DocumentManifest:
    """Parsed manifest: geometry, IR blocks and the reader's side tables."""

    schema_version: str
    source_pdf: str
    source_sha256: str
    mode_label: str
    generator: dict = field(default_factory=dict)
    pages: list[PageGeometry] = field(default_factory=list)
    blocks: list[Block] = field(default_factory=list)
    figures: list[Figure] = field(default_factory=list)
    tables: list[Figure] = field(default_factory=list)
    references: list[ReferenceEntry] = field(default_factory=list)
    glossary: list[dict] = field(default_factory=list)

    @property
    def page_count(self) -> int:
        return len(self.pages)

    def page(self, index: int) -> PageGeometry | None:
        return self.pages[index] if 0 <= index < len(self.pages) else None

    def markdown(self, side: str = "translated") -> str:
        """The document as light Markdown, for search and the vision check."""
        parts: list[str] = []
        for block in self.blocks:
            text = _block_side_text(block, side).strip()
            if not text:
                continue
            if isinstance(block, Title):
                parts.append(f"{'#' * max(1, min(block.level, 6))} {text}")
            elif isinstance(block, DisplayMath):
                parts.append(f"$$\n{block.latex}\n$$")
            elif isinstance(block, Image):
                parts.append(text)
            else:
                parts.append(text)
        return "\n\n".join(part for part in parts if part).strip()

    def alignment_pairs(self) -> list[tuple[str, str]]:
        """Source/translation pairs for the bilingual locator, in reading order."""
        pairs: list[tuple[str, str]] = []
        for block in self.blocks:
            if not isinstance(block, (Title, Paragraph, ListBlock)):
                continue
            source = _block_side_text(block, "source").strip()
            translated = _block_side_text(block, "translated").strip()
            if source and translated:
                pairs.append((source, translated))
        return pairs


def _block_side_text(block: Block, side: str) -> str:
    if side == "source":
        if isinstance(block, (Title, Author, Paragraph, ListBlock)):
            return block.source_text or _rendered_text(block)
        if isinstance(block, DisplayMath):
            return block.source_text or block.latex
        return block.caption if isinstance(block, (Image, Table)) else ""
    if isinstance(block, (Title, Author)):
        return block.text
    if isinstance(block, Paragraph):
        return _rendered_text(block)
    if isinstance(block, ListBlock):
        return _rendered_text(block)
    if isinstance(block, (Image, Table)):
        return block.translated_caption or block.caption
    return ""


def _rendered_text(block: Block) -> str:
    if isinstance(block, Paragraph):
        runs = block.runs
    elif isinstance(block, ListBlock):
        runs = [run for item in block.items for run in item]
    else:
        return ""
    return "".join(
        run.text if isinstance(run, TextRun) else (f"${run.latex}$" if run.latex else "")
        for run in runs
    )


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _rect(value: Any) -> Rect | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    return tuple(_number(item) for item in value)  # type: ignore[return-value]


def load_manifest(path: Path) -> dict:
    """Read and validate a manifest file."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ManifestError(f"manifest not found: {path}") from exc
    except (OSError, ValueError) as exc:
        raise ManifestError(f"manifest is not valid JSON: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ManifestError("manifest must hold a JSON object")
    version = str(payload.get("schema_version") or "")
    if version != MANIFEST_SCHEMA_VERSION:
        raise ManifestError(
            f"unsupported manifest schema {version!r}; expected {MANIFEST_SCHEMA_VERSION!r}"
        )
    if not isinstance(payload.get("pages"), list):
        raise ManifestError("manifest has no 'pages' list")
    return payload


def pages_of(manifest: dict) -> list[PageGeometry]:
    pages: list[PageGeometry] = []
    for index, page in enumerate(manifest.get("pages") or []):
        if not isinstance(page, dict):
            page = {}
        pages.append(
            PageGeometry(
                index=index,
                width=_number(page.get("width")),
                height=_number(page.get("height")),
            )
        )
    return pages


def blocks_of(manifest: dict) -> list[Block]:
    """Every page's blocks as typed IR, in reading order."""
    blocks: list[Block] = []
    for page_index, page in enumerate(manifest.get("pages") or []):
        if not isinstance(page, dict):
            continue
        for raw in page.get("blocks") or []:
            if not isinstance(raw, dict):
                continue
            block = _block_from(raw, page_index)
            if block is not None:
                blocks.append(block)
    classify_roles(blocks)
    return blocks


def _block_from(raw: dict, page_index: int) -> Block | None:
    kind = str(raw.get("kind") or "")
    bbox = _rect(raw.get("bbox"))
    source_text = str(raw.get("source_text") or "")
    translated_text = str(raw.get("translated_text") or "")
    label = str(raw.get("layout_label") or "")
    if kind == "title":
        return Title(
            level=max(1, int(raw.get("level") or 1)),
            text=translated_text or source_text,
            page_index=page_index,
            bbox=bbox,
            source_text=source_text,
            block_type=label,
        )
    if kind == "list":
        items = [
            [TextRun(line)]
            for line in (translated_text or source_text).splitlines()
            if line.strip()
        ]
        return ListBlock(
            list_type="reference_list" if label.startswith("reference") else "",
            items=items or [[TextRun(translated_text or source_text)]],
            page_index=page_index,
            bbox=bbox,
            source_text=source_text,
            block_type=label,
        )
    if kind == "formula":
        return DisplayMath(
            latex=source_text,
            page_index=page_index,
            bbox=bbox,
            source_text=source_text,
            block_type=label or "formula",
        )
    if kind == "figure":
        return Image(
            caption=source_text,
            translated_caption=translated_text,
            page_index=page_index,
            bbox=bbox,
            caption_bbox=_rect(raw.get("caption_bbox")),
            source_text=source_text,
            block_type=label or "figure",
        )
    if kind == "table":
        return Table(
            html=str(raw.get("table_html") or ""),
            caption=source_text,
            translated_caption=translated_text,
            page_index=page_index,
            bbox=bbox,
            caption_bbox=_rect(raw.get("caption_bbox")),
            cells=_cells_of(raw),
            source_text=source_text,
            block_type=label or "table",
        )
    if kind == "paragraph":
        return Paragraph(
            runs=[TextRun(translated_text or source_text)],
            page_index=page_index,
            bbox=bbox,
            source_text=source_text,
            block_type=label,
        )
    return None


def _cells_of(raw: dict) -> list[TableCell]:
    table = raw.get("table")
    if not isinstance(table, dict):
        return []
    cells: list[TableCell] = []
    for row in table.get("rows") or []:
        if not isinstance(row, list):
            continue
        for cell in row:
            if not isinstance(cell, dict):
                continue
            cells.append(
                TableCell(
                    text=str(cell.get("text") or ""),
                    bbox=_rect(cell.get("bbox")),
                    translated=str(cell.get("translated_text") or ""),
                )
            )
    return cells


def figures_of(manifest: dict, key: str) -> list[Figure]:
    figures: list[Figure] = []
    for raw in manifest.get(key) or []:
        if not isinstance(raw, dict):
            continue
        bbox = _rect(raw.get("bbox"))
        figures.append(
            Figure(
                kind="table" if key == "tables" else "figure",
                caption=str(raw.get("caption") or ""),
                translated_caption=str(raw.get("translated_caption") or ""),
                page_index=int(raw.get("page_index") or 0),
                bbox=bbox,
                logical_id=str(raw.get("logical_id") or ""),
            )
        )
    return figures


def references_of(manifest: dict) -> list[ReferenceEntry]:
    entries: list[ReferenceEntry] = []
    for index, raw in enumerate(manifest.get("references") or [], start=1):
        if not isinstance(raw, dict):
            continue
        text = str(raw.get("text") or "").strip()
        if not text:
            continue
        entries.append(ReferenceEntry(index=index, text=text))
    return entries


def load_document_manifest(path: Path) -> DocumentManifest:
    """Load a manifest and build every reader-facing structure from it."""
    payload = load_manifest(path)
    return DocumentManifest(
        schema_version=str(payload.get("schema_version") or ""),
        source_pdf=str(payload.get("source_pdf") or ""),
        source_sha256=str(payload.get("source_sha256") or ""),
        mode_label=str(payload.get("mode_label") or ""),
        generator=payload.get("generator") or {},
        pages=pages_of(payload),
        blocks=blocks_of(payload),
        figures=figures_of(payload, "figures"),
        tables=figures_of(payload, "tables"),
        references=references_of(payload),
        glossary=[
            {"source": str(item.get("source") or ""), "target": str(item.get("target") or "")}
            for item in (payload.get("glossary") or [])
            if isinstance(item, dict)
        ],
    )


def outline_of(manifest: DocumentManifest) -> list[dict]:
    """Nest the titles into the outline the reader renders."""
    outline: list[dict] = []
    stack: list[tuple[int, dict]] = []
    for block in manifest.blocks:
        if not isinstance(block, Title):
            continue
        title = (block.text or block.source_text).strip()
        if not title:
            continue
        level = max(1, min(int(block.level or 1), 6))
        item = {"title": title, "page_index": block.page_index, "items": []}
        while stack and stack[-1][0] >= level:
            stack.pop()
        if stack:
            stack[-1][1]["items"].append(item)
        else:
            outline.append(item)
        stack.append((level, item))
    return outline
