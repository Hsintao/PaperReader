"""Entry point: ``python -m workers.pdfmathtranslate <job.json>``."""

from __future__ import annotations

from workers.pdfmathtranslate.runner import main

if __name__ == "__main__":
    raise SystemExit(main())
