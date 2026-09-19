"""Library-wide search, document structure, and BibTeX export."""

import re

from fastapi import APIRouter, HTTPException, Query

from app.models.store import list_documents, require_document
from app.services.document_structure import build_document_structure


router = APIRouter()


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", (text or "").lower())


@router.get("/document/{document_id}/structure")
def get_structure(
    document_id: str
) -> dict:
    record = require_document(document_id)
    return build_document_structure(record)


@router.get("/search")
def search_library(
    q: str = Query(..., min_length=1),
) -> list[dict]:
    """Substring search across the user's parsed documents.

    A personal library holds tens of documents, so a normalized scan over the
    stored texts is fast enough and handles CJK better than FTS tokenizers.
    """
    needle = _normalize(q)
    if not needle:
        return []
    hits: list[dict] = []
    for record in list_documents():
        if record.status != "done":
            continue
        for side, text in (("translated", record.translated_text), ("original", record.extracted_text)):
            if not text:
                continue
            normalized_text = _normalize(text)
            at = normalized_text.find(needle)
            if at < 0:
                continue
            ratio = at / max(1, len(normalized_text))
            char_at = min(len(text), int(ratio * len(text)))
            snippet = " ".join(text[char_at : char_at + 160].split())
            hits.append(
                {
                    "document_id": record.document_id,
                    "document_title": str(record.metadata.get("title") or "")
                    or record.source_filename
                    or record.document_id,
                    "side": side,
                    "snippet": snippet,
                    "position_ratio": round(ratio, 4),
                }
            )
            break
        if len(hits) >= 20:
            break
    return hits


def _bibtex_key(record, metadata: dict) -> str:
    author = (metadata.get("authors") or "").split(",")[0].split()[-1:] or ["paper"]
    author_part = re.sub(r"[^a-zA-Z]", "", author[0]).lower() or "paper"
    year = metadata.get("year") or ""
    title_word = re.sub(r"[^a-zA-Z]", "", (metadata.get("title") or record.source_filename).split()[0] if (metadata.get("title") or record.source_filename).split() else "untitled").lower()
    return f"{author_part}{year}{title_word}"


def _generate_bibtex(record, metadata: dict) -> str:
    key = _bibtex_key(record, metadata)
    title = metadata.get("title") or (record.source_filename or record.document_id).rsplit(".", 1)[0]
    fields = [f"  title = {{{title}}}"]
    if metadata.get("authors"):
        fields.append(f"  author = {{{metadata['authors']}}}")
    if metadata.get("year"):
        fields.append(f"  year = {{{metadata['year']}}}")
    if metadata.get("venue"):
        fields.append(f"  journal = {{{metadata['venue']}}}")
    if metadata.get("doi"):
        fields.append(f"  doi = {{{metadata['doi']}}}")
    if metadata.get("eprint"):
        fields.append(f"  eprint = {{{metadata['eprint']}}}")
        fields.append("  archivePrefix = {arXiv}")
    entry_type = "article" if metadata.get("venue") else "misc"
    return f"@{entry_type}{{{key},\n" + ",\n".join(fields) + "\n}"


@router.get("/document/{document_id}/bibtex")
def get_bibtex(
    document_id: str
) -> dict:
    record = require_document(document_id)
    metadata = dict(record.metadata or {})
    if not metadata:
        raise HTTPException(status_code=404, detail="此文档暂无可导出的元数据")
    return {
        "bibtex": _generate_bibtex(record, metadata),
        "filename": f"{_bibtex_key(record, metadata)}.bib",
    }
