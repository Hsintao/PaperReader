"""Job specification accepted by the worker.

The backend writes one JSON document per job and passes its path on the command
line. Everything the worker needs comes from that file: no configuration is read
from the environment, so a job is reproducible from the file alone.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


class JobError(ValueError):
    """The job file is missing, unreadable, or incomplete."""


@dataclass(frozen=True)
class Job:
    job_id: str
    input_pdf: Path
    output_dir: Path
    work_dir: Path
    api_key: str
    base_url: str
    model: str
    source_lang: str = "en"
    target_lang: str = "zh"
    output_mode: str = "mono"
    no_watermark: bool = True
    keep_debug: bool = False
    qps: int = 4
    glossary_path: Path | None = None

    @property
    def extraction_dir(self) -> Path:
        """Where the manifest and the parse output are published."""
        from workers.pdfmathtranslate import EXTRACTION_DIR_NAME

        return self.output_dir / EXTRACTION_DIR_NAME

    @property
    def translated_pdf(self) -> Path:
        return self.output_dir / "translated.pdf"

    @property
    def dual_pdf(self) -> Path:
        """The bilingual PDF, published beside the monolingual one."""
        return self.output_dir / "translated.dual.pdf"

    @property
    def manifest_path(self) -> Path:
        from workers.pdfmathtranslate import MANIFEST_FILENAME

        return self.extraction_dir / MANIFEST_FILENAME

    @property
    def babeldoc_work_dir(self) -> Path:
        return self.work_dir / "babeldoc"

    def redacted(self) -> dict:
        """The job as it may appear in a log: the API key never does."""
        payload = {
            "job_id": self.job_id,
            "input_pdf": str(self.input_pdf),
            "output_dir": str(self.output_dir),
            "work_dir": str(self.work_dir),
            "translation": {
                "api_key": "***" if self.api_key else "",
                "base_url": self.base_url,
                "model": self.model,
            },
            "options": {
                "output": self.output_mode,
                "no_watermark": self.no_watermark,
                "debug": self.keep_debug,
            },
        }
        return payload


def _require(payload: dict, key: str) -> object:
    if key not in payload or payload[key] in (None, ""):
        raise JobError(f"job is missing required field '{key}'")
    return payload[key]


def load_job(path: str | Path) -> Job:
    source = Path(path).resolve()
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise JobError(f"job file not found: {source}") from exc
    except (OSError, ValueError) as exc:
        raise JobError(f"job file is not valid JSON: {source}: {exc}") from exc
    if not isinstance(payload, dict):
        raise JobError("job file must hold a JSON object")
    return job_from_mapping(payload, base_dir=source.parent)


def job_from_mapping(payload: dict, *, base_dir: Path | None = None) -> Job:
    translation = payload.get("translation")
    if not isinstance(translation, dict):
        raise JobError("job is missing the 'translation' object")
    options = payload.get("options")
    if options is None:
        options = {}
    if not isinstance(options, dict):
        raise JobError("'options' must be a JSON object")

    def resolve_path(value: object) -> Path:
        path = Path(str(value))
        if base_dir is not None and not path.is_absolute():
            path = base_dir / path
        return path.resolve()

    output_dir = resolve_path(_require(payload, "output_dir"))
    work_dir_value = payload.get("work_dir")
    work_dir = (
        resolve_path(work_dir_value) if work_dir_value else output_dir / "work"
    )
    glossary = payload.get("glossary")
    output_mode = str(options.get("output") or "mono").lower()
    if output_mode not in {"mono", "dual", "both"}:
        raise JobError(f"unsupported output mode: {output_mode!r}")

    return Job(
        job_id=str(_require(payload, "job_id")),
        input_pdf=resolve_path(_require(payload, "input_pdf")),
        output_dir=output_dir,
        work_dir=work_dir,
        api_key=str(translation.get("api_key") or ""),
        base_url=str(translation.get("base_url") or ""),
        model=str(translation.get("model") or ""),
        source_lang=str(payload.get("source_lang") or "en"),
        target_lang=str(payload.get("target_lang") or "zh"),
        output_mode=output_mode,
        no_watermark=bool(options.get("no_watermark", True)),
        keep_debug=bool(options.get("debug", False)),
        qps=max(1, _int(payload.get("qps"), 4)),
        glossary_path=resolve_path(glossary) if glossary else None,
    )


def _int(value: object, default: int) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
