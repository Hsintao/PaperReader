"""PDFMathTranslate-next worker: translates a PDF and reports a stable manifest.

The worker is a standalone process. PaperReader's backend starts it, streams its
newline-delimited JSON events from stdout, and reads the manifest it writes. The
PDFMathTranslate-next / BabelDOC dependency lives only here, never in the
backend's import graph.
"""

from __future__ import annotations

# Pinned because the manifest converter reads BabelDOC's debug layout, which is
# version specific.
PDFMATHTRANSLATE_VERSION = "2.9.0"
BABELDOC_VERSION = "0.6.2"

MANIFEST_SCHEMA_VERSION = "paperreader-manifest-v1"
MANIFEST_FILENAME = "manifest.json"
EXTRACTION_DIR_NAME = "extraction"

__all__ = [
    "PDFMATHTRANSLATE_VERSION",
    "BABELDOC_VERSION",
    "MANIFEST_SCHEMA_VERSION",
    "MANIFEST_FILENAME",
    "EXTRACTION_DIR_NAME",
]
