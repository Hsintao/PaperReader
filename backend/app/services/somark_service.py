"""SoMark 文档解析 API client.

Submits a PDF to SoMark's asynchronous parse endpoint, polls the task and
converts the structured result into the `content_list_v2` shape the rest of
the pipeline already consumes, so the IR builder, the extraction checkpoint,
translation and rendering stay unchanged.

Public surface mirrors `mineru_service.extract_structured_from_pdf`.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urlparse

import requests

from app.core.config import settings
from app.services.mineru_service import MinerUResult


class SoMarkError(RuntimeError):
    """A SoMark response that is authoritative (bad key, bad request)."""


@dataclass(frozen=True)
class SoMarkConfig:
    api_key: str
    base_url: str
    poll_interval: float
    timeout: float


def _default_config() -> SoMarkConfig:
    return SoMarkConfig(
        api_key=settings.somark_api_key,
        base_url=settings.somark_base_url,
        poll_interval=settings.somark_poll_interval,
        timeout=settings.somark_timeout,
    )


# Requested once per task: LaTeX formulas, HTML tables and image URLs are what
# the block mapping below expects. Title levels drive the outline the reader
# shows, and header/footer text is dropped server-side.
_ELEMENT_FORMATS = {"image": "url", "formula": "latex", "table": "html", "cs": "image"}
_FEATURE_CONFIG = {
    "enable_title_level_recognition": True,
    "enable_inline_image": True,
    "enable_table_image": True,
    "enable_image_understanding": True,
    "keep_header_footer": False,
}

_TITLE_TYPES = {"title"}
_FORMULA_TYPES = {"formula", "equation", "chem_formula"}
_TABLE_TYPES = {"table"}
_IMAGE_TYPES = {"image", "chart", "figure", "qrcode", "seal", "cs", "inline_image"}
_CAPTION_TYPES = {"image_caption", "table_caption", "figure_caption", "caption"}

_STATUS_PROGRESS = {
    "QUEUING": (0.55, "SoMark 排队中"),
    "PROCESSING": (0.75, "SoMark 解析中"),
}


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------

def _check_response(resp: requests.Response, action: str) -> dict:
    if resp.status_code != 200:
        raise RuntimeError(f"SoMark {action} HTTP {resp.status_code}: {resp.text[:300]}")
    try:
        payload = resp.json()
    except ValueError as exc:
        raise RuntimeError(f"SoMark {action} returned non-JSON body: {resp.text[:300]}") from exc
    if payload.get("code") != 0:
        message = str(payload.get("message") or "").strip()
        if payload.get("code") == 1107:
            message = f"{message}（请检查设置中的 SoMark API Key 是否有效）"
        raise SoMarkError(
            f"SoMark {action} failed (code={payload.get('code')}): {message}"
        )
    return payload


def _submit_task(pdf_path: str, config: SoMarkConfig) -> str:
    """Upload the PDF and return the async task id."""
    url = f"{config.base_url.rstrip('/')}/parse/async"
    # `output_formats` is a repeated multipart field, so the body is built as a
    # list of pairs instead of a dict.
    fields = [
        ("api_key", config.api_key),
        ("output_formats", "markdown"),
        ("output_formats", "json"),
        ("element_formats", json.dumps(_ELEMENT_FORMATS, ensure_ascii=False)),
        ("feature_config", json.dumps(_FEATURE_CONFIG, ensure_ascii=False)),
    ]
    with open(pdf_path, "rb") as handle:
        resp = requests.post(
            url,
            data=fields,
            files={"file": (Path(pdf_path).name, handle, "application/pdf")},
            timeout=300,
        )
    payload = _check_response(resp, "submit task")
    task_id = str((payload.get("data") or {}).get("task_id") or "")
    if not task_id:
        raise RuntimeError(f"SoMark submit task: missing task_id in {payload.get('data')}")
    return task_id


def _poll_task(
    task_id: str,
    config: SoMarkConfig,
    log_sink: list[str] | None = None,
    progress_cb: Callable[[float, str], None] | None = None,
) -> dict:
    """Poll until the task succeeds; returns the complete API response."""
    url = f"{config.base_url.rstrip('/')}/parse/async_check"
    interval = max(0.0, float(config.poll_interval))
    deadline = time.time() + max(0.0, float(config.timeout))
    last_status = ""

    while time.time() < deadline:
        try:
            resp = requests.post(
                url, data={"api_key": config.api_key, "task_id": task_id}, timeout=30
            )
            payload = _check_response(resp, "poll task")
        except SoMarkError:
            raise
        except (requests.RequestException, RuntimeError) as exc:
            # A single failed poll (gateway hiccup, dropped connection) is not
            # fatal: keep polling until the deadline.
            if log_sink is not None:
                log_sink.append(f"SoMark poll retry: {exc}")
            time.sleep(interval)
            continue

        data = payload.get("data") or {}
        status = str(data.get("status") or "")
        if status != last_status:
            last_status = status
            if log_sink is not None:
                log_sink.append(f"SoMark state: {status}")
            if progress_cb is not None and status in _STATUS_PROGRESS:
                fraction, label = _STATUS_PROGRESS[status]
                progress_cb(fraction, label)
        if status == "SUCCESS":
            if progress_cb is not None:
                progress_cb(0.95, "SoMark 解析完成")
            return payload
        if status == "FAILED":
            raise RuntimeError(
                f"SoMark parsing failed: {data.get('message') or payload.get('message') or 'unknown error'}"
            )
        time.sleep(interval)

    raise RuntimeError(f"SoMark 任务轮询超时: task_id={task_id}")


# ---------------------------------------------------------------------------
# Result conversion
# ---------------------------------------------------------------------------

def _image_filename(url: str, index: int) -> str:
    name = re.sub(r"[^0-9A-Za-z._-]+", "_", Path(unquote(urlparse(url).path)).name)
    if not name:
        name = f"image_{index}.png"
    if not Path(name).suffix:
        name += ".png"
    return name


def _download_images(
    pages: list,
    images_dir: Path,
    log_sink: list[str] | None = None,
) -> dict[str, str]:
    """Download every referenced asset. Returns {img_url: "images/<file>"}."""
    urls: list[str] = []
    for page in pages:
        if not isinstance(page, dict):
            continue
        for block in page.get("blocks") or []:
            if not isinstance(block, dict):
                continue
            url = str(block.get("img_url") or "").strip()
            if url and url not in urls:
                urls.append(url)
    if not urls:
        return {}

    images_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, str] = {}
    taken: set[str] = set()
    for index, url in enumerate(urls, start=1):
        name = _image_filename(url, index)
        if name in taken:
            name = f"{index}_{name}"
        target = images_dir / name
        try:
            resp = requests.get(url, timeout=(30, 300))
            if resp.status_code != 200:
                raise RuntimeError(f"HTTP {resp.status_code}")
            target.write_bytes(resp.content)
        except (requests.RequestException, OSError, RuntimeError) as exc:
            # A missing asset must not fail the parse: the block is dropped
            # downstream, exactly as an unreachable MinerU image is.
            if log_sink is not None:
                log_sink.append(f"SoMark image download skipped ({name}): {exc}")
            continue
        taken.add(name)
        paths[url] = f"images/{name}"
    return paths


def _as_float(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _normalize_bbox(raw: object, width: float, height: float) -> list[int]:
    """Convert a pixel bbox against the page size into 0-1000 coordinates."""
    if not isinstance(raw, (list, tuple)) or len(raw) < 4 or not width or not height:
        return []
    try:
        x0, y0, x1, y1 = (float(value) for value in raw[:4])
    except (TypeError, ValueError):
        return []
    return [
        min(1000, max(0, round(x0 / width * 1000))),
        min(1000, max(0, round(y0 / height * 1000))),
        min(1000, max(0, round(x1 / width * 1000))),
        min(1000, max(0, round(y1 / height * 1000))),
    ]


def _strip_math_delimiters(text: str) -> str:
    value = text.strip()
    for opening, closing in (("$$", "$$"), (r"\[", r"\]"), (r"\(", r"\)"), ("$", "$")):
        if value.startswith(opening) and value.endswith(closing) and len(value) > len(opening) + len(closing):
            return value[len(opening) : -len(closing)].strip()
    return value


def _caption_items(caption: str) -> list[dict]:
    return [{"type": "text", "content": caption}] if caption else []


def _caption_text(block: dict, captions: dict[int, str]) -> str:
    """Caption text referenced by this block's `captions` index list."""
    parts = [captions[idx] for idx in block.get("captions") or [] if idx in captions]
    return " ".join(part for part in parts if part).strip()


def _convert_block(
    block: dict,
    kind: str,
    text: str,
    captions: dict[int, str],
    image_paths: dict[str, str],
    width: float,
    height: float,
) -> dict | None:
    bbox = _normalize_bbox(block.get("bbox"), width, height)
    url = str(block.get("img_url") or "").strip()
    rel_path = image_paths.get(url, "") if url else ""

    if kind in _TITLE_TYPES:
        if not text:
            return None
        try:
            level = int(block.get("title_level") or 1)
        except (TypeError, ValueError):
            level = 1
        return {
            "type": "title",
            "bbox": bbox,
            "content": {
                "title_content": [{"type": "text", "content": text}],
                "level": max(1, level),
            },
        }

    if kind in _FORMULA_TYPES:
        latex = _strip_math_delimiters(text)
        if not latex:
            return None
        return {"type": "equation_interline", "bbox": bbox, "content": {"math_content": latex}}

    if kind in _TABLE_TYPES:
        if not text and not rel_path:
            return None
        content: dict = {
            "html": text,
            "table_caption": _caption_items(_caption_text(block, captions)),
        }
        if rel_path:
            content["image_source"] = {"path": rel_path}
        return {"type": "table", "bbox": bbox, "content": content}

    # A block that carries artwork is a figure, whether or not its type is one
    # of the known image kinds. An unknown type that also has text stays prose,
    # so its wording is not dropped.
    if kind in _IMAGE_TYPES or (rel_path and not text):
        if not rel_path:
            return None
        content = {"image_source": {"path": rel_path}}
        caption = _caption_text(block, captions)
        if caption:
            content["image_caption"] = _caption_items(caption)
        return {"type": "image", "bbox": bbox, "content": content}

    if not text:
        return None
    return {
        "type": "paragraph",
        "bbox": bbox,
        "content": {"paragraph_content": [{"type": "text", "content": text}]},
    }


def _to_content_blocks(pages: list, image_paths: dict[str, str]) -> list[list[dict]]:
    """Map SoMark's per-page blocks onto MinerU `content_list_v2` blocks."""
    result: list[list[dict]] = []
    for page in pages:
        if not isinstance(page, dict):
            result.append([])
            continue
        raw_blocks = [block for block in page.get("blocks") or [] if isinstance(block, dict)]
        size = page.get("page_size") or {}
        width = _as_float(size.get("w"))
        height = _as_float(size.get("h"))

        # A caption block is referenced by its figure/table through `captions`;
        # its text belongs to that owner, so the block itself is not emitted.
        text_by_idx = {
            block.get("idx"): str(block.get("content") or "").strip()
            for block in raw_blocks
            if isinstance(block.get("idx"), int) and str(block.get("content") or "").strip()
        }
        captions: dict[int, str] = {}
        for block in raw_blocks:
            for idx in block.get("captions") or []:
                if isinstance(idx, int) and idx in text_by_idx:
                    captions.setdefault(idx, text_by_idx[idx])
        referenced = set(captions)

        converted: list[dict] = []
        for block in raw_blocks:
            kind = str(block.get("type") or "").strip().lower()
            text = str(block.get("content") or "").strip()
            if kind in _CAPTION_TYPES and block.get("idx") in referenced:
                continue
            item = _convert_block(block, kind, text, captions, image_paths, width, height)
            if item is not None:
                converted.append(item)
        result.append(converted)
    return result


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def extract_structured_from_pdf_somark(
    pdf_path: str,
    output_dir: Path,
    log_sink: list[str] | None = None,
    progress_cb: Callable[[float, str], None] | None = None,
    config: SoMarkConfig | None = None,
) -> MinerUResult:
    """Submit `pdf_path` to SoMark and return the MinerU-shaped result.

    `output_dir` receives `somark.md`, `somark.json` (the full API response)
    and the downloaded assets under `images/`.
    """
    config = config or _default_config()
    if not config.api_key:
        raise RuntimeError(
            "SOMARK_API_KEY is not configured. Set it in your .env to enable PDF parsing via SoMark."
        )

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    task_id = _submit_task(pdf_path, config)
    if log_sink is not None:
        log_sink.append(f"SoMark task_id: {task_id}")
    if progress_cb is not None:
        progress_cb(0.35, "SoMark 任务已提交")

    response = _poll_task(task_id, config, log_sink=log_sink, progress_cb=progress_cb)
    result = (response.get("data") or {}).get("result") or {}
    outputs = result.get("outputs") or {}
    markdown = str(outputs.get("markdown") or "")

    md_path = output_dir / "somark.md"
    md_path.write_text(markdown, encoding="utf-8")
    json_path = output_dir / "somark.json"
    json_path.write_text(json.dumps(response, ensure_ascii=False, indent=2), encoding="utf-8")

    structured = outputs.get("json")
    pages = structured.get("pages") or [] if isinstance(structured, dict) else []
    pages = [page for page in pages if isinstance(page, dict)]

    images_dir = output_dir / "images"
    image_paths = _download_images(pages, images_dir, log_sink=log_sink)
    content_blocks = _to_content_blocks(pages, image_paths)

    return MinerUResult(
        markdown=markdown,
        mode_label="somark",
        extracted_files=[md_path, json_path],
        content_blocks=content_blocks,
        layout_payload=None,
        boxes_normalized=True,
        images_dir=images_dir if image_paths else None,
        two_column=False,
    )
