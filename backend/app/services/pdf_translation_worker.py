"""Process boundary around the PDFMathTranslate-next worker.

The backend never imports the translator. It writes a job file and hands its
path to a long-lived worker process (prewarmed with the backend and reused
across jobs, so the interpreter and model startup is off every job's path),
reading newline-delimited JSON events from its stdout. Everything the pipeline needs
afterwards — the translated PDF, the stable manifest — is a file the worker
published.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from app.core.config import settings
from app.services.translation_prompts import build_worker_system_prompt

EventSink = Callable[[dict], None]

_STAGES = {"parse", "translate", "render"}


class WorkerError(RuntimeError):
    """The worker could not produce a usable result."""

    def __init__(self, message: str, *, stage: str = "parse") -> None:
        super().__init__(message)
        self.stage = stage if stage in _STAGES else "parse"


class WorkerUnavailable(WorkerError):
    """No usable worker command is configured on this machine."""


class WorkerCancelled(WorkerError):
    """The operator cancelled the active worker run."""


@dataclass
class WorkerProducts:
    translated_pdf: Path
    manifest_path: Path
    extraction_dir: Path
    debug_dir: Path
    mode_label: str
    page_count: int
    glossary_path: Path | None = None
    dual_pdf: Path | None = None


def bundle_root() -> Path:
    """Where ``workers/`` lives: the repository root, or the desktop bundle."""
    override = os.environ.get("PAPERREADER_BUNDLE_ROOT")
    if override:
        return Path(override).resolve()
    # backend/app/services/pdf_translation_worker.py -> repository root
    return Path(__file__).resolve().parents[3]


def _unbracket_ipv6(value: str) -> str:
    """Rewrite bracketed IPv6 entries in a NO_PROXY list.

    httpx builds its proxy mounts from ``NO_PROXY`` and does not recognise the
    bracketed form: ``[::1]`` falls through to its wildcard branch and becomes
    ``all://*[::1]``, which fails URL parsing with
    ``InvalidURL: Invalid port: ':1]'``. Proxy managers do write that form, and
    to httpx ``::1`` means the same bypass.
    """
    entries: list[str] = []
    for entry in value.split(","):
        entry = entry.strip()
        if len(entry) > 2 and entry.startswith("[") and entry.endswith("]"):
            entry = entry[1:-1]
        if entry and entry not in entries:
            entries.append(entry)
    return ",".join(entries)


def worker_environment() -> dict[str, str]:
    """The environment a worker process is started with."""
    environment = {**os.environ, "PYTHONPATH": str(bundle_root())}
    # A frozen app boots with PYTHONHOME aimed at its own bundled stdlib; the
    # worker is a different interpreter and would die looking for ``encodings``.
    environment.pop("PYTHONHOME", None)
    for name in ("NO_PROXY", "no_proxy"):
        value = environment.get(name)
        if value:
            environment[name] = _unbracket_ipv6(value)
    return environment


def worker_command(job_path: Path | None = None) -> list[str]:
    """The argv that starts the worker.

    ``PDFMATHTRANSLATE_WORKER`` may name any executable, which is how a desktop
    bundle ships a frozen worker. Otherwise the worker runs as a module under
    the configured interpreter.
    """
    template = (settings.pdfmathtranslate_worker or "").strip()
    if template:
        parts = shlex.split(template)
        if job_path is not None and not any(str(job_path) in part for part in parts):
            parts.append(str(job_path))
        return parts
    interpreter = (settings.pdfmathtranslate_python or "").strip() or "python3"
    parts = [interpreter, "-m", "workers.pdfmathtranslate"]
    if job_path is not None:
        parts.append(str(job_path))
    return parts


def require_worker_ready() -> None:
    """Fail an upload early when the worker cannot be started at all."""
    executable = worker_command()[0]
    if os.sep in executable:
        if not Path(executable).is_file():
            raise WorkerUnavailable(
                f"worker interpreter not found: {executable}", stage="parse"
            )
        return
    from shutil import which

    if which(executable) is None:
        raise WorkerUnavailable(
            f"worker interpreter not found on PATH: {executable}", stage="parse"
        )


def _job_payload(
    *,
    document_id: str,
    input_pdf: Path,
    output_dir: Path,
    work_dir: Path,
    api_key: str,
    base_url: str,
    model: str,
    glossary_path: Path | None,
    translation_domain: str = "general",
) -> dict:
    payload = {
        "job_id": document_id,
        "input_pdf": str(input_pdf),
        "output_dir": str(output_dir),
        "work_dir": str(work_dir),
        "translation": {
            "api_key": api_key,
            "base_url": base_url,
            "model": model,
            "custom_system_prompt": build_worker_system_prompt(translation_domain),
        },
        "options": {
            "output": settings.pdfmathtranslate_output_mode,
            "no_watermark": True,
            "debug": bool(settings.pdfmathtranslate_debug),
        },
        "qps": max(1, int(settings.pdfmathtranslate_qps)),
    }
    if glossary_path is not None:
        payload["glossary"] = str(glossary_path)
    return payload


class WorkerRun:
    """One worker process, tracked so it can always be stopped."""

    def __init__(self, process: subprocess.Popen) -> None:
        self.process = process
        self.timed_out = False
        self.cancelled = False
        self._lock = threading.Lock()

    @property
    def pid(self) -> int:
        return self.process.pid

    def cancel(self) -> None:
        """Stop the worker; it cleans up its own working directory on SIGTERM."""
        self.cancelled = True
        self._stop(force=False)

    def kill(self) -> None:
        # ``SIGKILL`` is not defined on Windows. ``Popen.kill`` provides the
        # same forceful operation on every supported platform.
        self._stop(force=True)

    def _stop(self, *, force: bool) -> None:
        with self._lock:
            if self.process.poll() is not None:
                return
            try:
                self.process.kill() if force else self.process.terminate()
                self.process.wait(timeout=10)
            except (subprocess.TimeoutExpired, OSError):
                try:
                    self.process.kill()
                    self.process.wait(timeout=10)
                except (subprocess.TimeoutExpired, OSError):
                    pass


_ACTIVE_RUNS: dict[str, WorkerRun] = {}
_ACTIVE_RUNS_LOCK = threading.RLock()


def cancel_worker(document_id: str) -> bool:
    """Cancel the worker currently translating ``document_id``."""
    with _ACTIVE_RUNS_LOCK:
        run = _ACTIVE_RUNS.get(document_id)
    if run is None:
        return False
    run.cancel()
    return True


def _parse_event(line: str) -> dict | None:
    line = line.strip()
    if not line.startswith("{"):
        return None
    try:
        event = json.loads(line)
    except ValueError:
        return None
    return event if isinstance(event, dict) else None


def _deliver(events: list[dict], event: dict, sink: EventSink | None) -> None:
    events.append(event)
    if sink is not None:
        try:
            sink(event)
        except Exception:  # noqa: BLE001 - reporting never fails a job
            pass


def _read_events(stream, sink: EventSink | None) -> list[dict]:
    """Drain the worker's event stream. Never raises: the exit code decides."""
    events: list[dict] = []
    for line in stream:
        event = _parse_event(line)
        if event is not None:
            _deliver(events, event, sink)
    return events


def _read_job_events(stream, sink: EventSink | None) -> list[dict]:
    """Read one job's events off a persistent worker's stream.

    The stream stays open for the next job, so a job ends at its own
    ``finish`` or ``error`` event, not at EOF.
    """
    events: list[dict] = []
    for line in stream:
        event = _parse_event(line)
        if event is None:
            continue
        _deliver(events, event, sink)
        if event.get("type") in {"finish", "error"}:
            break
    return events


class _WorkerHandle:
    """A persistent worker process plus the bookkeeping reuse decisions need."""

    def __init__(self, process: subprocess.Popen) -> None:
        self.process = process
        self.jobs_done = 0
        self.stderr_tail: deque[str] = deque(maxlen=200)
        self.drainer = threading.Thread(target=self._drain_stderr, daemon=True)
        self.drainer.start()

    def _drain_stderr(self) -> None:
        # stderr must be read continuously: a worker that fills its pipe
        # buffer blocks mid-job. The tail is kept for crash reports.
        stream = self.process.stderr
        if stream is None:
            return
        for line in stream:
            self.stderr_tail.append(line)


_SERVE_LOCK = threading.Lock()
_HANDLE: _WorkerHandle | None = None


def _serve_command() -> list[str]:
    return [*worker_command(), "--serve"]


def _worker_handle() -> _WorkerHandle:
    """The live worker, starting one when none is running."""
    global _HANDLE
    if _HANDLE is not None and _HANDLE.process.poll() is None:
        return _HANDLE
    # On Windows a console-subsystem worker launched from the windowed app
    # would get its own console window; keep it headless.
    hidden = (
        {"creationflags": subprocess.CREATE_NO_WINDOW}
        if os.name == "nt"
        else {}
    )
    process = subprocess.Popen(
        _serve_command(),
        cwd=str(bundle_root()),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=worker_environment(),
        **hidden,
    )
    _HANDLE = _WorkerHandle(process)
    return _HANDLE


def _drop_worker(handle: _WorkerHandle) -> None:
    """Stop reusing a worker: forget it and kill the process."""
    global _HANDLE
    if _HANDLE is handle:
        _HANDLE = None
    WorkerRun(handle.process).kill()


def shutdown_worker() -> None:
    """Stop the persistent worker, if one is running."""
    global _HANDLE
    handle = _HANDLE
    _HANDLE = None
    if handle is not None:
        WorkerRun(handle.process).kill()


def worker_runtime_configured() -> bool:
    """Whether the operator pointed the worker at a real runtime.

    The fallback interpreter is a bare ``python3`` with none of the worker's
    dependencies; prewarming it would spawn a process that only idles.
    """
    if settings.pdfmathtranslate_worker.strip():
        return True
    return settings.pdfmathtranslate_python != "python3"


def prewarm_worker() -> threading.Thread | None:
    """Start the persistent worker ahead of the first job, in the background.

    The worker pays its interpreter and model startup while the operator is
    still settling in, instead of inside the first translation. Best-effort:
    a worker that cannot start raises the same error again when a real job
    asks for one, where it is reported properly.
    """
    if not worker_runtime_configured():
        return None

    def _start() -> None:
        try:
            with _SERVE_LOCK:
                _worker_handle()
        except Exception:  # noqa: BLE001 - the first real job reports it
            pass

    thread = threading.Thread(target=_start, daemon=True, name="worker-prewarm")
    thread.start()
    return thread


def _worker_crash_error(handle: _WorkerHandle) -> WorkerError:
    """The error reported when the worker died without describing why."""
    try:
        code = handle.process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        code = handle.process.poll()
    handle.drainer.join(timeout=2)
    tail = [line.strip() for line in handle.stderr_tail if line.strip()][-3:]
    detail = f": {' | '.join(tail)}" if tail else ""
    return WorkerError(f"worker exited with code {code}{detail}", stage="parse")


def _send_job(
    handle: _WorkerHandle,
    run: WorkerRun,
    job_path: Path,
    on_event: EventSink | None,
) -> tuple[_WorkerHandle, list[dict]]:
    """Hand the job to the worker and read its events.

    A worker that died between jobs is only noticed when the write fails; one
    retry on a fresh process keeps that race invisible to the caller. A worker
    that has never completed a job is not retried — it never worked at all.
    """
    try:
        assert handle.process.stdin is not None
        handle.process.stdin.write(f"{job_path}\n")
        handle.process.stdin.flush()
    except (BrokenPipeError, OSError):
        if handle.jobs_done == 0:
            raise _worker_crash_error(handle)
        _drop_worker(handle)
        handle = _worker_handle()
        run.process = handle.process
        assert handle.process.stdin is not None
        handle.process.stdin.write(f"{job_path}\n")
        handle.process.stdin.flush()
    return handle, _read_job_events(handle.process.stdout, on_event)


def run_worker(
    *,
    document_id: str,
    input_pdf: Path,
    output_dir: Path,
    work_dir: Path,
    api_key: str,
    base_url: str,
    model: str,
    glossary_path: Path | None = None,
    translation_domain: str = "general",
    on_event: EventSink | None = None,
    on_start: Callable[[WorkerRun], None] | None = None,
    timeout: float | None = None,
) -> WorkerProducts:
    """Translate one PDF and return the products the worker published."""
    require_worker_ready()
    output_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    job_path = work_dir / "job.json"
    job_path.write_text(
        json.dumps(
            _job_payload(
                document_id=document_id,
                input_pdf=input_pdf,
                output_dir=output_dir,
                work_dir=work_dir,
                api_key=api_key,
                base_url=base_url,
                model=model,
                glossary_path=glossary_path,
                translation_domain=translation_domain,
            ),
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    started = time.monotonic()
    budget = settings.pdfmathtranslate_timeout if timeout is None else timeout

    # The persistent worker runs one job at a time, so the lock is held for
    # the whole job: handing two jobs to one stdin would interleave events.
    with _SERVE_LOCK:
        handle = _worker_handle()
        run = WorkerRun(handle.process)
        with _ACTIVE_RUNS_LOCK:
            _ACTIVE_RUNS[document_id] = run
        if on_start is not None:
            on_start(run)

        def _deadline() -> None:
            run.timed_out = True
            run.kill()

        watchdog = threading.Timer(budget, _deadline)
        watchdog.daemon = True
        watchdog.start()

        try:
            handle, events = _send_job(handle, run, job_path, on_event)
        finally:
            watchdog.cancel()
            with _ACTIVE_RUNS_LOCK:
                if _ACTIVE_RUNS.get(document_id) is run:
                    del _ACTIVE_RUNS[document_id]

        if run.timed_out:
            _drop_worker(handle)
            raise WorkerError(
                f"worker exceeded its {int(budget)}s budget", stage="translate"
            )
        if run.cancelled:
            _drop_worker(handle)
            raise WorkerCancelled("worker cancelled", stage="translate")

        failure = _last_error(events)
        if failure is not None:
            # A job-level error leaves a healthy worker usable for the next
            # job; a dead one is dropped either way.
            if handle.process.poll() is not None:
                _drop_worker(handle)
            else:
                handle.jobs_done += 1
            raise WorkerError(failure[0], stage=failure[1])

        finish = _last_finish(events)
        if finish is None:
            error = _worker_crash_error(handle)
            _drop_worker(handle)
            raise error

        handle.jobs_done += 1

    products = _products_from_finish(finish, output_dir)
    elapsed = time.monotonic() - started
    products.mode_label = f"{products.mode_label} · {elapsed:.0f}s"
    return products


def _last_error(events: Iterable[dict]) -> tuple[str, str] | None:
    for event in reversed(list(events)):
        if event.get("type") == "error":
            stage = str(event.get("stage") or "parse")
            return str(event.get("message") or "worker failed"), stage
    return None


def _last_finish(events: Iterable[dict]) -> dict | None:
    for event in reversed(list(events)):
        if event.get("type") == "finish":
            return event
    return None


def _existing_path(value: object) -> Path | None:
    text = str(value or "").strip()
    if not text:
        return None
    path = Path(text)
    return path if path.is_file() else None


def _products_from_finish(finish: dict, output_dir: Path) -> WorkerProducts:
    translated = _existing_path(finish.get("translated_pdf"))
    if translated is None:
        raise WorkerError(
            f"worker reported a translated PDF that does not exist: "
            f"{finish.get('translated_pdf')}",
            stage="render",
        )
    manifest = _existing_path(finish.get("manifest_path"))
    if manifest is None:
        raise WorkerError(
            f"worker reported a manifest that does not exist: "
            f"{finish.get('manifest_path')}",
            stage="render",
        )
    extraction = Path(str(finish.get("extraction_dir") or output_dir / "extraction"))
    debug = Path(str(finish.get("debug_dir") or extraction / "debug"))
    dual = None
    reported_dual = str(finish.get("dual_pdf") or "").strip()
    if reported_dual:
        dual = _existing_path(reported_dual)
        if dual is None:
            raise WorkerError(
                f"worker reported a bilingual PDF that does not exist: {reported_dual}",
                stage="render",
            )
    return WorkerProducts(
        translated_pdf=translated,
        manifest_path=manifest,
        extraction_dir=extraction,
        debug_dir=debug,
        mode_label=str(finish.get("mode_label") or "PDFMathTranslate-next"),
        page_count=int(finish.get("page_count") or 0),
        glossary_path=_existing_path(finish.get("glossary_path")),
        dual_pdf=dual,
    )
