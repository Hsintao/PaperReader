"""Per-domain terminology glossary built from the operator's own translations.

Each translation pass extracts a handful of recurring terms; they land in a
pending pool and a background thread folds them into the domain glossary on a
fixed interval. Consolidation is deterministic — no LLM call — so it keeps
working offline and cannot fail a translation.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from app.core.config import settings
from app.services.translation_prompts import DOMAINS, normalize_domain


logger = logging.getLogger(__name__)

_LOCK = threading.RLock()
_REFRESH_STARTED = threading.Event()

_GLOSSARY_VERSION = 1
# The glossary is bounded by file size, not term count: the worker matches the
# whole library against each paragraph offline, so a big library costs nothing
# per request — the file just must not grow without bound.
MAX_GLOSSARY_BYTES = 50 * 1024 * 1024
PROMPT_TERM_LIMIT = 24
_MAX_DOCUMENTS_PER_TERM = 5
_MAX_EN_CHARS = 80


def glossary_dir() -> Path:
    return settings.data_dir / "glossary"


def glossary_path(domain: str) -> Path:
    return glossary_dir() / f"{normalize_domain(domain)}.json"


def pending_path(domain: str) -> Path:
    return glossary_dir() / f"{normalize_domain(domain)}.pending.json"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_payload(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict) or payload.get("version") != _GLOSSARY_VERSION:
        return {}
    return payload


def _read(path: Path) -> list[dict]:
    terms = _read_payload(path).get("terms")
    return [term for term in terms if isinstance(term, dict)] if isinstance(terms, list) else []


def _read_updated_at(path: Path) -> str | None:
    value = _read_payload(path).get("updated_at")
    return value if isinstance(value, str) and value else None


def _write(path: Path, terms: list[dict], *, updated_at: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": _GLOSSARY_VERSION,
        "domain": path.name.split(".", 1)[0],
        "updated_at": updated_at,
        "terms": terms,
    }
    body = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _clean_pair(en: object, zh: object) -> tuple[str, str] | None:
    english = str(en or "").strip()
    chinese = str(zh or "").strip()
    if not english or not chinese:
        return None
    if len(english) > _MAX_EN_CHARS or "\n" in english or "\r" in english:
        return None
    if english.casefold() == chinese.casefold():
        return None
    return english, chinese


def _dominant_rendering(term: dict) -> tuple[str, int]:
    """Return the pool's most-voted Chinese rendering and its vote count."""
    renderings = term.get("renderings")
    votes: dict[str, int] = {}
    if isinstance(renderings, dict):
        for zh, count in renderings.items():
            label = str(zh).strip()
            try:
                value = int(count)
            except (TypeError, ValueError):
                continue
            if label and value > 0:
                votes[label] = value
    if votes:
        return max(votes.items(), key=lambda item: item[1])
    return str(term.get("zh", "")).strip(), max(1, int(term.get("count") or 0))


def _sorted_terms(terms: list[dict], limit: int | None = None) -> list[dict]:
    ordered = sorted(
        terms,
        key=lambda term: (-int(term.get("count") or 0), str(term.get("en", "")).casefold()),
    )
    return ordered[:limit] if limit is not None else ordered


def record_candidate_terms(domain: str, pairs, document_id: str = "") -> None:
    """Accumulate extracted terms into the domain's pending pool.

    Terminology is an optimization: a storage failure must never fail or block
    the translation that produced these terms.
    """
    try:
        cleaned = [pair for pair in (_clean_pair(en, zh) for en, zh in pairs) if pair]
        if not cleaned:
            return
        target = pending_path(domain)
        with _LOCK:
            entries = _read(target)
            index = {
                str(term.get("en", "")).strip().casefold(): term
                for term in entries
                if str(term.get("en", "")).strip()
            }
            now = _utcnow()
            for english, chinese in cleaned:
                key = english.casefold()
                term = index.get(key)
                if term is None:
                    term = {"en": english, "zh": chinese, "count": 0, "documents": []}
                    entries.append(term)
                    index[key] = term
                term["count"] = int(term.get("count") or 0) + 1
                renderings = term.get("renderings")
                if not isinstance(renderings, dict):
                    renderings = {}
                renderings[chinese] = int(renderings.get(chinese) or 0) + 1
                term["renderings"] = renderings
                term["zh"] = _dominant_rendering(term)[0]
                term["last_seen"] = now
                documents = term.get("documents")
                if not isinstance(documents, list):
                    documents = []
                if document_id and document_id not in documents:
                    documents.append(document_id)
                term["documents"] = documents[-_MAX_DOCUMENTS_PER_TERM:]
            _write(target, entries, updated_at=_read_updated_at(target) or now)
    except Exception as exc:  # noqa: BLE001 - never block translation
        logger.warning("Could not record glossary candidates for %s: %s", domain, exc)


def _payload_bytes(domain: str, terms: list[dict]) -> int:
    """The on-disk size of a glossary file holding ``terms``."""
    body = json.dumps(
        {
            "version": _GLOSSARY_VERSION,
            "domain": domain,
            "updated_at": _utcnow(),
            "terms": terms,
        },
        ensure_ascii=False,
        indent=2,
    )
    return len(body.encode("utf-8")) + 1


def _pruned_to_size(domain: str, terms: list[dict]) -> list[dict]:
    """Keep the best-supported terms that fit the glossary's size budget."""
    ordered = _sorted_terms(terms)
    if _payload_bytes(domain, ordered) <= MAX_GLOSSARY_BYTES:
        return ordered
    lo, hi = 0, len(ordered)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _payload_bytes(domain, ordered[:mid]) <= MAX_GLOSSARY_BYTES:
            lo = mid
        else:
            hi = mid - 1
    return ordered[:lo]


def _merge_pending(domain: str, glossary: list[dict], pending: list[dict]) -> list[dict]:
    merged = [dict(term) for term in glossary]
    index = {
        str(term.get("en", "")).strip().casefold(): term
        for term in merged
        if str(term.get("en", "")).strip()
    }
    now = _utcnow()
    for candidate in pending:
        english = str(candidate.get("en", "")).strip()
        chinese, count = _dominant_rendering(candidate)
        if not english or not chinese:
            continue
        key = english.casefold()
        existing = index.get(key)
        if existing is None:
            entry = {
                "en": english,
                "zh": chinese,
                "count": max(1, count),
                "updated_at": now,
            }
            merged.append(entry)
            index[key] = entry
            continue
        existing_count = int(existing.get("count") or 0)
        if str(existing.get("zh", "")).strip() == chinese:
            existing["count"] = existing_count + max(1, count)
        elif count > existing_count:
            # The pool saw this rendering more often than the stored one.
            existing["zh"] = chinese
            existing["count"] = count
        else:
            # Keep the stored rendering but keep the term's evidence growing.
            existing["count"] = existing_count + max(1, count)
        existing["updated_at"] = now
    return _pruned_to_size(domain, merged)


def consolidate_glossary(domain: str) -> dict:
    """Fold the pending pool into the domain glossary.

    The glossary is capped, so a candidate whose votes cannot yet displace an
    existing entry stays in the pool: its votes keep accumulating across
    documents until it earns a slot, instead of being dropped at the cap.
    """
    normalized = normalize_domain(domain)
    target = glossary_path(normalized)
    pending = pending_path(normalized)
    with _LOCK:
        candidates = _read(pending)
        if not candidates:
            return _snapshot(normalized, refresh=False)
        glossary = _read(target)
        merged = _merge_pending(normalized, glossary, candidates)
        admitted = {
            str(term.get("en", "")).strip().casefold()
            for term in merged
            if str(term.get("en", "")).strip()
        }
        leftover = []
        for candidate in candidates:
            english = str(candidate.get("en", "")).strip()
            chinese, _ = _dominant_rendering(candidate)
            if english and chinese and english.casefold() not in admitted:
                leftover.append(candidate)
        _write(target, merged, updated_at=_utcnow())
        _write(pending, leftover, updated_at=_utcnow())
    logger.info(
        "Glossary consolidated for %s: %d candidate term(s) merged, %d kept pending",
        normalized,
        len(candidates) - len(leftover),
        len(leftover),
    )
    return _snapshot(normalized, refresh=False)


def _refresh_due(domain: str) -> bool:
    if not _read(pending_path(domain)):
        return False
    updated_at = _read_updated_at(glossary_path(domain))
    if not updated_at:
        return True
    try:
        last = datetime.fromisoformat(updated_at)
    except ValueError:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    interval = max(1, int(settings.glossary_refresh_interval_minutes)) * 60
    return (datetime.now(timezone.utc) - last).total_seconds() >= interval


def refresh_if_due(domain: str) -> None:
    """Catch up on a missed interval (for example after an app restart)."""
    normalized = normalize_domain(domain)
    with _LOCK:
        if _refresh_due(normalized):
            consolidate_glossary(normalized)


def consolidate_due_domains() -> None:
    for domain in DOMAINS:
        refresh_if_due(domain)


def glossary_terms_for_prompt(domain: str, limit: int | None = PROMPT_TERM_LIMIT) -> list[tuple[str, str]]:
    with _LOCK:
        terms = _read(glossary_path(domain))
    return [
        (str(term.get("en", "")).strip(), str(term.get("zh", "")).strip())
        for term in _sorted_terms(terms, limit)
        if str(term.get("en", "")).strip() and str(term.get("zh", "")).strip()
    ]


def _snapshot(domain: str, *, refresh: bool) -> dict:
    normalized = normalize_domain(domain)
    if refresh:
        refresh_if_due(normalized)
    with _LOCK:
        terms = _read(glossary_path(normalized))
        pending = _read(pending_path(normalized))
        updated_at = _read_updated_at(glossary_path(normalized))
    try:
        size_bytes = glossary_path(normalized).stat().st_size
    except OSError:
        size_bytes = 0
    return {
        "domain": normalized,
        "label": DOMAINS[normalized].label,
        "updated_at": updated_at,
        "term_count": len(terms),
        "pending_count": len(pending),
        "size_bytes": size_bytes,
        "interval_minutes": max(1, int(settings.glossary_refresh_interval_minutes)),
    }


def glossary_snapshot(domain: str, *, refresh: bool = True) -> dict:
    return _snapshot(domain, refresh=refresh)


def delete_glossary_term(domain: str, en: str) -> dict:
    normalized = normalize_domain(domain)
    key = str(en or "").strip().casefold()
    target = glossary_path(normalized)
    with _LOCK:
        terms = [
            term for term in _read(target) if str(term.get("en", "")).strip().casefold() != key
        ]
        if terms or target.is_file():
            _write(target, terms, updated_at=_read_updated_at(target))
    return _snapshot(normalized, refresh=False)


def start_refresh_scheduler() -> None:
    """Start the periodic consolidation thread once per process."""
    if _REFRESH_STARTED.is_set():
        return
    _REFRESH_STARTED.set()
    thread = threading.Thread(target=_refresh_loop, name="glossary-refresh", daemon=True)
    thread.start()


def _refresh_loop() -> None:
    while True:
        interval = max(1, int(settings.glossary_refresh_interval_minutes)) * 60
        time.sleep(interval)
        try:
            consolidate_due_domains()
        except Exception as exc:  # noqa: BLE001 - keep the scheduler alive
            logger.warning("Scheduled glossary refresh failed: %s", exc)
