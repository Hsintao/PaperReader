"""Low-level PDF page surgery for the layout renderer.

A translated page keeps the source page's vector content (rules, table lines,
images, formulas, captions) and replaces only the text that was translated:

1. every text-showing operator whose origin falls inside a replaced block is
   dropped from the page content stream, so no English text stays behind in the
   text layer;
2. the translated text is drawn as an overlay on top.

Removing operators instead of covering them keeps the translated page's text
layer clean: selection and search in the reader only ever see the translation.
"""

from __future__ import annotations

import io
from typing import Iterable, Sequence

from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject,
    ContentStream,
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    NumberObject,
    RectangleObject,
)

Rect = tuple[float, float, float, float]

_MAX_FORM_DEPTH = 4
_SHOW_TEXT_OPERATORS = {b"Tj", b"TJ", b"'", b'"'}


def page_size(page) -> tuple[float, float]:
    box = page.mediabox
    return float(box.width), float(box.height)


def page_origin(page) -> tuple[float, float]:
    box = page.mediabox
    return float(box.left), float(box.bottom)


def point_in_rect(x: float, y: float, rect: Rect) -> bool:
    x0, y0, x1, y1 = rect
    return x0 <= x <= x1 and y0 <= y <= y1


def text_origin_in_rects(origin: tuple[float, float], rects: Sequence[Rect]) -> bool:
    return any(point_in_rect(origin[0], origin[1], rect) for rect in rects)


def _intersects(a, b: Rect) -> bool:
    if not a or len(a) < 4:
        return False
    try:
        ax0, ay0, ax1, ay1 = (float(value) for value in list(a)[:4])
    except (TypeError, ValueError):
        return False
    bx0, by0, bx1, by1 = b
    return not (ax1 <= bx0 or ax0 >= bx1 or ay1 <= by0 or ay0 >= by1)


def _multiply(left: Sequence[float], right: Sequence[float]) -> tuple:
    a, b, c, d, e, f = left
    a2, b2, c2, d2, e2, f2 = right
    return (
        a * a2 + b * c2,
        a * b2 + b * d2,
        c * a2 + d * c2,
        c * b2 + d * d2,
        e * a2 + f * c2 + e2,
        e * b2 + f * d2 + f2,
    )


def _apply(matrix: Sequence[float], x: float, y: float) -> tuple[float, float]:
    a, b, c, d, e, f = matrix
    return (a * x + c * y + e, b * x + d * y + f)


def filter_text_operations(
    operations: Iterable,
    removal_rects: Sequence[Rect],
    origin: tuple[float, float] = (0.0, 0.0),
) -> list:
    """Drop text-showing operators whose text origin sits inside a removal rect.

    Tracks the graphics and text matrices so each show operator's origin is
    known in page space. Glyph advances are not modelled: an operator keeps the
    last explicitly set position, which is where generated PDFs place each line
    of text. Pages whose text is packed into a single show operator are caught
    by the renderer's verification pass and fall back to the original page.
    """
    kept: list = []
    ctm: tuple = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    stack: list = []
    text_matrix = None
    line_matrix: tuple = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    leading = 0.0
    ox, oy = origin

    for operands, operator in operations:
        if operator == b"q":
            stack.append(ctm)
            kept.append((operands, operator))
            continue
        if operator == b"Q":
            if stack:
                ctm = stack.pop()
            kept.append((operands, operator))
            continue
        if operator == b"cm" and len(operands) >= 6:
            ctm = _multiply(tuple(float(value) for value in operands[:6]), ctm)
            kept.append((operands, operator))
            continue
        if operator == b"BT":
            text_matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
            line_matrix = text_matrix
            kept.append((operands, operator))
            continue
        if operator == b"ET":
            text_matrix = None
            kept.append((operands, operator))
            continue
        if operator == b"TL" and operands:
            leading = float(operands[0])
            kept.append((operands, operator))
            continue
        if operator == b"Tm" and len(operands) >= 6:
            text_matrix = line_matrix = tuple(float(value) for value in operands[:6])
            kept.append((operands, operator))
            continue
        if operator in {b"Td", b"TD"} and len(operands) >= 2:
            tx, ty = float(operands[0]), float(operands[1])
            if operator == b"TD" and operands:
                leading = -ty
            line_matrix = _multiply((1.0, 0.0, 0.0, 1.0, tx, ty), line_matrix)
            text_matrix = line_matrix
            kept.append((operands, operator))
            continue
        if operator == b"T*":
            line_matrix = _multiply((1.0, 0.0, 0.0, 1.0, 0.0, -leading), line_matrix)
            text_matrix = line_matrix
            kept.append((operands, operator))
            continue
        if operator in _SHOW_TEXT_OPERATORS:
            matrix = text_matrix or line_matrix
            x, y = _apply(_multiply(matrix, ctm), 0.0, 0.0)
            if text_origin_in_rects((x - ox, y - oy), removal_rects):
                continue
            kept.append((operands, operator))
            continue
        kept.append((operands, operator))
    return kept


def _serialize(reader: PdfReader, operations: list) -> DecodedStreamObject:
    content = ContentStream(None, reader)
    content.operations = operations
    stream = DecodedStreamObject()
    stream.set_data(content.get_data())
    return stream


def _strip_form(writer: PdfWriter, reader: PdfReader, form, rects, origin, depth: int):
    operations = ContentStream(form, reader).operations
    kept = filter_text_operations(operations, rects, origin)
    if len(kept) == len(operations):
        return None
    stripped = _serialize(reader, kept)
    stripped.update(
        {
            NameObject("/Type"): NameObject("/XObject"),
            NameObject("/Subtype"): NameObject("/Form"),
            NameObject("/FormType"): form.get("/FormType", 1),
            NameObject("/BBox"): form["/BBox"],
        }
    )
    if "/Matrix" in form:
        stripped[NameObject("/Matrix")] = form["/Matrix"]
    resources = form.get("/Resources")
    if resources is not None:
        stripped[NameObject("/Resources")] = _strip_form_resources(
            writer, reader, resources.get_object(), rects, origin, depth + 1
        )
    return stripped


def _strip_form_resources(writer, reader, resources, rects, origin, depth: int):
    xobjects = resources.get("/XObject")
    if xobjects is None:
        return resources
    xobjects = xobjects.get_object()
    updated = DictionaryObject()
    changed = False
    for name, reference in xobjects.items():
        target = reference.get_object()
        if (
            isinstance(target, DictionaryObject)
            and target.get("/Subtype") == "/Form"
            and depth < _MAX_FORM_DEPTH
            and _intersects(target.get("/BBox"), (0.0, 0.0, 1e9, 1e9))
        ):
            stripped = _strip_form(writer, reader, target, rects, origin, depth + 1)
            if stripped is not None:
                updated[NameObject(name)] = writer._add_object(stripped)
                changed = True
                continue
        updated[NameObject(name)] = reference
    if not changed:
        return resources
    clone = DictionaryObject()
    for key, value in resources.items():
        clone[NameObject(key)] = (
            writer._add_object(updated) if key == "/XObject" else value
        )
    return writer._add_object(clone)


def remove_text_page(writer: PdfWriter, reader: PdfReader, page, removal_rects):
    """Clone `page` into `writer` with the text inside `removal_rects` removed."""
    target = writer.add_page(page)
    origin = page_origin(page)
    contents = page.get_contents()
    if contents is not None:
        operations = ContentStream(contents, reader).operations
        kept = filter_text_operations(operations, removal_rects, origin)
        target[NameObject("/Contents")] = writer._add_object(_serialize(reader, kept))
    if removal_rects and "/Resources" in page:
        resources = page["/Resources"].get_object()
        stripped = _strip_form_resources(
            writer, reader, resources, removal_rects, origin, 0
        )
        if stripped is not resources:
            target[NameObject("/Resources")] = (
                stripped
                if getattr(stripped, "indirect_reference", None) is not None
                else writer._add_object(stripped)
            )
    return target


def hide_link_borders(page) -> int:
    """Remove the visible border of every Link annotation on a cloned page.

    The source page's link rectangles stay where they were while the translated
    text moves, so their coloured borders end up drawn over unrelated Chinese
    characters. Navigation is what matters, so the border is normalized away:
    ``/Border`` becomes ``[0 0 0]``, ``/BS /W`` becomes 0, the border colour
    ``/C`` is dropped, and a link-only appearance stream is removed. ``/Rect``,
    ``/A`` and ``/Dest`` are preserved untouched, and non-Link annotations
    (highlights, notes, form fields) are left exactly as they were.
    """
    annots = page.get("/Annots")
    if annots is None:
        return 0
    annots = annots.get_object()
    normalized = 0
    for reference in annots:
        annotation = reference.get_object()
        if not isinstance(annotation, DictionaryObject):
            continue
        if annotation.get("/Subtype") != "/Link":
            continue
        annotation[NameObject("/Border")] = ArrayObject(
            [NumberObject(0), NumberObject(0), NumberObject(0)]
        )
        border = annotation.get("/BS")
        border = border.get_object() if border is not None else None
        if isinstance(border, DictionaryObject):
            border[NameObject("/W")] = NumberObject(0)
        else:
            annotation[NameObject("/BS")] = DictionaryObject(
                {
                    NameObject("/Type"): NameObject("/Border"),
                    NameObject("/S"): NameObject("/S"),
                    NameObject("/W"): NumberObject(0),
                }
            )
        if "/C" in annotation:
            del annotation[NameObject("/C")]
        # A link's appearance stream exists only to paint its border; the
        # clickable rectangle is `/Rect`, which stays.
        if "/AP" in annotation:
            del annotation[NameObject("/AP")]
        normalized += 1
    return normalized


def merge_overlay_bytes(
    target_page, overlay_bytes: bytes, clip: Rect | None = None
) -> None:
    """Draw a single-page overlay PDF (same page size) onto `target_page`.

    `merge_page` clips the target to the overlay's crop box, so a page whose
    media box does not start at the origin passes its own box here.
    """
    overlay = PdfReader(io.BytesIO(overlay_bytes)).pages[0]
    if clip is not None:
        overlay.cropbox = RectangleObject(clip)
    target_page.merge_page(overlay)
