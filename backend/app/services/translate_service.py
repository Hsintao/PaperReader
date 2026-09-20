import hashlib
import json
import logging
import os
import re
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, TypeVar

from app.core.config import settings
from app.services.llm_client import LLMOutputTruncatedError, llm_client
from app.services.mineru_layout import (
    Block,
    apply_translations,
    collect_translatable_strings,
    translatable_mask,
)
from app.services.translation_prompts import (
    PROTOCOL_BATCH,
    PROTOCOL_CONCISE,
    PROTOCOL_SINGLE,
    build_system_prompt,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")


def _run_concurrent(
    items: list[T],
    worker: Callable[[int, T], str],
    fallback: Callable[[int, T, Exception], str],
    max_workers: int | None = None,
) -> list[str]:
    """Run `worker(idx, item)` for each item concurrently; on per-item exception
    after all retries are exhausted by the worker, call `fallback(idx, item, exc)`
    so the pipeline never fails wholesale due to a single chunk being rejected.
    Results are returned in the original order.
    """
    if not items:
        return []
    workers = max(1, max_workers or settings.translate_concurrency)
    workers = min(workers, len(items))
    results: list[str] = [""] * len(items)

    def _safe(idx: int, item: T) -> tuple[int, str]:
        try:
            return idx, worker(idx, item)
        except Exception as exc:  # noqa: BLE001 - want to capture all to fallback
            logger.warning("Chunk %d failed after retries, using fallback: %s", idx, exc)
            return idx, fallback(idx, item, exc)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(_safe, i, item) for i, item in enumerate(items)]
        for fut in futures:
            idx, value = fut.result()
            results[idx] = value
    return results


_PLACEHOLDER_PATTERN = re.compile(r"((?<!\\)\$[^$\n]+?(?<!\\)\$|\\\[[\s\S]*?\\\]|\\\([\s\S]*?\\\)|\\(?:cite|ref)\{[^}]+\}|https?://\S+)")
_LATEX_ENV_PATTERN = re.compile(r"\\(?:begin|end)\s*\{[A-Za-z@]+\*?\}")
_MAX_CHARS_PER_CHUNK = 4000
_STRUCTURAL_TAG_PATTERN = re.compile(r"</?[A-Za-z][^>\r\n]*>")
_PLACEHOLDER_TOKEN_RE = re.compile(r"__PR_PH_\d+__")
_UNESCAPED_DOLLAR_RE = re.compile(r"(?<!\\)\$")
_LATEX_FENCE_PATTERN = re.compile(r"^```(?:latex)?\s*|\s*```$", re.MULTILINE)
def _is_escaped_at(text: str, offset: int) -> bool:
    slashes = 0
    offset -= 1
    while offset >= 0 and text[offset] == "\\":
        slashes += 1
        offset -= 1
    return slashes % 2 == 1


def protect_placeholders(text: str) -> tuple[str, dict[str, str]]:
    mapping: dict[str, str] = {}
    source_text = text
    next_index = 0

    def repl(match: re.Match[str]) -> str:
        nonlocal next_index
        token = f"__PR_PH_{next_index:04d}__"
        while token in source_text or token in mapping:
            next_index += 1
            token = f"__PR_PH_{next_index:04d}__"
        next_index += 1
        mapping[token] = match.group(0)
        return token

    # LaTeX environment commands go first: models silently drop or rewrite
    # them (observed: \begin{figure*}/\begin{promptbox} lost mid-document,
    # which left every later \end mispaired and the document uncompilable).
    # As placeholder tokens they are validated verbatim like math and cites.
    text = _LATEX_ENV_PATTERN.sub(repl, text)
    text = _STRUCTURAL_TAG_PATTERN.sub(repl, text)
    return _PLACEHOLDER_PATTERN.sub(repl, text), mapping


def _placeholder_tokens(text: str) -> list[str]:
    return _PLACEHOLDER_TOKEN_RE.findall(text)


def _placeholder_only(text: str, mapping: dict[str, str] | None = None) -> bool:
    if mapping is None:
        remainder = _PLACEHOLDER_TOKEN_RE.sub("", text)
    else:
        remainder = text
        for token in mapping:
            remainder = remainder.replace(token, "")
    return not remainder.strip()


def restore_placeholders(text: str, mapping: dict[str, str]) -> str:
    # Later placeholders may contain earlier ones (a URL immediately followed
    # by a protected closing tag is one example), so unwind in reverse order.
    for key, value in reversed(mapping.items()):
        text = text.replace(key, value)
    return text


def split_text_into_chunks(text: str, max_chars: int = _MAX_CHARS_PER_CHUNK) -> list[str]:
    paragraphs = [paragraph for paragraph in text.split("\n\n") if paragraph.strip()]
    if not paragraphs:
        return [text]

    chunks: list[str] = []
    current_parts: list[str] = []
    current_len = 0

    def flush_current() -> None:
        nonlocal current_parts, current_len
        if current_parts:
            chunks.append("\n\n".join(current_parts).strip())
            current_parts = []
            current_len = 0

    for paragraph in paragraphs:
        paragraph_len = len(paragraph)
        if paragraph_len > max_chars:
            flush_current()
            start = 0
            while start < paragraph_len:
                end = min(start + max_chars, paragraph_len)
                if end < paragraph_len:
                    split_newline = paragraph.rfind("\n", start, end)
                    split_space = paragraph.rfind(" ", start, end)
                    split_at = max(split_newline, split_space)
                    if split_at > start + (max_chars // 2):
                        end = split_at
                piece = paragraph[start:end].strip()
                if piece:
                    chunks.append(piece)
                start = end
            continue

        proposed_len = paragraph_len if not current_parts else current_len + 2 + paragraph_len
        if proposed_len <= max_chars:
            current_parts.append(paragraph)
            current_len = proposed_len
        else:
            flush_current()
            current_parts.append(paragraph)
            current_len = paragraph_len

    flush_current()
    return chunks or [text]


def _fail_incomplete_translation(idx: int, _item: T, exc: Exception) -> str:
    """A chunk that produced nothing at all is omitted, not published in English."""
    logger.warning("Chunk %d produced no usable translation and was omitted: %s", idx + 1, exc)
    return ""


_MAX_TRANSLATION_ATTEMPTS = 4  # first attempt plus three retries with feedback


def _retry_feedback(problem: Exception) -> str:
    """The rejection reason, appended to the prompt for the next attempt."""
    return (
        f"\n\nYour previous attempt was rejected: {problem}. "
        "Translate the same text again and correct this problem. Output only "
        "the Chinese translation, keep every placeholder token exactly once "
        "and in its original order, and add no commentary."
    )


def _salvage_translation(output: str, mapping: dict[str, str]) -> str:
    """Best-effort cleanup of a rejected translation.

    After repeated rejections the model's last output is still preferable to
    the English source: strip fences and control characters, drop placeholder
    tokens the model invented, and restore the tokens that survived.
    """
    text = _strip_code_fences(output).replace("\ufffd", "")
    text = "".join(ch for ch in text if ord(ch) >= 32 or ch in "\n\r\t")
    if mapping:
        text = _PLACEHOLDER_TOKEN_RE.sub(
            lambda match: match.group(0) if match.group(0) in mapping else "",
            text,
        )
        text = restore_placeholders(text, mapping)
    return text.strip()


def _translate_with_feedback(
    source: str,
    mapping: dict[str, str],
    base_prompt: str,
    override_api_key: str | None,
    override_base_url: str | None,
    override_model: str | None,
    *,
    strip_fences: bool,
) -> str:
    """Translate one piece of text, retrying with the rejection reason in the
    prompt. When every attempt is rejected, return the best-effort salvage of
    what the model produced; raise only when it produced nothing usable."""
    feedback = ""
    last_error: Exception | None = None
    salvage = ""
    for _attempt in range(_MAX_TRANSLATION_ATTEMPTS):
        try:
            translated = _translate_complete_chunk(
                source,
                base_prompt + feedback,
                override_api_key,
                override_base_url,
                override_model,
                strip_fences=strip_fences,
            )
        except TranslationValidationError as exc:
            last_error = exc
            if exc.output.strip() and not _looks_untranslated(source, exc.output):
                salvage = exc.output
            feedback = _retry_feedback(exc)
            continue
        except Exception as exc:
            # Transport-level failure: the client already retried internally
            # and there is no model output to give feedback on or salvage.
            last_error = exc
            break
        if _looks_untranslated(source, translated):
            # An echo of the English source is a failure, never a fallback.
            last_error = TranslationValidationError(
                "the output echoes the source text instead of translating it"
            )
            feedback = _retry_feedback(last_error)
            continue
        return translated
    if salvage.strip():
        cleaned = _salvage_translation(salvage, mapping)
        if cleaned:
            logger.warning(
                "Using best-effort translation after %d rejected attempts: %s",
                _MAX_TRANSLATION_ATTEMPTS,
                last_error,
            )
            return cleaned
    raise last_error or TranslationValidationError("translation failed")


_GLOSSARY_SAMPLE_CHARS = 6000
_GLOSSARY_MAX_TERMS = 24


def extract_document_terms(
    title: str | None,
    sample_text: str,
    override_api_key: str | None = None,
    override_base_url: str | None = None,
    override_model: str | None = None,
) -> list[tuple[str, str]]:
    """Extract recurring terminology from one paper via a best-effort LLM pass.

    Batches otherwise translate in isolation and can render the same term
    differently in the abstract and the conclusion. Any failure returns an
    empty list — terminology improves consistency but must never block
    translation.
    """
    try:
        sample = sample_text[:_GLOSSARY_SAMPLE_CHARS]
        if not sample.strip():
            return []
        response = llm_client.chat(
            message=f"Paper title: {title or 'unknown'}\n\n{sample}",
            system_prompt=(
                "You extract domain terminology from an academic paper so later "
                "translation batches stay consistent. Return strict JSON only: "
                '{"terms": [{"en": "...", "zh": "..."}]}. '
                f"Include at most {_GLOSSARY_MAX_TERMS} entries: recurring technical "
                "terms, method or system names, and acronyms, each with its "
                "established Chinese translation. No commentary."
            ),
            override_api_key=override_api_key,
            override_base_url=override_base_url,
            override_model=override_model,
        )
        start = response.find("{")
        end = response.rfind("}")
        if start == -1 or end <= start:
            return []
        data = json.loads(response[start : end + 1])
        terms = data.get("terms") if isinstance(data, dict) else None
        if not isinstance(terms, list):
            return []
        return [
            (str(term["en"]).strip(), str(term["zh"]).strip())
            for term in terms[:_GLOSSARY_MAX_TERMS]
            if isinstance(term, dict)
            and isinstance(term.get("en"), str)
            and isinstance(term.get("zh"), str)
            and term["en"].strip()
            and term["zh"].strip()
        ]
    except Exception as exc:
        logger.info("Terminology extraction skipped: %s", exc)
        return []


def _translate_complete_chunk(
    text: str,
    system_prompt: str,
    override_api_key: str | None,
    override_base_url: str | None,
    override_model: str | None,
    *,
    strip_fences: bool,
    ordered: bool = True,
) -> str:
    """Translate one bounded chunk, recursively shrinking on output truncation."""
    try:
        translated = llm_client.chat(
            message=text,
            system_prompt=system_prompt,
            override_api_key=override_api_key,
            override_base_url=override_base_url,
            override_model=override_model,
        )
    except LLMOutputTruncatedError:
        # Halve the source at a natural boundary. At the minimum size, surface
        # the failure instead of returning a knowingly partial translation.
        if len(text) <= 300:
            raise
        smaller = split_text_into_chunks(text, max_chars=max(300, len(text) // 2))
        if len(smaller) <= 1:
            raise
        return "\n\n".join(
            _translate_complete_chunk(
                part,
                system_prompt,
                override_api_key,
                override_base_url,
                override_model,
                strip_fences=strip_fences,
                ordered=ordered,
            )
            for part in smaller
        )

    translated = _normalize_translation(text, translated, ordered=ordered)
    cleaned = _strip_code_fences(translated) if strip_fences else translated.strip()
    cleaned = _normalize_translation(text, cleaned, ordered=ordered)
    return cleaned


def translate_text(
    text: str,
    override_api_key: str | None = None,
    override_base_url: str | None = None,
    override_model: str | None = None,
    *,
    checkpoint_path: Path | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
    translation_context: str = "",
    domain: str = "",
) -> str:
    protected_text, mapping = protect_placeholders(text)
    chunks = split_text_into_chunks(protected_text)
    system_prompt = build_system_prompt(
        domain, protocol=PROTOCOL_SINGLE, context=translation_context
    )

    checkpoint_entries = _load_translation_checkpoint(checkpoint_path)
    translated_chunks: list[str] = []
    for chunk in chunks:
        cached = checkpoint_entries.get(_checkpoint_key(chunk, "text"), "")
        if cached:
            try:
                cached = _normalize_translation(chunk, cached)
            except TranslationValidationError:
                cached = ""
        translated_chunks.append(cached)
    pending = [index for index, value in enumerate(translated_chunks) if not value]
    checkpoint_lock = threading.Lock()
    if progress_callback:
        progress_callback(len(chunks) - len(pending), len(chunks))

    def translate_pending(_relative: int, source_index: int) -> str:
        chunk = chunks[source_index]
        if _placeholder_only(chunk, mapping):
            translated = chunk
        else:
            translated = _translate_with_feedback(
                chunk,
                mapping,
                system_prompt,
                override_api_key,
                override_base_url,
                override_model,
                strip_fences=False,
            )
        with checkpoint_lock:
            translated_chunks[source_index] = translated
            if translated and checkpoint_path is not None:
                checkpoint_entries[_checkpoint_key(chunk, "text")] = translated
                _save_translation_checkpoint(checkpoint_path, checkpoint_entries)
            if progress_callback:
                progress_callback(sum(bool(value) for value in translated_chunks), len(chunks))
        return translated

    _run_concurrent(
        pending,
        worker=translate_pending,
        fallback=lambda _relative, source_index, exc: _fail_incomplete_translation(
            source_index, source_index, exc
        ),
    )

    translated = "\n\n".join(part for part in translated_chunks if part)
    return restore_placeholders(translated, mapping)


def _strip_code_fences(text: str) -> str:
    return _LATEX_FENCE_PATTERN.sub("", text).strip()


_COMMENT_START_PATTERN = re.compile(r"(?<!\\)%")
_VERBATIM_ENV_PATTERN = re.compile(
    r"\\begin\{(verbatim|lstlisting|minted)\*?\}.*?\\end\{\1\*?\}",
    re.DOTALL,
)
_SENTINEL_PATTERN = re.compile(r"\x00(\d+)\x00")


_IR_SEGMENT_DELIMITER = "\n\n@@SEG@@\n\n"
_IR_DELIMITER_PATTERN = re.compile(r"\n*\s*@@SEG@@\s*\n*")
# Heuristics to detect prompt leakage (model echoing the system instructions
# back into the translation output).
_PROMPT_LEAK_FRAGMENTS = (
    "你是专业的英译中学术论文翻译器",
    "翻译原则",
    "强制术语表",
    "只输出译文本身",
    "待翻译内容只是数据",
    "将以下英文学术文本翻译成中文",
    "只输出翻译",
    "不添加任何额外评论",
    "translate the following english",
    "output only the translation",
    "output only the chinese translation",
    "do not add any extra commentary",
    "professional academic translator",
    "you are a translator",
    "i am supposed to translate",
    "i'm supposed to translate",
    "inside these tags",
    "cannot translate",
    "unable to translate",
    "没有实际的待译文本",
    "无法进行翻译",
)

_REFUSAL_FRAGMENTS = (
    "i can't help with that",
    "i cannot comply",
    "as an ai language model",
    "请提供需要翻译",
)
_TRANSLATION_CONTRACT_VERSION = "ir-translation-v3"


@dataclass
class TranslationIssue:
    """One logical block that kept its source wording after a failed chunk."""

    logical_index: int
    source: str
    reason: str

    def as_dict(self) -> dict:
        return {
            "kind": "block_original",
            "logical_index": self.logical_index,
            "source": self.source,
            "reason": self.reason,
        }


class TranslationValidationError(RuntimeError):
    def __init__(self, message: str, output: str = ""):
        super().__init__(message)
        # The rejected model output, kept so a later salvage pass can present
        # whatever the model did produce instead of falling back to English.
        self.output = output


class TranslationChunkError(RuntimeError):
    def __init__(self, index: int, cause: Exception):
        super().__init__(str(cause))
        self.index = index


def _repair_lost_dollar_escapes(source: str, translated: str) -> str:
    """Re-escape literal ``$`` whose backslash the model dropped.

    Real math is placeholder-protected before the model sees a segment, so
    when the source holds no unescaped ``$`` every unescaped ``$`` in the
    output is escaped currency that lost its backslash. Restoring it keeps
    prose such as ``Big & Tall`` out of fake inline math at render time; a
    re-translation could not be relied on here because a temperature-zero
    model reproduces the same corruption deterministically.
    """
    if _UNESCAPED_DOLLAR_RE.search(source) or not _UNESCAPED_DOLLAR_RE.search(translated):
        return translated
    return _UNESCAPED_DOLLAR_RE.sub(r"\\$", translated)


def _normalize_translation(source: str, translated: str, *, ordered: bool = True) -> str:
    """Repair deterministic corruptions first, then validate the result."""
    # U+FFFD carries no recoverable content and renders as a blank glyph in
    # XeLaTeX. Models can also insert it between duplicated neighboring
    # characters (for example 发�现), where removal restores the intended word.
    repaired = translated.replace("\ufffd", "")
    repaired = _repair_lost_dollar_escapes(source, repaired)
    _validate_translation(source, repaired, ordered=ordered)
    return repaired


def _comparable_text(text: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", (text or "").lower())


def _looks_untranslated(source: str, translated: str) -> bool:
    """True when the "translation" is the source text handed straight back.

    A model that echoes its input used to be cached as a valid translation, so
    the page stayed English forever after: every later run reused the echoed
    text instead of asking again.
    """
    if not source or not translated:
        return False
    if len(re.findall(r"[A-Za-z]", source)) < 8:
        return False
    return _comparable_text(source) == _comparable_text(translated)


def _validate_translation(source: str, translated: str, *, ordered: bool = True) -> None:
    """Reject structurally unsafe or clearly non-translation model output.

    ``ordered=False`` compares placeholder tokens as a multiset instead of in
    sequence: callers that restore placeholders by token key (LaTeX body
    chunks) tolerate the reordering a target language's word order produces,
    while the IR path maps translations positionally and needs the sequence.
    """
    if not translated.strip():
        raise TranslationValidationError("empty translation", output=translated)
    if "```" in translated:
        raise TranslationValidationError("unexpected code fence", output=translated)
    controls = [ch for ch in translated if ord(ch) < 32 and ch not in "\n\r\t"]
    if controls:
        raise TranslationValidationError("unsafe control character", output=translated)
    source_tokens = _placeholder_tokens(source)
    translated_tokens = _placeholder_tokens(translated)
    if (source_tokens != translated_tokens if ordered
            else Counter(source_tokens) != Counter(translated_tokens)):
        raise TranslationValidationError("placeholder count or order changed", output=translated)
    # Real math is placeholder-protected before the model sees a segment, so a
    # source without unescaped ``$`` must never gain one: losing the backslash
    # of ``\$10.99`` would later pair into fake inline math and swallow prose
    # such as ``Big & Tall`` into math mode.
    if not _UNESCAPED_DOLLAR_RE.search(source) and _UNESCAPED_DOLLAR_RE.search(translated):
        raise TranslationValidationError("unescaped '$' introduced (escaped currency/math lost)", output=translated)
    if _STRUCTURAL_TAG_PATTERN.findall(source) != _STRUCTURAL_TAG_PATTERN.findall(translated):
        raise TranslationValidationError("unexpected structural tag", output=translated)
    low_source = source.lower()
    low_output = translated.lower()
    for fragment in _PROMPT_LEAK_FRAGMENTS + _REFUSAL_FRAGMENTS:
        if fragment in low_output and fragment not in low_source:
            raise TranslationValidationError("model meta-commentary or refusal detected", output=translated)
    if re.search(r"\\(?:documentclass|begin\{document\}|usepackage)\b", translated):
        raise TranslationValidationError("unexpected document structure", output=translated)
    if len(translated) > max(800, len(source) * 5):
        raise TranslationValidationError("abnormal output expansion", output=translated)


def _batch_segments(segments: list[str], max_chars: int) -> list[list[int]]:
    """Group segment indices into batches whose joined length stays under
    `max_chars`. Each segment is contributed individually if it alone exceeds
    the budget."""
    batches: list[list[int]] = []
    current: list[int] = []
    current_len = 0
    delim_len = len(_IR_SEGMENT_DELIMITER)
    for idx, seg in enumerate(segments):
        seg_len = len(seg)
        proposed = seg_len if not current else current_len + delim_len + seg_len
        if current and proposed > max_chars:
            batches.append(current)
            current = [idx]
            current_len = seg_len
        else:
            current.append(idx)
            current_len = proposed
    if current:
        batches.append(current)
    return batches


def _translate_segment_batch(
    segments: list[str],
    override_api_key: str | None,
    override_base_url: str | None,
    override_model: str | None,
    on_result: Callable[[int, str], None] | None = None,
    translation_context: str = "",
    domain: str = "",
) -> list[str]:
    if not segments:
        return []

    def emit(index: int, value: str, results: list[str]) -> None:
        results.append(value)
        if on_result:
            on_result(index, value)

    def translate_individually() -> list[str]:
        results: list[str] = []
        for index, segment in enumerate(segments):
            try:
                translated = _translate_single_segment(
                    segment,
                    override_api_key,
                    override_base_url,
                    override_model,
                    translation_context,
                    domain,
                )
            except Exception as exc:
                raise TranslationChunkError(index, exc) from exc
            emit(index, translated, results)
        return results

    if len(segments) == 1:
        return translate_individually()

    # Protect any residual $...$ / \[...\] math that survived as plain text in
    # a TextRun (e.g. when MinerU didn't split the paragraph into runs).
    protected_segments: list[str] = []
    mappings: list[dict[str, str]] = []
    for seg in segments:
        p, m = protect_placeholders(seg)
        protected_segments.append(p)
        mappings.append(m)

    joined = _IR_SEGMENT_DELIMITER.join(protected_segments)
    system_prompt = build_system_prompt(
        domain, protocol=PROTOCOL_BATCH, context=translation_context
    )
    try:
        response = llm_client.chat(
            message=joined,
            system_prompt=system_prompt,
            override_api_key=override_api_key,
            override_base_url=override_base_url,
            override_model=override_model,
        )
    except Exception as exc:
        # A rejected/truncated batch can still be recovered safely as smaller,
        # individually validated requests.
        logger.warning("Batched translation failed; retrying segments individually: %s", exc)
        return translate_individually()
    response = response.strip()
    parts = [p.strip() for p in _IR_DELIMITER_PATTERN.split(response)]
    parts = [p for p in parts if p]
    if len(parts) == len(segments):
        results: list[str] = []
        for index, (source, translated, mapping) in enumerate(
            zip(protected_segments, parts, mappings)
        ):
            try:
                translated = _normalize_translation(source, translated)
            except TranslationValidationError as exc:
                logger.warning("Invalid batch member; retrying only that segment: %s", exc)
                try:
                    value = _translate_single_segment(
                        source,
                        override_api_key,
                        override_base_url,
                        override_model,
                        translation_context,
                        domain,
                    )
                except Exception as retry_exc:
                    raise TranslationChunkError(index, retry_exc) from retry_exc
            else:
                value = restore_placeholders(translated, mapping)
            emit(index, value, results)
        return results
    # Fallback: translate each segment individually to recover from a malformed batch.
    return translate_individually()


def _translate_single_segment(
    text: str,
    override_api_key: str | None,
    override_base_url: str | None,
    override_model: str | None,
    translation_context: str = "",
    domain: str = "",
) -> str:
    stripped = text.strip()
    if not stripped:
        return text
    # Protect any residual $...$ / \[...\] math in the text run before sending
    # to the LLM, then restore afterwards so the formula is never re-translated.
    protected, mapping = protect_placeholders(stripped)
    if _placeholder_only(protected, mapping if mapping else None):
        return restore_placeholders(protected, mapping)
    system_prompt = build_system_prompt(
        domain, protocol=PROTOCOL_SINGLE, context=translation_context
    )
    translated = _translate_with_feedback(
        protected,
        mapping,
        system_prompt,
        override_api_key,
        override_base_url,
        override_model,
        strip_fences=True,
    )
    cleaned = restore_placeholders(translated.strip(), mapping)
    if not cleaned:
        raise TranslationValidationError("empty translation")
    return cleaned


def translate_concise(
    text: str,
    override_api_key: str | None = None,
    override_base_url: str | None = None,
    override_model: str | None = None,
    *,
    translation_context: str = "",
    domain: str = "",
) -> str:
    """Brevity-constrained re-translation for a block that did not fit its box.

    The first translation optimizes for fidelity; when the layout search
    reports the text cannot fit above the minimum font size, a second pass
    with an explicit character budget usually lands inside the box.
    """
    stripped = text.strip()
    if not stripped:
        return ""
    budget = max(20, int(len(stripped) * 0.75))
    system_prompt = build_system_prompt(
        domain,
        protocol=PROTOCOL_CONCISE,
        context=translation_context,
        brevity_budget=budget,
    )
    protected, mapping = protect_placeholders(stripped)
    translated = _translate_with_feedback(
        protected,
        mapping,
        system_prompt,
        override_api_key,
        override_base_url,
        override_model,
        strip_fences=True,
    )
    return restore_placeholders(translated.strip(), mapping)


def _checkpoint_key(source: str, namespace: str = "ir") -> str:
    material = f"{_TRANSLATION_CONTRACT_VERSION}\0{namespace}\0{source}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _load_translation_checkpoint(path: Path | None) -> dict[str, str]:
    if path is None or not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    if payload.get("version") != _TRANSLATION_CONTRACT_VERSION:
        return {}
    entries = payload.get("segments")
    if not isinstance(entries, dict):
        return {}
    return {str(key): str(value) for key, value in entries.items() if isinstance(value, str)}


def _save_translation_checkpoint(path: Path, entries: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    temporary.write_text(
        json.dumps(
            {"version": _TRANSLATION_CONTRACT_VERSION, "segments": entries},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def translate_ir(
    ir: list[Block],
    override_api_key: str | None = None,
    override_base_url: str | None = None,
    override_model: str | None = None,
    *,
    checkpoint_path: Path | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
    translation_context: str = "",
    domain: str = "",
    checkpoint_namespace: str = "ir",
) -> tuple[list[str], list[dict]]:
    """Translate the prose content of an IR list in place.

    Math (display + inline) is left untouched. `Title.text`, `TextRun.text`,
    list items, table cells, figure/table captions and affiliation text are
    sent to the LLM, minus the segments `translatable_mask` marks as
    source-language (author names and the bibliography entries).

    A segment whose translation fails is retried with the rejection reason
    fed back into the prompt (three retries). A logical block is atomic: when
    any of its pieces finally fails, the whole block keeps its complete source
    wording instead of publishing a half-translated paragraph, and the failure
    is returned as a structured issue. Returns `(notes, issues)`.
    """
    source_segments = collect_translatable_strings(ir)
    if not source_segments:
        return [], []

    # MinerU occasionally emits a whole page as one TextRun. Split each such
    # logical segment before batching, then reassemble it after translation.
    # Placeholders are protected before the split so math/URLs cannot be cut.
    translatable = translatable_mask(ir)
    segments: list[str] = []
    segment_groups: list[tuple[list[int], dict[str, str]]] = []
    max_segment_chars = max(300, int(settings.translate_segment_max_chars))
    for source_index, source in enumerate(source_segments):
        protected, mapping = protect_placeholders(source)
        pieces = split_text_into_chunks(protected, max_chars=max_segment_chars)
        indices = list(range(len(segments), len(segments) + len(pieces)))
        segments.extend(pieces)
        segment_groups.append((indices, mapping))
    piece_translatable = [
        translatable[group_index]
        for group_index, (indices, _mapping) in enumerate(segment_groups)
        for _ in indices
    ]

    checkpoint_entries = _load_translation_checkpoint(checkpoint_path)
    translations: list[str] = [""] * len(segments)
    for index, segment in enumerate(segments):
        if not piece_translatable[index]:
            translations[index] = segment
            continue
        cached = checkpoint_entries.get(_checkpoint_key(segment, checkpoint_namespace))
        if cached:
            try:
                cached = _normalize_translation(segment, cached)
            except TranslationValidationError:
                continue
            if _looks_untranslated(segment, cached):
                # A cached echo of the source is not a translation: ask again.
                continue
            translations[index] = cached

    pending = [index for index, value in enumerate(translations) if not value]
    relative_batches = _batch_segments(
        [segments[index] for index in pending],
        max(max_segment_chars, settings.translate_batch_max_chars),
    )
    batches = [[pending[index] for index in batch] for batch in relative_batches]
    checkpoint_lock = threading.Lock()
    progress_lock = threading.Lock()
    if progress_callback:
        progress_callback(len(segments) - len(pending), len(segments))

    def _do_batch(_i: int, batch: list[int]) -> str:
        batch_segments = [segments[j] for j in batch]

        def persist_result(relative_index: int, value: str) -> None:
            slot = batch[relative_index]
            try:
                value = _normalize_translation(segments[slot], value)
                usable = not _looks_untranslated(segments[slot], value)
            except TranslationValidationError:
                usable = False
            if not usable:
                # Retry this slot on its own, feeding the rejection reason
                # back into the prompt. A piece that still fails keeps its
                # translation empty, which restores the whole logical block.
                try:
                    value = _translate_single_segment(
                        segments[slot],
                        override_api_key,
                        override_base_url,
                        override_model,
                        translation_context,
                        domain,
                    )
                except Exception as exc:
                    logger.warning("Segment %d could not be translated: %s", slot, exc)
                    return
            with checkpoint_lock:
                translations[slot] = value
                if checkpoint_path is not None:
                    checkpoint_entries[_checkpoint_key(segments[slot], checkpoint_namespace)] = translations[slot]
                    _save_translation_checkpoint(checkpoint_path, checkpoint_entries)
            if progress_callback:
                with progress_lock:
                    progress_callback(sum(bool(item) for item in translations), len(segments))

        try:
            _translate_segment_batch(
                batch_segments,
                override_api_key=override_api_key,
                override_base_url=override_base_url,
                override_model=override_model,
                on_result=persist_result,
                translation_context=translation_context,
                domain=domain,
            )
        except TranslationChunkError as exc:
            slot = batch[exc.index]
            remaining = [
                index for index in batch[exc.index + 1 :] if not translations[index]
            ]
            if remaining:
                # The failure is local to one segment; the rest of the batch is
                # still worth translating one by one.
                _do_batch(_i, remaining)
        return ""

    def _fallback(_i: int, batch: list[int], _exc: Exception) -> str:
        return ""

    _run_concurrent(batches, worker=_do_batch, fallback=_fallback)

    logical_translations: list[str] = []
    issues: list[TranslationIssue] = []
    for group_index, (indices, mapping) in enumerate(segment_groups):
        parts = [translations[idx].strip() for idx in indices]
        if any(not part for part in parts):
            # The logical block is atomic: a failed piece restores the whole
            # block's source wording rather than dropping that piece and
            # publishing the rest.
            failed = [idx for idx in indices if not translations[idx]]
            source = source_segments[group_index]
            logical_translations.append(source)
            issues.append(
                TranslationIssue(
                    logical_index=group_index,
                    source=source,
                    reason=(
                        f"{len(failed)} of {len(indices)} piece(s) could not be "
                        "translated after repeated retries"
                    ),
                )
            )
            continue
        logical_translations.append(
            restore_placeholders(" ".join(parts), mapping)
        )

    apply_translations(ir, logical_translations)

    notes: list[str] = []
    if issues:
        sample = ", ".join(issue.source[:40] for issue in issues[:3])
        notes.append(
            f"{len(issues)} block(s) kept their source text after a piece "
            f"failed to translate (e.g. {sample})"
        )
    return notes, [issue.as_dict() for issue in issues]
