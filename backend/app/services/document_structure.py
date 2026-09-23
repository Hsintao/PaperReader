"""Backend-generated document outline and figure gallery.

Structure comes from the worker's manifest: titles become the outline, figures
and tables become gallery entries with a preview cropped from the page. The
source side crops the exact box the manifest reports; the translated side has no
structure of ours, so its caption is located in the translated PDF's text layer.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path

from app.core.config import settings
from app.models.store import DocumentRecord
from app.services.document_manifest import DocumentManifest, Figure, outline_of

_FIGURE_LIMIT = 200
_CROP_MARGIN = 4

# PDFium is not thread-safe, and FastAPI runs sync endpoints on a threadpool,
# so every pdfium call in this process is serialized through one lock.
_PDFIUM_LOCK = threading.Lock()


def _url_for(path: Path) -> str | None:
    try:
        relative = path.resolve().relative_to(settings.data_dir.resolve())
    except ValueError:
        return None
    return "/data/" + relative.as_posix()


def _manifest_for(record: DocumentRecord) -> DocumentManifest | None:
    path = settings.output_dir / record.document_id / "extraction" / "manifest.json"
    if not path.is_file():
        return None
    from app.services.document_manifest import load_document_manifest

    try:
        return load_document_manifest(path)
    except Exception:  # noqa: BLE001 - a broken manifest is a missing one
        return None


def _gallery(manifest: DocumentManifest) -> list[Figure]:
    """Figures and tables in reading order, capped for the gallery."""
    entries: list[Figure] = []
    for page in manifest.pages:
        for figure in (*manifest.figures, *manifest.tables):
            if figure.page_index == page.index:
                entries.append(figure)
        if len(entries) >= _FIGURE_LIMIT:
            break
    return entries[:_FIGURE_LIMIT]


def _crop_from_bbox(
    bbox: tuple[float, float, float, float], page_size: tuple[float, float]
) -> tuple[float, float, float, float]:
    """Turn a manifest box into pypdfium2's crop box, with a little margin.

    Both are in PDF user space, but a crop box is expressed as the distance
    from each page edge: (left, bottom, right, top).
    """
    width, height = page_size
    x0, y0, x1, y1 = bbox
    return (
        max(0.0, x0 - _CROP_MARGIN),
        max(0.0, y0 - _CROP_MARGIN),
        max(0.0, width - x1 - _CROP_MARGIN),
        max(0.0, height - y1 - _CROP_MARGIN),
    )


def _artwork_crop(page, textpage, start: int, length: int, kind: str) -> tuple | None:
    """Locate embedded artwork or table rules beside a caption in page coordinates."""
    import pypdfium2.raw as pdfium_c

    boxes = [textpage.get_charbox(i) for i in range(start, start + length)]
    caption = (min(b[0] for b in boxes), min(b[1] for b in boxes),
               max(b[2] for b in boxes), max(b[3] for b in boxes))
    other_captions = []
    for match in re.finditer(r"^\s*((?:Figure|Fig\.?|Table|图|表)\s*\d+\s*[.:：．])", textpage.get_text_bounded(), re.M):
        search = textpage.search(match.group(1))
        try:
            found = search.get_next()
        finally:
            search.close()
        if found and found[0] != start:
            other_captions.append(textpage.get_charbox(found[0]))

    def same_region(box):
        low, high = sorted(((caption[1] + caption[3]) / 2, (box[1] + box[3]) / 2))
        return not any(box[0] - 4 <= c[0] <= box[2] + 4 and low < c[1] < high
                       for c in other_captions)

    candidates = []
    for obj in page.get_objects(max_depth=1):
        box = obj.get_pos()
        width, height = box[2] - box[0], box[3] - box[1]
        if kind == "table":
            usable = obj.type == pdfium_c.FPDF_PAGEOBJ_PATH and width >= 36 and height <= 3
        else:
            usable = obj.type in (pdfium_c.FPDF_PAGEOBJ_IMAGE, pdfium_c.FPDF_PAGEOBJ_FORM) and width >= 12 and height >= 12
        if usable and same_region(box):
            candidates.append(box)
    nearby = [box for box in candidates if box[0] <= caption[2] and box[2] >= caption[0]]
    if not nearby:
        return None

    def distance(box):
        return max(0, caption[1] - box[3], box[1] - caption[3])

    nearest = min(nearby, key=distance)
    if distance(nearest) > 72:
        return None
    if kind == "table":
        # Full-width rules delimit the table; cmidrules and text stay inside.
        selected = [box for box in candidates
                    if abs(box[0] - nearest[0]) < 3 and abs(box[2] - nearest[2]) < 3
                    and (box[1] > caption[3]) == (nearest[1] > caption[3])]
        if len(selected) < 2:
            return None
    else:
        selected = [nearest]
        # Separate images/forms can make up a single multi-panel figure.
        remaining = [box for box in candidates if box != nearest]
        while remaining:
            adjacent = [box for box in remaining if any(
                max(0, box[0] - b[2], b[0] - box[2]) <= 12
                and max(0, box[1] - b[3], b[1] - box[3]) <= 12 for b in selected)]
            if not adjacent:
                break
            selected.extend(adjacent)
            remaining = [box for box in remaining if box not in adjacent]
    width, height = page.get_size()
    return (max(0, min(b[0] for b in selected) - 4),
            max(0, min(b[1] for b in selected) - 4),
            max(0, width - max(b[2] for b in selected) - 4),
            max(0, height - max(b[3] for b in selected) - 4))


def _float_crop(page, item: dict, anchors: list[tuple[float, float]]) -> tuple | None:
    """Crop artwork beside the caption, falling back to a TeX float anchor."""
    textpage = page.get_textpage()
    try:
        label = re.match(r".*?\d+\s*[.:：．]", item.get("locate_text", ""))
        if not label:
            return None
        search = textpage.search(label.group())
        hits: list[tuple[int, int]] = []
        try:
            while (found := search.get_next()) and len(hits) < 8:
                hits.append(found)
        finally:
            search.close()
        if not hits:
            return None
        for start, length in hits:
            crop = _artwork_crop(page, textpage, start, length, item["kind"])
            if crop:
                return crop
        start, length = hits[0]
        left, bottom, _, top = textpage.get_charbox(start)
        preceding = [(x, y) for x, y in anchors if abs(x - left) < 20 and y >= top]
        if not preceding:
            return None
        x, y = min(preceding, key=lambda point: point[1])
        width, height = page.get_size()
        line = textpage.get_text_range(start, min(300, textpage.count_chars() - start)).splitlines()[0]
        right = max(textpage.get_charbox(i)[2] for i in range(start, start + max(length, len(line))))
        crop_right = width - 24 if left >= width / 2 or right > width / 2 else width / 2 - 8
        return (max(0, x - 4), max(0, bottom - 4), max(0, width - crop_right), max(0, height - y - 4))
    finally:
        textpage.close()


def _render_crop(page, crop, preview: Path, source_mtime: float) -> None:
    if preview.exists() and preview.stat().st_mtime >= source_mtime:
        return
    bitmap = page.render(scale=0.8, crop=crop or (0, 0, 0, 0))
    try:
        bitmap.to_pil().save(preview)
    finally:
        bitmap.close()


def _original_previews(
    record: DocumentRecord, figures: list[Figure]
) -> list[dict]:
    """Crop each figure from the source page using the manifest's own box."""
    import pypdfium2 as pdfium

    url = record.original_pdf_url
    path = settings.data_dir / url.removeprefix("/data/") if url and url.startswith("/data/") else None
    preview_dir = settings.output_dir / record.document_id / "figure-previews"
    result: list[dict] = []
    pdf = pdfium.PdfDocument(str(path)) if path and path.is_file() else None
    try:
        for index, figure in enumerate(figures):
            item = figure.as_dict()
            page_index = figure.page_index
            if pdf is None or not (0 <= page_index < len(pdf)):
                result.append(item)
                continue
            page = pdf[page_index]
            try:
                size = page.get_size()
                preview_dir.mkdir(parents=True, exist_ok=True)
                preview = preview_dir / f"original-crop-{index + 1}.png"
                if figure.bbox:
                    _render_crop(
                        page,
                        _crop_from_bbox(figure.bbox, size),
                        preview,
                        path.stat().st_mtime,
                    )
            finally:
                page.close()
            if preview.exists():
                item["url"] = _url_for(preview) or ""
            result.append(item)
    finally:
        if pdf is not None:
            pdf.close()
    return result


def _translated_previews(record: DocumentRecord, figures: list[dict]) -> list[dict]:
    """Locate each caption in the translated PDF and crop the artwork beside it."""
    import pypdfium2 as pdfium
    from pypdf import PdfReader

    url = record.translated_pdf_url
    if not url or not url.startswith("/data/"):
        return []
    path = settings.data_dir / url.removeprefix("/data/")
    if not path.is_file():
        return []
    reader = PdfReader(path)
    pages = [page.extract_text() or "" for page in reader.pages]
    anchors: dict[tuple[int, str], list[tuple[float, float]]] = {}
    for name, destination in reader.named_destinations.items():
        kind = name.split('.')[0]
        if kind in {"figure", "table"} and destination.get('/Left') is not None and destination.get('/Top') is not None:
            anchors.setdefault((reader.get_destination_page_number(destination), kind), []).append(
                (float(destination['/Left']), float(destination['/Top']))
            )
    preview_dir = settings.output_dir / record.document_id / "figure-previews"
    result: list[dict] = []
    pdf = pdfium.PdfDocument(str(path))
    try:
        for figure_index, figure in enumerate(figures):
            item = dict(figure)
            item.pop("url", None)
            caption = str(item.get("caption") or "")
            match = re.match(r"(Figure|Fig\.?|Table|图|表)\s*(\d+)", caption, re.I)
            label = f"{item['kind'].title()} {match.group(2)}" if match else ""
            number = re.search(r"\d+", label)
            page_index = None
            if number:
                names = r"(?:Figure|Fig\.?|图)" if item["kind"] == "figure" else r"(?:Table|表)"
                patterns = (re.compile(rf"^\s*({names}\s*{number.group()}\s*[.:：．][^\n]*)", re.I | re.M),
                            re.compile(rf"({names}\s*{number.group()}\s*[.:：．][^\n]*)", re.I))
                for pattern in patterns:
                    for index, text in enumerate(pages):
                        found = pattern.search(text)
                        if found:
                            page_index = index
                            item["locate_text"] = found.group(1).strip()
                            if len(item.get("caption") or "") <= len(label):
                                item["caption"] = item["locate_text"]
                            break
                    if page_index is not None:
                        break
            if page_index is None or not (0 <= page_index < len(pdf)):
                item["page"] = None
                result.append(item)
                continue
            item["page"] = page_index + 1
            preview_dir.mkdir(parents=True, exist_ok=True)
            preview = preview_dir / f"translated-crop-{figure_index + 1}.png"
            page = pdf[page_index]
            try:
                _render_crop(
                    page,
                    _float_crop(page, item, anchors.get((page_index, item["kind"]), [])),
                    preview,
                    path.stat().st_mtime,
                )
            finally:
                page.close()
            if preview.exists():
                item["url"] = _url_for(preview) or ""
            result.append(item)
    finally:
        pdf.close()
    return result


def build_document_structure(record: DocumentRecord) -> dict:
    manifest = _manifest_for(record)
    if manifest is None or not manifest.pages:
        return {"outline": [], "figures": []}
    figures = _gallery(manifest)
    structure = {"outline": outline_of(manifest), "figures": []}
    with _PDFIUM_LOCK:
        try:
            structure["figures"] = _original_previews(record, figures)
        except Exception:  # noqa: BLE001 - the outline survives a failed crop
            structure["figures"] = [figure.as_dict() for figure in figures]
        try:
            structure["translated_figures"] = _translated_previews(record, structure["figures"])
        except Exception:  # noqa: BLE001 - the source gallery survives
            structure["translated_figures"] = []
    return structure
