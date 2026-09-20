"""Per-domain terminology glossary endpoints.

The glossary is local data the operator owns: list a domain's snapshot, force
a consolidation of the pending pool, or drop a term that reads wrong.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from app.services.glossary_service import (
    consolidate_glossary,
    delete_glossary_term,
    glossary_snapshot,
)
from app.services.translation_prompts import require_domain


router = APIRouter()


class DeleteGlossaryTermRequest(BaseModel):
    en: str


@router.get("/glossary/{domain}")
def get_glossary(domain: str) -> dict:
    require_domain(domain)
    return glossary_snapshot(domain)


@router.post("/glossary/{domain}/refresh")
def refresh_glossary(domain: str) -> dict:
    require_domain(domain)
    return consolidate_glossary(domain)


@router.delete("/glossary/{domain}/terms")
def remove_glossary_term(domain: str, payload: DeleteGlossaryTermRequest) -> dict:
    require_domain(domain)
    return delete_glossary_term(domain, payload.en)
