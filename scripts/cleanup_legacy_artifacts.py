#!/usr/bin/env python3
"""One-off cleanup of the artifacts the retired PDF parsers left behind.

Documents translated by the SoMark / MinerU pipeline cannot be reused: their
translated PDF was laid out by the in-house renderer and their parse caches are
in a format the worker never reads. This script removes those leftovers and
resets the affected document records so the reader offers a reprocess.

Run it once after upgrading:

    python scripts/cleanup_legacy_artifacts.py            # report only
    python scripts/cleanup_legacy_artifacts.py --apply     # make the changes

The source PDF of every document is kept, in `data/uploads` and as the
document's `original.pdf`, so nothing has to be re-uploaded.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.config import settings  # noqa: E402
from app.models import store  # noqa: E402
from app.models.store import FailureEntry  # noqa: E402


# Directories and files produced by the retired pipeline only.
LEGACY_ENTRIES = (
    "somark",
    "mineru",
    "local",
    "extraction-checkpoint.json",
    "translation-checkpoint.json",
    "layout-plan.json",
    "layout-debug.pdf",
    "rendered.pdf",
    "alignment.json",
    "formula-crops",
    "figure-previews",
    "translation-layout-v2-check",
)

# Artifact kinds whose files the retired pipeline produced.
LEGACY_ARTIFACT_KINDS = {"translated_pdf", "annotated_pdf", "manifest", "glossary", "alignment_index"}


def _remove(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def _legacy_document_dirs(output_dir: Path) -> tuple[list[Path], list[Path]]:
    """Split output directories into the ones the worker produced and the rest."""
    current: list[Path] = []
    stale: list[Path] = []
    if not output_dir.is_dir():
        return current, stale
    for entry in sorted(output_dir.iterdir()):
        if not entry.is_dir():
            continue
        (current if (entry / "extraction" / "manifest.json").is_file() else stale).append(entry)
    return current, stale


def _stale_files(document_dir: Path) -> list[Path]:
    """The leftovers in one retired document directory.

    The translated PDFs the old renderer wrote are found by their name pattern,
    which also covers a directory whose document is no longer in the database.
    """
    targets = [document_dir / name for name in LEGACY_ENTRIES]
    for suffix in ("_Chinese_ver.pdf", "_原文标注.pdf"):
        targets.extend(document_dir.glob(f"*{suffix}"))
    return [path for path in targets if path.exists()]


def _reset_record(record, removed: list[str]) -> None:
    """Point a document back at its source PDF and ask for a reprocess."""
    for field in ("translated_pdf_url", "annotated_pdf_url"):
        if getattr(record, field, None):
            setattr(record, field, None)
            removed.append(f"{record.document_id}: cleared {field}")
    kept = [item for item in record.artifacts if item.kind not in LEGACY_ARTIFACT_KINDS]
    dropped = len(record.artifacts) - len(kept)
    if dropped:
        record.artifacts = kept
        removed.append(f"{record.document_id}: dropped {dropped} artifact(s)")
    record.extracted_text = ""
    record.translated_text = ""
    record.references = []
    record.metadata.pop("layout_issues", None)
    record.status = "failed"
    record.failure = FailureEntry(
        stage="parse",
        message="此文档由旧的解析流程生成，解析缓存已清理，请重新处理。",
        retryable=record.source_path.is_file(),
        retry_count=record.retry_count,
    )
    record.logs.append("Legacy artifacts removed; reprocess to translate with the new worker")
    store.save_document(record)
    removed.append(f"{record.document_id}: reset to failed (reprocess available)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--apply",
        action="store_true",
        help="make the changes; without it the script only reports what it would do",
    )
    args = parser.parse_args(argv)

    output_dir = settings.output_dir
    removed: list[str] = []
    stale_files: list[Path] = []

    current, stale = _legacy_document_dirs(output_dir)
    for document_dir in stale:
        stale_files.extend(_stale_files(document_dir))

    print(f"Data directory: {settings.data_dir}")
    print(f"Documents with a current manifest: {len(current)}")
    print(f"Documents from the retired pipeline: {len(stale)}")
    for path in stale_files:
        print(f"  remove {path.relative_to(settings.data_dir)}")
    if not args.apply:
        for document_dir in stale:
            record = store.get_document(document_dir.name)
            if record is not None:
                print(f"  reset  document {record.document_id} ({record.source_filename})")
        print("\nNothing changed. Re-run with --apply to perform the cleanup.")
        return 0

    for path in stale_files:
        _remove(path)
        removed.append(f"removed {path.relative_to(settings.data_dir)}")

    for document_dir in stale:
        if not any(document_dir.iterdir()):
            document_dir.rmdir()
            removed.append(f"removed {document_dir.relative_to(settings.data_dir)} (empty)")
        record = store.get_document(document_dir.name)
        if record is None:
            continue
        _reset_record(record, removed)

    print(f"\n{len(removed)} change(s):")
    for line in removed:
        print(f"  {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
