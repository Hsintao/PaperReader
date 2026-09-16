"""Backend-generated document outline and figure gallery.

PDFs use extracted layout blocks. LaTeX projects use the main document's
included Figure and Table environments, with previews from the compiled PDF.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from app.core.config import settings
from app.models.store import DocumentRecord
from app.services.alignment_service import _content_list_path
from app.services.alignment_service import _plain_target
from app.services.latex_service import flatten_tex_project

_FIGURE_LIMIT = 200


def _caption_text(parts) -> str:
    texts: list[str] = []
    for part in parts or []:
        value = str(part.get("content") or "") if isinstance(part, dict) else str(part)
        value = value.strip()
        if value and value not in texts:
            texts.append(value)
    return " ".join(texts)


def _url_for(path: Path) -> str | None:
    try:
        relative = path.resolve().relative_to(settings.data_dir.resolve())
    except ValueError:
        return None
    return "/data/" + relative.as_posix()


def _figure_entry(block: dict, page_number: int, base_dir: Path) -> dict | None:
    content = block.get("content") or {}
    rel_path = str((content.get("image_source") or {}).get("path") or "")
    if not rel_path:
        return None
    url = _url_for((base_dir / rel_path).resolve())
    if not url:
        return None
    return {
        "kind": "table" if block.get("type") == "table" else "figure",
        "caption": _caption_text(content.get("table_caption") or content.get("image_caption")),
        "page": page_number,
        "url": url,
    }


def _structure_from_blocks(pages, base_dir: Path) -> dict:
    outline: list[dict] = []
    stack: list[tuple[int, dict]] = []
    figures: list[dict] = []
    for page_index, blocks in enumerate(pages):
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            content = block.get("content") or {}
            if block_type == "title":
                title = _caption_text(content.get("title_content"))
                if not title:
                    continue
                try:
                    level = max(1, int(content.get("level") or 1))
                except (TypeError, ValueError):
                    level = 1
                item = {"title": title, "page_index": page_index, "items": []}
                while stack and stack[-1][0] >= level:
                    stack.pop()
                if stack:
                    stack[-1][1]["items"].append(item)
                else:
                    outline.append(item)
                stack.append((level, item))
            elif block_type in ("image", "chart", "table"):
                if len(figures) >= _FIGURE_LIMIT:
                    continue
                entry = _figure_entry(block, page_index + 1, base_dir)
                if entry:
                    figures.append(entry)
    return {"outline": outline, "figures": figures}


def _tex_project_figures(record: DocumentRecord) -> list[dict]:
    text = flatten_tex_project(record.source_path)
    text = re.sub(r"(?<!\\)%[^\n]*", "", text)
    text = text.split(r"\begin{document}", 1)[-1].split(r"\end{document}", 1)[0]
    figures: list[dict] = []
    counters = {"figure": 0, "table": 0}
    for match in re.finditer(r"\\begin\{(figure|table)(\*?)\}(.*?)\\end\{\1\2\}", text, re.DOTALL):
        kind, _, body = match.groups()
        captions = list(re.finditer(r"\\caption(\*?)\s*(?:\[[^\]]*\])?\s*\{", body))
        for caption in captions or [None]:
            value = ""
            if caption:
                depth = 1
                start = caption.end()
                for end in range(start, len(body)):
                    if body[end] == "{" and body[end - 1] != "\\":
                        depth += 1
                    elif body[end] == "}" and body[end - 1] != "\\":
                        depth -= 1
                    if depth == 0:
                        value = _plain_target(body[start:end])
                        break
            numbered = caption is not None and not caption.group(1)
            if numbered:
                counters[kind] += 1
            label = f"{kind.title()} {counters[kind]}" if numbered else kind.title()
            figures.append({"kind": kind, "label": label, "caption": f"{label}: {value}" if value else label,
                            "page": None, "url": ""})
    return figures


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


def _with_pdf_previews(record: DocumentRecord, figures: list[dict], side: str) -> list[dict]:
    """Locate captions in each PDF independently; cache thumbnails by mtime."""
    import pypdfium2 as pdfium
    from pypdf import PdfReader

    url = record.original_pdf_url if side == "original" else record.translated_pdf_url
    if not url or not url.startswith("/data/"):
        return figures if side == "original" else []
    path = settings.data_dir / url.removeprefix("/data/")
    if not path.is_file():
        return figures if side == "original" else []
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
    result = []
    pdf = pdfium.PdfDocument(str(path))
    try:
        for figure_index, figure in enumerate(figures):
            item = dict(figure)
            label = item.get("label") or ""
            if not label:
                match = re.match(r"(Figure|Fig\.?|Table|图|表)\s*(\d+)", item["caption"], re.I)
                if match:
                    label = f"{item['kind'].title()} {match.group(2)}"
            number = re.search(r"\d+", label)
            page_index = None
            if number:
                names = r"(?:Figure|Fig\.?|图)" if item["kind"] == "figure" else r"(?:Table|表)"
                # Extraction can merge subfigure labels onto the caption line
                # ("…bFigure 2:…"), so accept a mid-line label only when no
                # page anchors the caption at a line start.
                patterns = (re.compile(rf"^\s*({names}\s*{number.group()}\s*[.:：．][^\n]*)", re.I | re.M),
                            re.compile(rf"({names}\s*{number.group()}\s*[.:：．][^\n]*)", re.I))
                for pattern in patterns:
                    for index, text in enumerate(pages):
                        match = pattern.search(text)
                        if match:
                            page_index = index
                            item["locate_text"] = match.group(1).strip()
                            if side == "translated" or len(item["caption"]) <= len(label):
                                item["caption"] = item["locate_text"]
                            break
                    if page_index is not None:
                        break
            if page_index is None and side == "original" and item.get("page"):
                page_index = item["page"] - 1
            if page_index is not None:
                item["page"] = page_index + 1
                preview_dir.mkdir(parents=True, exist_ok=True)
                preview = preview_dir / f"{side}-crop-{figure_index + 1}.png"
                if not preview.exists() or preview.stat().st_mtime < path.stat().st_mtime:
                    page = pdf[page_index]
                    try:
                        crop = _float_crop(page, item, anchors.get((page_index, item['kind']), []))
                        bitmap = page.render(scale=0.8, crop=crop or (0, 0, 0, 0))
                        try:
                            bitmap.to_pil().save(preview)
                        finally:
                            bitmap.close()
                    finally:
                        page.close()
                if not item["url"] or side == "translated":
                    item["url"] = _url_for(preview) or ""
            elif side == "translated":
                item["page"] = None
            result.append(item)
    finally:
        pdf.close()
    return result


def build_document_structure(record: DocumentRecord) -> dict:
    if record.source_type in {"tex", "tex_project"}:
        figures = _tex_project_figures(record)
        return {"outline": [], "figures": _with_pdf_previews(record, figures, "original"),
                "translated_figures": _with_pdf_previews(record, figures, "translated")}
    content_path = _content_list_path(record)
    if content_path:
        try:
            pages = json.loads(content_path.read_text(encoding="utf-8"))
            structure = _structure_from_blocks(pages, content_path.parent)
            if structure["outline"] or structure["figures"]:
                structure["translated_figures"] = _with_pdf_previews(record, structure["figures"], "translated")
                return structure
        except Exception:
            pass
    checkpoint = settings.output_dir / record.document_id / "extraction-checkpoint.json"
    if checkpoint.is_file():
        try:
            payload = json.loads(checkpoint.read_text(encoding="utf-8"))
            pages = payload.get("content_blocks")
            if pages:
                structure = _structure_from_blocks(pages, checkpoint.parent)
                if structure["outline"] or structure["figures"]:
                    structure["translated_figures"] = _with_pdf_previews(record, structure["figures"], "translated")
                    return structure
        except Exception:
            pass
    return {"outline": [], "figures": []}
