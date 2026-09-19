"""Best-effort paper metadata lookup for the library list and BibTeX export."""

from __future__ import annotations

import logging

import requests


logger = logging.getLogger(__name__)


def fetch_paper_metadata(title: str) -> dict:
    """Look up title/authors/year/venue for a paper on Semantic Scholar.

    Enrichment only: any failure returns an empty dict and the caller keeps
    whatever metadata it already had.
    """
    query = " ".join((title or "").split())[:240]
    if len(query) < 4:
        return {}
    try:
        response = requests.get(
            "https://api.semanticscholar.org/graph/v1/paper/search",
            params={
                "query": query,
                "limit": 1,
                "fields": "title,year,authors,venue,externalIds",
            },
            timeout=8,
            headers={"User-Agent": "PaperReader/2.1 metadata"},
        )
        response.raise_for_status()
        papers = response.json().get("data") or []
    except Exception as exc:
        logger.info("Paper metadata lookup unavailable: %s", exc)
        return {}
    if not papers:
        return {}
    paper = papers[0]
    metadata = {
        "title": str(paper.get("title") or "").strip(),
        "year": str(paper.get("year") or ""),
        "venue": str(paper.get("venue") or "").strip(),
        "authors": ", ".join(
            str(author.get("name") or "") for author in (paper.get("authors") or [])[:10]
        ).strip(", "),
    }
    external = paper.get("externalIds") or {}
    if external.get("DOI"):
        metadata["doi"] = str(external["DOI"])
    if external.get("ArXiv"):
        metadata["eprint"] = str(external["ArXiv"])
    return {key: value for key, value in metadata.items() if value}
