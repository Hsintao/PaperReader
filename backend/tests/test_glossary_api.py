"""Glossary endpoints: snapshot, forced refresh and manual term removal."""

from fastapi.testclient import TestClient

from app.main import app
from app.services import glossary_service


def test_glossary_snapshot_starts_empty(isolated_storage):
    with TestClient(app) as client:
        response = client.get("/api/glossary/cs")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["domain"] == "cs"
    assert payload["label"] == "计算机科学"
    assert payload["terms"] == []
    assert payload["updated_at"] is None


def test_refresh_endpoint_merges_pending_candidates(isolated_storage):
    glossary_service.record_candidate_terms(
        "medical", [("myocardial infarction", "心肌梗死")], "doc-1"
    )

    with TestClient(app) as client:
        response = client.post("/api/glossary/medical/refresh")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert [term["en"] for term in payload["terms"]] == ["myocardial infarction"]
    assert payload["pending_count"] == 0


def test_delete_endpoint_removes_one_term(isolated_storage):
    glossary_service.record_candidate_terms(
        "cs", [("attention", "注意力"), ("embedding", "嵌入")], "doc-1"
    )
    glossary_service.consolidate_glossary("cs")

    with TestClient(app) as client:
        response = client.request(
            "DELETE", "/api/glossary/cs/terms", json={"en": "attention"}
        )

    assert response.status_code == 200, response.text
    assert [term["en"] for term in response.json()["terms"]] == ["embedding"]


def test_unknown_domain_is_rejected(isolated_storage):
    with TestClient(app) as client:
        assert client.get("/api/glossary/astrology").status_code == 404
        assert client.post("/api/glossary/astrology/refresh").status_code == 404
        assert (
            client.request(
                "DELETE", "/api/glossary/astrology/terms", json={"en": "attention"}
            ).status_code
            == 404
        )
