"""Per-domain terminology glossary: candidate pooling and consolidation."""

import json
from datetime import datetime, timedelta, timezone

from app.core.config import settings
from app.services import glossary_service


def _glossary(domain="cs"):
    return json.loads(glossary_service.glossary_path(domain).read_text(encoding="utf-8"))


def _pending(domain="cs"):
    return json.loads(glossary_service.pending_path(domain).read_text(encoding="utf-8"))


def test_candidate_terms_accumulate_across_documents(isolated_storage):
    glossary_service.record_candidate_terms("cs", [("attention", "注意力")], "doc-1")
    glossary_service.record_candidate_terms(
        "cs", [("attention", "注意力"), ("embedding", "嵌入")], "doc-2"
    )

    terms = {term["en"]: term for term in _pending("cs")["terms"]}
    assert terms["attention"]["count"] == 2
    assert terms["attention"]["documents"] == ["doc-1", "doc-2"]
    assert terms["embedding"]["count"] == 1


def test_candidate_terms_drop_unusable_pairs(isolated_storage):
    glossary_service.record_candidate_terms(
        "cs",
        [("", "空"), ("attention", ""), ("attention", "attention"), ("x" * 81, "长")],
        "doc-1",
    )
    assert not glossary_service.pending_path("cs").is_file() or _pending("cs")["terms"] == []


def test_candidate_recording_never_raises_on_storage_failure(isolated_storage, monkeypatch):
    def broken_write(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(glossary_service, "_write", broken_write)
    glossary_service.record_candidate_terms("cs", [("attention", "注意力")], "doc-1")


def test_consolidate_merges_pending_and_clears_the_pool(isolated_storage):
    glossary_service.record_candidate_terms("medical", [("myocardial infarction", "心肌梗死")], "d")
    snapshot = glossary_service.consolidate_glossary("medical")

    assert snapshot["term_count"] == 1
    assert snapshot["pending_count"] == 0
    assert snapshot["terms"][0]["en"] == "myocardial infarction"
    assert snapshot["updated_at"] is not None
    assert _pending("medical")["terms"] == []


def test_consolidate_without_candidates_keeps_updated_at(isolated_storage):
    glossary_service.record_candidate_terms("cs", [("attention", "注意力")], "d")
    first = glossary_service.consolidate_glossary("cs")
    second = glossary_service.consolidate_glossary("cs")

    assert second["updated_at"] == first["updated_at"]
    assert second["term_count"] == 1


def test_consolidate_keeps_the_better_supported_rendering(isolated_storage):
    glossary_service.record_candidate_terms("cs", [("attention", "注意力")], "d1")
    glossary_service.record_candidate_terms("cs", [("attention", "注意力")], "d2")
    glossary_service.record_candidate_terms("cs", [("attention", "关注")], "d3")
    glossary_service.consolidate_glossary("cs")
    assert _glossary("cs")["terms"][0]["zh"] == "注意力"

    # A rendering seen more often in the pool than in the glossary replaces it.
    for index in range(5):
        glossary_service.record_candidate_terms("cs", [("attention", "关注")], f"d{index}")
    glossary_service.consolidate_glossary("cs")
    assert _glossary("cs")["terms"][0]["zh"] == "关注"


def test_glossary_is_pruned_to_the_highest_counts(isolated_storage, monkeypatch):
    monkeypatch.setattr(glossary_service, "MAX_GLOSSARY_TERMS", 3)
    glossary_service.record_candidate_terms(
        "cs",
        [("alpha", "甲"), ("beta", "乙"), ("gamma", "丙"), ("delta", "丁")],
        "d1",
    )
    glossary_service.record_candidate_terms("cs", [("delta", "丁")], "d2")
    glossary_service.record_candidate_terms("cs", [("delta", "丁")], "d3")
    snapshot = glossary_service.consolidate_glossary("cs")

    assert snapshot["term_count"] == 3
    assert snapshot["terms"][0]["en"] == "delta"


def test_prompt_terms_are_ordered_by_evidence_and_capped(isolated_storage):
    glossary_service.record_candidate_terms(
        "cs", [("alpha", "甲"), ("beta", "乙")], "d1"
    )
    glossary_service.record_candidate_terms("cs", [("beta", "乙")], "d2")
    glossary_service.consolidate_glossary("cs")

    assert glossary_service.glossary_terms_for_prompt("cs") == [("beta", "乙"), ("alpha", "甲")]
    assert glossary_service.glossary_terms_for_prompt("cs", limit=1) == [("beta", "乙")]


def test_refresh_if_due_respects_the_interval(isolated_storage, monkeypatch):
    monkeypatch.setattr(settings, "glossary_refresh_interval_minutes", 30)
    glossary_service.record_candidate_terms("cs", [("attention", "注意力")], "d1")
    glossary_service.consolidate_glossary("cs")
    glossary_service.record_candidate_terms("cs", [("embedding", "嵌入")], "d2")

    glossary_service.refresh_if_due("cs")
    assert glossary_service.glossary_snapshot("cs")["pending_count"] == 1

    path = glossary_service.glossary_path("cs")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["updated_at"] = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    path.write_text(json.dumps(payload), encoding="utf-8")

    glossary_service.refresh_if_due("cs")
    assert glossary_service.glossary_snapshot("cs")["pending_count"] == 0


def test_snapshot_consolidates_the_first_pending_pool(isolated_storage):
    glossary_service.record_candidate_terms("cs", [("attention", "注意力")], "d")

    snapshot = glossary_service.glossary_snapshot("cs")

    assert snapshot["domain"] == "cs"
    assert snapshot["label"] == "计算机科学"
    assert snapshot["term_count"] == 1
    assert snapshot["pending_count"] == 0
    assert snapshot["interval_minutes"] == settings.glossary_refresh_interval_minutes


def test_delete_glossary_term_persists(isolated_storage):
    glossary_service.record_candidate_terms(
        "cs", [("attention", "注意力"), ("embedding", "嵌入")], "d"
    )
    glossary_service.consolidate_glossary("cs")

    snapshot = glossary_service.delete_glossary_term("cs", "Attention")

    assert [term["en"] for term in snapshot["terms"]] == ["embedding"]
    assert [term["en"] for term in _glossary("cs")["terms"]] == ["embedding"]


def test_unknown_domain_falls_back_to_general(isolated_storage):
    glossary_service.record_candidate_terms("astrology", [("attention", "注意力")], "d")
    assert glossary_service.pending_path("astrology") == glossary_service.pending_path("general")
    glossary_service.consolidate_glossary("astrology")
    assert glossary_service.glossary_snapshot("astrology")["domain"] == "general"


def test_corrupt_glossary_file_is_ignored(isolated_storage):
    path = glossary_service.glossary_path("cs")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    snapshot = glossary_service.glossary_snapshot("cs")

    assert snapshot["term_count"] == 0
    assert snapshot["updated_at"] is None
