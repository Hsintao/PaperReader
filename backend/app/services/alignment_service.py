"""Persistent, structure-preserving alignment between source and translation.

The manifest pairs every block's source text with its translation, so the
bilingual locator reads exact pairs rather than reconstructing them. The pairs
are persisted as ``alignment.json`` when a document finishes, which is what the
"locate counterpart" endpoint searches.
"""

from __future__ import annotations

import json
import re
import threading
from difflib import SequenceMatcher
from pathlib import Path

from app.core.config import settings
from app.models.store import DocumentRecord


_ALIGNMENT_FILENAME = "alignment.json"
_LOCK = threading.RLock()


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", (text or "").lower())


def _plain_target(text: str) -> str:
    value = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text or "")
    value = re.sub(r"\$\$.*?\$\$|\$[^$]*\$", " ", value, flags=re.DOTALL)
    value = re.sub(r"<[^>]{1,40}>", " ", value)
    value = re.sub(r"\\(?:label|ref|cite)\{[^{}]*\}", " ", value)
    value = re.sub(r"\\[a-zA-Z]+\*?", "", value)
    value = value.replace("\\_", "_").replace("\\%", "%").replace("\\&", "&")
    value = re.sub(r"[#*_`{}]+", " ", value)
    return " ".join(value.split()).strip()


def save_alignment_entries(record: DocumentRecord, entries: list[dict]) -> Path:
    path = settings.output_dir / record.document_id / _ALIGNMENT_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 2, "entries": entries}
    temporary = path.with_suffix(".tmp")
    with _LOCK:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
    return path


def save_exact_alignment(
    record: DocumentRecord, source_segments: list[str], translated_segments: list[str]
) -> Path | None:
    if not source_segments or len(source_segments) != len(translated_segments):
        return None
    total = max(1, len(source_segments) - 1)
    entries = [
        {
            "index": index,
            "position": index / total,
            "original": source,
            "translated": translated,
            "kind": "translation_segment",
        }
        for index, (source, translated) in enumerate(zip(source_segments, translated_segments))
        if source.strip() and translated.strip()
    ]
    return save_alignment_entries(record, entries) if entries else None


def _read_alignment(path: Path) -> list[dict]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        entries = payload.get("entries") if isinstance(payload, dict) else None
        if isinstance(entries, list):
            return [
                item
                for item in entries
                if isinstance(item, dict) and item.get("original") and item.get("translated")
            ]
    except Exception:
        pass
    return []


def load_alignment_entries(record: DocumentRecord) -> tuple[list[dict], str]:
    path = settings.output_dir / record.document_id / _ALIGNMENT_FILENAME
    entries = _read_alignment(path) if path.is_file() else []
    return (entries, "exact_index") if entries else ([], "legacy_ratio")


def _entry_score(candidate: str, needle: str) -> float:
    if not candidate or not needle:
        return 0.0
    if needle in candidate:
        return 1.0
    if candidate in needle and len(candidate) >= 8:
        return min(0.98, 0.72 + len(candidate) / max(1, len(needle)) * 0.25)
    match = SequenceMatcher(None, needle[:1600], candidate[:4000]).find_longest_match()
    coverage = match.size / max(1, len(needle))
    return coverage * 0.92


def _match_start_ratio(candidate: str, needle: str) -> float:
    """Where the best match of ``needle`` starts inside ``candidate`` (0..1)."""
    if not candidate or not needle:
        return 0.0
    if needle in candidate:
        return candidate.index(needle) / len(candidate)
    if candidate in needle:
        return 0.0
    match = SequenceMatcher(None, needle[:1600], candidate[:4000]).find_longest_match()
    return match.b / len(candidate)


_END_CHARS = "。！？!?\n"
_SENTENCE_SCAN = 200


def _is_sentence_end(target: str, index: int) -> bool:
    if target[index] in _END_CHARS:
        return True
    if target[index] == "." and (
        index + 1 >= len(target) or target[index + 1].isspace()
    ):
        return True
    return False


def _sentence_start(target: str, pos: int) -> int:
    """Move ``pos`` back to the first character after the previous sentence end."""
    pos = min(pos, len(target))
    window = max(0, pos - _SENTENCE_SCAN)
    if pos <= 0:
        return 0
    if _is_sentence_end(target, pos - 1):
        return pos
    for index in range(pos - 1, window - 1, -1):
        if _is_sentence_end(target, index):
            return index + 1
    return window


def _sentence_finish(target: str, start: int, end: int) -> int:
    """Extend ``end`` forward so the fragment stops at a sentence boundary."""
    end = min(end, len(target))
    if end >= len(target) or end <= start or _is_sentence_end(target, end - 1):
        return end
    limit = min(len(target), end + _SENTENCE_SCAN)
    for index in range(end, limit):
        if _is_sentence_end(target, index):
            return index + 1
    return end


def _highlight_span(target: str, start_ratio: float, span_ratio: float) -> str:
    """Slice the sentence-bounded fragment of ``target`` covering the
    proportional window implied by the user's selection inside its block."""
    if not target:
        return ""
    if start_ratio <= 0.02 and span_ratio >= 0.85:
        return target
    length = max(int(span_ratio * len(target)), min(40, len(target) // 4))
    start = _sentence_start(target, round(start_ratio * len(target)))
    end = _sentence_finish(target, start, start + length)
    fragment = target[start:end].strip()
    if len(fragment) >= 0.95 * len(target):
        return target
    return fragment


def proportional_highlight(
    source_text: str, selected_text: str, target_text: str
) -> str:
    """Map the selected fragment's position inside its source block onto the
    counterpart block, so the highlight lands near the matching part instead
    of always at the start of the block."""
    candidate = _normalize(source_text)
    needle = _normalize(selected_text)
    if not candidate or not needle or not target_text:
        return ""
    span_ratio = min(1.0, len(needle) / max(1, len(candidate)))
    return _highlight_span(target_text, _match_start_ratio(candidate, needle), span_ratio)


def locate_in_alignment(
    entries: list[dict], *, source_side: str, selected_text: str, page_ratio: float
) -> tuple[str, float, float, int, str]:
    source_key = "original" if source_side == "original" else "translated"
    target_key = "translated" if source_side == "original" else "original"
    needle = _normalize(selected_text)[:1600]
    if not entries:
        return "", page_ratio, 0.0, 0, ""

    # Lexical match quality decides; page position only breaks ties between
    # equally good candidates (e.g. repeated phrases across sections).
    scored: list[tuple[float, float, int]] = []
    for index, entry in enumerate(entries):
        candidate = _normalize(str(entry.get(source_key) or ""))
        lexical = _entry_score(candidate, needle)
        position = float(entry.get("position", index / max(1, len(entries) - 1)))
        scored.append((lexical, abs(position - page_ratio), index))
    best_lexical = max(item[0] for item in scored)
    _, best_distance, best_index = min(
        (item for item in scored if item[0] >= best_lexical - 0.02),
        key=lambda item: (item[1], item[2]),
    )
    confidence = max(0.0, min(1.0, best_lexical))
    if confidence < 0.55:
        confidence = 0.0
    best = entries[best_index]
    position = float(best.get("position", best_index / max(1, len(entries) - 1)))
    target = _plain_target(str(best.get(target_key) or ""))
    highlight = ""
    if confidence >= 0.55:
        source_text = str(best.get(source_key) or "")
        highlight = proportional_highlight(source_text, selected_text, target)
    return target[:1600], max(0.0, min(1.0, position)), confidence, best_index, highlight
