"""Run one PDF through the full Translation Layout V2 pipeline.

Parses the PDF with the configured backend (MinerU), translates it, lays the
translation out on the source pages and writes the translated PDF, the annotated
source PDF and the versioned layout plan. This is the end-to-end path the app
itself runs, used for real-paper acceptance.

Usage:
    conda run -n pt python scripts/translation_layout_v2_paper.py PAPER.pdf --name NAME
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.core.config import settings  # noqa: E402
from app.services import app_settings, document_pipeline  # noqa: E402
from app.services.mineru_service import MinerUResult  # noqa: E402


def _provider():
    return app_settings.load_settings()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("pdf")
    parser.add_argument("--name", default=None)
    parser.add_argument("--out", default="data/translation-layout-v2-check")
    args = parser.parse_args()

    source = Path(args.pdf).resolve()
    if not source.is_file():
        print(f"missing input: {source}")
        return 2
    name = args.name or source.stem
    provider = _provider()
    if not provider.api_key:
        print("no translation provider configured; set it in the app settings first")
        return 2

    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    target = settings.upload_dir / f"{name}.pdf"
    shutil.copyfile(source, target)

    record = document_pipeline.create_document_record(target, "pdf")
    record.source_filename = f"{name}.pdf"
    result = document_pipeline.process_document(
        record, provider_settings=provider, resume_from=None
    )

    out_dir = Path(args.out).resolve() / name
    out_dir.mkdir(parents=True, exist_ok=True)
    document_dir = settings.output_dir / result.document_id
    written: dict[str, str] = {}
    for artifact in result.artifacts:
        path = Path(artifact.path)
        if path.is_file():
            destination = out_dir / path.name
            if path.resolve() != destination.resolve():
                shutil.copyfile(path, destination)
            written[artifact.kind] = str(destination)
    summary = {
        "name": name,
        "document_id": result.document_id,
        "status": result.status,
        "failure": (result.failure.__dict__ if result.failure else None),
        "page_count": None,
        "layout_issues": result.metadata.get("layout_issues", []),
        "artifacts": written,
        "logs": result.logs[-30:],
    }
    plan_path = document_dir / "layout-plan.json"
    if plan_path.is_file():
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        summary["page_count"] = len(plan.get("pages", []))
        summary["page_statuses"] = [
            {"index": page["index"], "status": page["status"], "reason": page["reason"]}
            for page in plan.get("pages", [])
        ]
    (out_dir / "run.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if result.status == "done" else 1


if __name__ == "__main__":
    raise SystemExit(main())
