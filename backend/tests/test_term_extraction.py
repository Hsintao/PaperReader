"""Background term learning: batching, parsing and the pending-pool handoff."""

import json
from types import SimpleNamespace

import pytest

from app.services import glossary_service, term_extraction


def _manifest(*texts):
    return SimpleNamespace(
        blocks=[SimpleNamespace(source_text=text) for text in texts]
    )


def _pending_terms(domain="cs"):
    path = glossary_service.pending_path(domain)
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {term["en"]: term for term in payload["terms"]}


def test_source_texts_skip_blocks_without_terminology():
    manifest = _manifest(
        "Attention mechanisms improve translation quality.",
        "E = mc^2",
        "3.14159",
        "   ",
        None,
    )

    assert term_extraction.source_texts(manifest) == [
        "Attention mechanisms improve translation quality."
    ]


def test_batches_split_on_the_count_limit():
    batches = list(term_extraction._batches(["term text"] * 25))
    assert [len(batch) for batch in batches] == [12, 12, 1]


def test_batches_split_on_the_token_budget():
    batches = list(term_extraction._batches(["x" * 2500] * 3))
    assert [len(batch) for batch in batches] == [1, 1, 1]


def test_parse_terms_tolerates_fences_and_prose():
    raw = (
        "Here are the terms:\n```json\n"
        '[{"src": "attention", "tgt": "注意力"}]\n```\nDone.'
    )
    assert term_extraction._parse_terms(raw) == [("attention", "注意力")]


def test_parse_terms_rejects_junk():
    assert term_extraction._parse_terms("no json here") == []
    assert term_extraction._parse_terms('{"src": "a", "tgt": "b"}') == []
    messy = json.dumps(
        [
            {"src": "", "tgt": "空"},
            {"src": "x" * 120, "tgt": "太长"},
            "not a dict",
            {"src": "attention", "tgt": "注意力"},
        ]
    )
    assert term_extraction._parse_terms(messy) == [("attention", "注意力")]


def test_extract_terms_batches_calls_and_flattens(monkeypatch):
    calls = []

    def fake_complete(prompt, *, api_key, base_url, model):
        calls.append(prompt)
        return json.dumps([{"src": f"term-{len(calls)}", "tgt": "译"}])

    monkeypatch.setattr(term_extraction, "_llm_complete", fake_complete)

    pairs = term_extraction.extract_terms(
        ["text"] * 25, domain="cs", api_key="k", base_url="https://llm.example/v1", model="m"
    )

    assert len(calls) == 3
    assert pairs == [("term-1", "译"), ("term-2", "译"), ("term-3", "译")]


def test_extract_terms_continues_after_a_failed_batch(monkeypatch):
    calls = []

    def flaky_complete(prompt, *, api_key, base_url, model):
        calls.append(prompt)
        if len(calls) == 1:
            raise OSError("connection reset")
        return '[{"src": "attention", "tgt": "注意力"}]'

    monkeypatch.setattr(term_extraction, "_llm_complete", flaky_complete)

    pairs = term_extraction.extract_terms(
        ["text"] * 13, domain="cs", api_key="k", base_url="https://llm.example/v1", model="m"
    )

    assert len(calls) == 2
    assert pairs == [("attention", "注意力")]


def test_extract_terms_skips_without_credentials(monkeypatch):
    monkeypatch.setattr(
        term_extraction,
        "_llm_complete",
        lambda *args, **kwargs: pytest.fail("no call without credentials"),
    )

    assert (
        term_extraction.extract_terms(
            ["text"], domain="cs", api_key="", base_url="https://llm.example", model="m"
        )
        == []
    )


def test_the_prompt_carries_only_glossary_terms_present_in_the_text(isolated_storage):
    glossary_service._write(
        glossary_service.glossary_path("cs"),
        [
            {"en": "attention", "zh": "注意力", "count": 3},
            {"en": "residual learning", "zh": "残差学习", "count": 1},
        ],
    )

    section = term_extraction._reference_glossary_section(
        "We study attention in sequence models.", "cs"
    )

    assert "attention → 注意力" in section
    assert "residual learning" not in section
    assert term_extraction._reference_glossary_section("Nothing relevant.", "cs") == ""


def test_scheduled_extraction_records_deduped_terms_into_the_pending_pool(
    isolated_storage, monkeypatch
):
    monkeypatch.setattr(
        term_extraction,
        "_llm_complete",
        lambda prompt, **kwargs: json.dumps(
            [
                {"src": "Attention", "tgt": "注意力"},
                {"src": "attention", "tgt": "注意机制"},
            ]
        ),
    )
    manifest = _manifest("Attention is all you need in this paper.")

    thread = term_extraction.schedule_extraction(
        document_id="doc-9",
        manifest=manifest,
        domain="cs",
        api_key="k",
        base_url="https://llm.example/v1",
        model="m",
    )
    thread.join(timeout=10)

    terms = _pending_terms("cs")
    assert terms["Attention"]["zh"] == "注意力"
    assert terms["Attention"]["count"] == 1
    assert terms["Attention"]["documents"] == ["doc-9"]


def test_scheduling_is_skipped_without_texts_or_credentials(isolated_storage):
    assert (
        term_extraction.schedule_extraction(
            document_id="d",
            manifest=_manifest(),
            domain="cs",
            api_key="k",
            base_url="https://llm.example",
            model="m",
        )
        is None
    )
    assert (
        term_extraction.schedule_extraction(
            document_id="d",
            manifest=_manifest("A real sentence worth learning from."),
            domain="cs",
            api_key="",
            base_url="https://llm.example",
            model="m",
        )
        is None
    )
