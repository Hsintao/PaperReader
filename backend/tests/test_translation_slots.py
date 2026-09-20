"""Translation slots: captions, front matter and whole-block fallback."""

import pytest

from app.services import translate_service
from app.services.mineru_layout import (
    Author,
    Image,
    ListBlock,
    Paragraph,
    Table,
    TableCell,
    TextRun,
    Title,
    apply_translations,
    blocks_to_ir,
    collect_translatable_strings,
    translatable_mask,
)


def _front_matter_pages() -> list:
    return [[
        {
            "type": "title",
            "content": {"title_content": [{"type": "text", "content": "Paper Title"}], "level": 1},
        },
        {
            "type": "paragraph",
            "content": {"paragraph_content": [{"type": "text", "content": "Jane Doe, John Smith"}]},
        },
        {
            "type": "paragraph",
            "content": {
                "paragraph_content": [{
                    "type": "text",
                    "content": "Department of Computer Science, Example University, jane@example.edu",
                }]
            },
        },
        {
            "type": "title",
            "content": {"title_content": [{"type": "text", "content": "1 Introduction"}], "level": 1},
        },
        {
            "type": "paragraph",
            "content": {"paragraph_content": [{"type": "text", "content": "Body text."}]},
        },
    ]]


def test_figure_and_table_captions_enter_the_translation_queue():
    pages = [[
        {
            "type": "title",
            "content": {"title_content": [{"type": "text", "content": "Paper"}], "level": 1},
        },
        {
            "type": "image",
            "bbox": [100, 100, 400, 300],
            "content": {
                "image_source": {"path": "images/f1.jpg"},
                "image_caption": [{"type": "text", "content": "Figure 1. A caption."}],
            },
        },
        {
            "type": "table",
            "bbox": [100, 400, 400, 500],
            "content": {
                "image_source": {"path": "images/t1.jpg"},
                "table_caption": [{"type": "text", "content": "Table 2. Results."}],
                "table_footnote": [{"type": "text", "content": "Values are means."}],
            },
        },
    ]]
    ir = blocks_to_ir(pages)
    assert collect_translatable_strings(ir) == [
        "Paper",
        "Figure 1. A caption.",
        "Table 2. Results. Values are means.",
    ]
    assert translatable_mask(ir) == [True, True, True]

    apply_translations(ir, ["论文", "图 1。一个图注。", "表 2。结果。数值为均值。"])
    assert ir[1].caption == "Figure 1. A caption."
    assert ir[1].translated_caption == "图 1。一个图注。"
    assert ir[2].caption == "Table 2. Results. Values are means."
    assert ir[2].translated_caption == "表 2。结果。数值为均值。"


def test_author_names_stay_and_affiliations_translate():
    ir = blocks_to_ir(_front_matter_pages())
    roles = [block.role for block in ir]
    assert roles[1] == "author"
    assert roles[2] == "affiliation"
    segments = collect_translatable_strings(ir)
    # The byline is an author node and never enters the queue; the affiliation
    # stays a translatable paragraph.
    assert "Jane Doe, John Smith" not in segments
    assert "Department of Computer Science, Example University, jane@example.edu" in segments
    assert translatable_mask(ir) == [True, True, True, True]


def test_references_heading_translates_and_entries_stay_english():
    pages = [[
        {
            "type": "title",
            "content": {"title_content": [{"type": "text", "content": "Paper"}], "level": 1},
        },
        {
            "type": "title",
            "content": {"title_content": [{"type": "text", "content": "References"}], "level": 1},
        },
        {
            "type": "list",
            "content": {
                "list_type": "reference_list",
                "list_items": [{
                    "item_type": "text",
                    "item_content": [{"type": "text", "content": "[1] Smith, J. A paper. 2024."}],
                }],
            },
        },
    ]]
    ir = blocks_to_ir(pages)
    assert collect_translatable_strings(ir) == [
        "Paper",
        "References",
        "[1] Smith, J. A paper. 2024.",
    ]
    assert translatable_mask(ir) == [True, True, False]


def _paragraph_ir(text: str) -> list:
    return [Paragraph(runs=[TextRun(text=text)], source_text=text)]


def test_one_failed_piece_restores_the_whole_logical_segment(monkeypatch):
    """A paragraph split into several pieces must never publish a partial one."""
    calls: list[str] = []

    def fake_chat(message, system_prompt, **kwargs):
        calls.append(message)
        if "@@SEG@@" in message:
            parts = message.split("@@SEG@@")
            # The second piece comes back empty, which the validator rejects.
            return "@@SEG@@".join(
                "译" + part.strip() if index != 1 else "" for index, part in enumerate(parts)
            )
        raise RuntimeError("single-segment retry also fails")

    monkeypatch.setattr(translate_service.llm_client, "chat", fake_chat)
    monkeypatch.setattr(translate_service.settings, "translate_segment_max_chars", 300)
    monkeypatch.setattr(translate_service, "_MAX_TRANSLATION_ATTEMPTS", 1)

    source = "START " + " ".join(f"word{index}" for index in range(160)) + " END"
    ir = _paragraph_ir(source)
    notes, issues = translate_service.translate_ir(ir)

    # The whole logical block keeps its source wording instead of a half
    # translation with the failed piece missing.
    assert ir[0].runs[0].text == source
    assert issues, "the failed block must be reported"
    assert issues[0]["kind"] == "block_original"
    assert issues[0]["logical_index"] == 0
    assert issues[0]["source"] == source
    assert "kept their source text" in " ".join(notes)


def test_successful_translation_is_unaffected(monkeypatch):
    def fake_chat(message, system_prompt, **kwargs):
        if "@@SEG@@" in message:
            return "@@SEG@@".join(f"译({part.strip()})" for part in message.split("@@SEG@@"))
        return f"译({message.strip()})"

    monkeypatch.setattr(translate_service.llm_client, "chat", fake_chat)
    ir = _paragraph_ir("Hello world, this is a paragraph.")
    notes, issues = translate_service.translate_ir(ir)
    assert issues == []
    assert ir[0].runs[0].text.startswith("译(")


def test_checkpoint_reuses_a_complete_translation_and_never_a_partial_one(monkeypatch, tmp_path):
    from app.services import translate_service as service

    checkpoint = tmp_path / "translation-checkpoint.json"
    calls: list[str] = []

    def fake_chat(message, system_prompt, **kwargs):
        calls.append(message)
        return f"译({message.strip()})"

    monkeypatch.setattr(service.llm_client, "chat", fake_chat)
    first = _paragraph_ir("A short paragraph to translate.")
    service.translate_ir(first, checkpoint_path=checkpoint)
    assert len(calls) == 1

    second = _paragraph_ir("A short paragraph to translate.")
    service.translate_ir(second, checkpoint_path=checkpoint)
    assert len(calls) == 1, "the cached translation must be reused"
    assert second[0].runs[0].text == first[0].runs[0].text


def test_table_cell_slots_still_translate_and_caption_stays_source():
    table = Table(
        rel_path="t.jpg",
        caption="Table 1. Results.",
        cells=[TableCell(text="Method", bbox=(10, 10, 60, 20))],
    )
    ir = [Title(level=1, text="Paper"), table]
    assert collect_translatable_strings(ir) == ["Paper", "Table 1. Results.", "Method"]


def test_unwritable_checkpoint_does_not_fail_the_translation(monkeypatch, tmp_path):
    """A read-only output directory must not turn a translation into a fallback."""
    from app.services import translate_service as service

    def fake_chat(message, system_prompt, **kwargs):
        if "@@SEG@@" in message:
            return "@@SEG@@".join(f"译({part.strip()})" for part in message.split("@@SEG@@"))
        return f"译({message.strip()})"

    monkeypatch.setattr(service.llm_client, "chat", fake_chat)
    # A path whose parent is a file can never be written.
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory")
    checkpoint = blocker / "translation-checkpoint.json"

    ir = _paragraph_ir("A short paragraph to translate.")
    notes, issues = service.translate_ir(ir, checkpoint_path=checkpoint)

    assert issues == []
    assert ir[0].runs[0].text.startswith("译(")
    assert notes == []
