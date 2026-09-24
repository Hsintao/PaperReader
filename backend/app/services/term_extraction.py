"""Learn terminology from a finished document, off the operator's wait path.

The worker translates with the operator's curated glossary only: BabelDOC's
automatic term extraction runs its LLM sweep before translating and costs
more than the translation itself. The learning still happens, here — once a
document is done, a daemon thread walks the manifest's source text, asks the
LLM for term candidates in batches, and folds them into the domain's pending
pool. Nothing in this module blocks or fails the document it came from.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from typing import Iterable, Sequence

import requests

from app.services.document_manifest import DocumentManifest
from app.services.glossary_service import glossary_terms_for_prompt, record_candidate_terms

logger = logging.getLogger(__name__)

# Batching mirrors BabelDOC's own extraction pass: ~600 tokens or 12 texts,
# whichever fills first. The batch cap only bounds LLM spend on book-length
# PDFs; terms repeat, so the head of the document is representative.
_BATCH_TOKEN_BUDGET = 600
_BATCH_TEXT_LIMIT = 12
_BATCH_LIMIT = 200
_REQUEST_TIMEOUT = 120
_TARGET_LANGUAGE = "Chinese"

# Adapted from BabelDOC's automatic_term_extractor prompt so background
# extraction produces the same candidates the in-run pass did.
_PROMPT_TEMPLATE = """
You are an expert multilingual terminologist. Extract key terms from the text and translate them into {target_language}.

### Extraction Rules
1. Include only: named entities (people, orgs, locations, theorem/algorithm names, dates) and domain-specific nouns/noun phrases essential to meaning.
2. No full sentences. Ignore function words.
3. Use minimal noun phrases (≤5 words unless a named entity). No generic academic nouns (e.g., model, case, property) unless part of a standard term.
4. No mathematical items: variables (X1, a, ε), symbols (=, +, →, ⊥⊥, ∈), subscripts/superscripts, formula fragments, mappings (T: H1→H2), etc. Keep only natural-language concepts.
5. Extract each term once. Keep order of first appearance.

### Translation Rules
1. Translate each term into {target_language}.
2. If in the reference glossary, use its translation exactly.
3. Keep proper names in original language unless a well-known translation exists.
4. Ensure consistent translations.

{reference_glossary_section}

### Output Format
- Return ONLY a valid JSON array.
- Each element: {{"src": "...", "tgt": "..."}}.
- No comments, no backticks, no extra text.
- If no terms: [].

Input Text:
```
{text_to_process}
```

Return JSON ONLY. NO OTHER TEXT.
Result:
"""


def schedule_extraction(
    *,
    document_id: str,
    manifest: DocumentManifest,
    domain: str,
    api_key: str,
    base_url: str,
    model: str,
) -> threading.Thread | None:
    """Start background term learning for a finished document, if worthwhile."""
    texts = source_texts(manifest)
    if not texts or not (api_key and base_url and model):
        return None
    thread = threading.Thread(
        target=_run,
        kwargs={
            "document_id": document_id,
            "texts": texts,
            "domain": domain,
            "api_key": api_key,
            "base_url": base_url,
            "model": model,
        },
        name=f"term-extraction-{document_id[:8]}",
        daemon=True,
    )
    thread.start()
    return thread


def source_texts(manifest: DocumentManifest) -> list[str]:
    """The manifest's source text, minus blocks that carry no terminology."""
    texts: list[str] = []
    for block in manifest.blocks:
        text = str(getattr(block, "source_text", "") or "").strip()
        # Formulas and numeric-only lines have no terms worth extracting.
        if len(re.findall(r"[A-Za-z]", text)) < 4:
            continue
        texts.append(text)
    return texts


def extract_terms(
    texts: Sequence[str],
    *,
    domain: str,
    api_key: str,
    base_url: str,
    model: str,
) -> list[tuple[str, str]]:
    """Ask the LLM for term candidates over the texts, batch by batch."""
    if not (api_key and base_url and model):
        return []
    pairs: list[tuple[str, str]] = []
    batches = list(_batches(texts))
    for batch in batches[:_BATCH_LIMIT]:
        prompt = _build_prompt(batch, domain)
        try:
            raw = _llm_complete(prompt, api_key=api_key, base_url=base_url, model=model)
        except Exception as exc:  # noqa: BLE001 - a lost batch loses nothing else
            logger.warning("term extraction batch failed: %s", exc)
            continue
        pairs.extend(_parse_terms(raw))
    if len(batches) > _BATCH_LIMIT:
        logger.info("term extraction stopped after %d batches", _BATCH_LIMIT)
    return pairs


def _run(*, document_id: str, texts: list[str], domain: str, api_key: str, base_url: str, model: str) -> None:
    try:
        pairs = extract_terms(texts, domain=domain, api_key=api_key, base_url=base_url, model=model)
        deduped = list(_dedupe(pairs))
        if deduped:
            record_candidate_terms(domain, deduped, document_id)
        logger.info("term extraction for %s learned %d term(s)", document_id, len(deduped))
    except Exception:  # noqa: BLE001 - learning must never surface as a failure
        logger.exception("term extraction failed for document %s", document_id)


def _dedupe(pairs: Iterable[tuple[str, str]]) -> Iterable[tuple[str, str]]:
    """One vote per source term per document; the first rendering wins."""
    seen: set[str] = set()
    for source, target in pairs:
        key = source.casefold()
        if key in seen:
            continue
        seen.add(key)
        yield source, target


def _batches(texts: Sequence[str]) -> Iterable[list[str]]:
    batch: list[str] = []
    tokens = 0
    for text in texts:
        batch.append(text)
        tokens += _approx_tokens(text)
        if tokens > _BATCH_TOKEN_BUDGET or len(batch) >= _BATCH_TEXT_LIMIT:
            yield batch
            batch, tokens = [], 0
    if batch:
        yield batch


def _approx_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _build_prompt(texts: Sequence[str], domain: str) -> str:
    joined = "\n\n".join(texts)
    reference_section = _reference_glossary_section(joined, domain)
    return _PROMPT_TEMPLATE.format(
        target_language=_TARGET_LANGUAGE,
        reference_glossary_section=reference_section,
        text_to_process=joined,
    )


def _reference_glossary_section(text: str, domain: str) -> str:
    """The operator's glossary entries that occur in this batch's text."""
    lowered = text.casefold()
    entries = [
        (source, target)
        for source, target in glossary_terms_for_prompt(domain, limit=None)
        if source.casefold() in lowered
    ]
    if not entries:
        return ""
    lines = "\n".join(f"- {source} → {target}" for source, target in sorted(set(entries)))
    return (
        "Reference Glossary (use these translations exactly when the term appears):\n"
        f"{lines}\n"
    )


def _parse_terms(raw: str) -> list[tuple[str, str]]:
    """The model's answer as (source, target) pairs, tolerating stray prose."""
    text = raw.strip()
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end <= start:
        return []
    try:
        data = json.loads(text[start : end + 1])
    except ValueError:
        return []
    if not isinstance(data, list):
        return []
    pairs: list[tuple[str, str]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        source = str(item.get("src") or "").strip()
        target = str(item.get("tgt") or "").strip()
        if source and target and len(source) < 100:
            pairs.append((source, target))
    return pairs


def _llm_complete(prompt: str, *, api_key: str, base_url: str, model: str) -> str:
    response = requests.post(
        f"{base_url.rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
        },
        timeout=_REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    payload = response.json()
    return str(payload["choices"][0]["message"]["content"] or "")
