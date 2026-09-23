"""The neutral result of translating one PDF.

Whoever produced the translation — today the PDFMathTranslate-next worker — the
pipeline hands its downstream stages this object and nothing else. It names the
translated PDF, the stable manifest that describes it, and the optional glossary
the translator extracted.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - import cycle only matters for typing
    from app.services.document_manifest import DocumentManifest


@dataclass
class PdfTranslationResult:
    source_pdf: Path
    translated_pdf: Path
    manifest_path: Path
    extraction_dir: Path
    mode_label: str
    page_count: int = 0
    glossary_path: Path | None = None
    dual_pdf: Path | None = None

    def manifest(self) -> "DocumentManifest":
        """Parse the manifest this result points at."""
        from app.services.document_manifest import load_document_manifest

        return load_document_manifest(self.manifest_path)

    @classmethod
    def from_worker(cls, source_pdf: Path, products) -> "PdfTranslationResult":
        """Build from the products of :mod:`app.services.pdf_translation_worker`."""
        return cls(
            source_pdf=source_pdf,
            translated_pdf=products.translated_pdf,
            manifest_path=products.manifest_path,
            extraction_dir=products.extraction_dir,
            mode_label=products.mode_label,
            page_count=products.page_count,
            glossary_path=products.glossary_path,
            dual_pdf=products.dual_pdf,
        )

    def is_usable(self) -> bool:
        return self.translated_pdf.is_file() and self.manifest_path.is_file()
