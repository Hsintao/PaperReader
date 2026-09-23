"""Newline-delimited JSON events the worker writes on stdout.

Every event is one line of JSON with a ``type`` field. PaperReader's backend
reads these lines and maps them onto document stages; anything the worker
cannot express as an event goes to stderr as plain log text, so stdout stays
parseable.
"""

from __future__ import annotations

import json
import sys
from typing import Any, TextIO

# BabelDOC's progress stages, grouped into the units PaperReader shows.
_STAGE_GROUPS: dict[str, str] = {
    "Parse PDF and Create Intermediate Representation": "parse",
    "DetectScannedFile": "parse",
    "Parse Page Layout": "parse",
    "Parse Table": "parse",
    "Parse Paragraphs": "parse",
    "Parse Formulas and Styles": "parse",
    "Remove Char Descent": "parse",
    "Automatic Term Extraction": "translate",
    "Translate Paragraphs": "translate",
    "Typesetting": "render",
    "Add Fonts": "render",
    "Generate drawing instructions": "render",
    "Subset font": "render",
    "Save PDF": "render",
}


def stage_group(stage: str) -> str:
    """Map a BabelDOC stage name onto a PaperReader stage."""
    return _STAGE_GROUPS.get(stage, "render")


class EventWriter:
    """Serialise worker events as one JSON object per line."""

    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream if stream is not None else sys.stdout
        self._stages: list[dict[str, Any]] = []

    def emit(self, event_type: str, **payload: Any) -> None:
        event = {"type": event_type, **payload}
        self._stream.write(json.dumps(event, ensure_ascii=False) + "\n")
        self._stream.flush()

    def stage_summary(self, stages: list[dict[str, Any]]) -> None:
        self._stages = [
            {
                "name": str(item.get("name") or ""),
                "stage": stage_group(str(item.get("name") or "")),
                "weight": float(item.get("percent") or 0.0),
            }
            for item in stages
        ]
        self.emit("stage_summary", stages=self._stages)

    def progress(self, event_type: str, stage: str, payload: dict[str, Any]) -> None:
        self.emit(
            event_type,
            stage=stage,
            group=stage_group(stage),
            progress=_fraction(payload.get("stage_progress")),
            overall=_fraction(payload.get("overall_progress")),
            current=_int(payload.get("stage_current")),
            total=_int(payload.get("stage_total")),
        )

    def finish(self, result: dict[str, Any]) -> None:
        self.emit("finish", status="finished", **result)

    def error(self, message: str, *, stage: str = "") -> None:
        self.emit("error", status="failed", message=message, stage=stage)


def _fraction(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value) / 100.0))
    except (TypeError, ValueError):
        return 0.0


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
