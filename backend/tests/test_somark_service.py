import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.core.config import settings
from app.services import somark_service
from app.services.somark_service import SoMarkConfig


def _json_resp(status: int, payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = payload
    resp.text = str(payload)
    return resp


def _bytes_resp(status: int, data: bytes) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.content = data
    return resp


def _config(**overrides) -> SoMarkConfig:
    values = {
        "api_key": "sk-test",
        "base_url": "https://somark.example/api/v1",
        "poll_interval": 0.0,
        "timeout": 30.0,
    }
    values.update(overrides)
    return SoMarkConfig(**values)


def _page() -> dict:
    return {
        "page_num": 0,
        "page_size": {"w": 1000, "h": 2000},
        "blocks": [
            {
                "idx": 0,
                "type": "image",
                "bbox": [100, 200, 300, 400],
                "content": "",
                "format": "image",
                "captions": [1],
                "img_url": "https://cdn.example/img/fig1.png",
            },
            {
                "idx": 1,
                "type": "image_caption",
                "bbox": [100, 410, 300, 440],
                "content": "Figure 1: system overview",
                "format": "text",
                "captions": [],
                "img_url": "",
            },
            {
                "idx": 2,
                "type": "title",
                "bbox": [50, 10, 500, 30],
                "content": "Introduction",
                "format": "text",
                "captions": [],
                "img_url": "",
                "title_level": 2,
            },
            {
                "idx": 3,
                "type": "text",
                "bbox": [50, 50, 500, 100],
                "content": "Body paragraph.",
                "format": "text",
                "captions": [],
                "img_url": "",
            },
            {
                "idx": 4,
                "type": "formula",
                "bbox": [200, 120, 400, 160],
                "content": "$$E = mc^2$$",
                "format": "latex",
                "captions": [],
                "img_url": "",
            },
            {
                "idx": 5,
                "type": "table",
                "bbox": [50, 600, 500, 800],
                "content": "<table><tr><td>1</td></tr></table>",
                "format": "html",
                "captions": [],
                "img_url": "https://cdn.example/img/tbl1.png",
            },
        ],
    }


def _success_payload(markdown: str = "# Introduction\n\nBody paragraph.") -> dict:
    return {
        "code": 0,
        "message": "查询成功",
        "data": {
            "task_id": "T1",
            "status": "SUCCESS",
            "metadata": {"page_num": 1, "file_type": ".pdf"},
            "result": {
                "outputs": {
                    "markdown": markdown,
                    "json": {"pages": [_page()]},
                }
            },
        },
    }


def test_extract_structured_from_pdf_somark_happy_path(tmp_path):
    pdf = tmp_path / "demo.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")

    submit = _json_resp(200, {"code": 0, "message": "任务已提交", "data": {"task_id": "T1", "status": "QUEUING"}})
    processing = _json_resp(200, {"code": 0, "data": {"task_id": "T1", "status": "PROCESSING"}})
    success = _json_resp(200, _success_payload())
    image = _bytes_resp(200, b"png-bytes")

    logs: list[str] = []
    progress: list[tuple[float, str]] = []
    with patch.object(somark_service.requests, "post", side_effect=[submit, processing, success]) as post, \
         patch.object(somark_service.requests, "get", return_value=image) as get:
        out_dir = tmp_path / "out"
        result = somark_service.extract_structured_from_pdf_somark(
            str(pdf),
            out_dir,
            log_sink=logs,
            progress_cb=lambda frac, label: progress.append((frac, label)),
            config=_config(),
        )

    # The submit body repeats output_formats and carries both JSON options.
    fields = post.call_args_list[0].kwargs["data"]
    assert ("api_key", "sk-test") in fields
    assert [value for name, value in fields if name == "output_formats"] == ["markdown", "json"]
    assert json.loads(dict(fields)["element_formats"])["formula"] == "latex"
    assert get.call_count == 2  # figure + table image

    assert result.mode_label == "somark"
    assert result.markdown == "# Introduction\n\nBody paragraph."
    assert result.layout_payload is None
    assert result.boxes_normalized is True

    blocks = result.content_blocks
    assert [block["type"] for block in blocks[0]] == [
        "image",
        "title",
        "paragraph",
        "equation_interline",
        "table",
    ]

    image_block, title_block, paragraph_block, formula_block, table_block = blocks[0]
    assert image_block["content"]["image_source"]["path"] == "images/fig1.png"
    assert image_block["content"]["image_caption"] == [
        {"type": "text", "content": "Figure 1: system overview"}
    ]
    assert image_block["bbox"] == [100, 100, 300, 200]
    assert title_block["content"]["level"] == 2
    assert title_block["content"]["title_content"] == [{"type": "text", "content": "Introduction"}]
    assert paragraph_block["content"]["paragraph_content"] == [
        {"type": "text", "content": "Body paragraph."}
    ]
    assert formula_block["content"]["math_content"] == "E = mc^2"
    assert table_block["content"]["html"].startswith("<table>")
    assert table_block["content"]["image_source"]["path"] == "images/tbl1.png"
    # The referenced caption block is folded into its owner, not emitted again.
    assert not any(
        "Figure 1: system overview" in str(block.get("content"))
        for block in blocks[0]
        if block["type"] != "image"
    )

    assert (out_dir / "images" / "fig1.png").read_bytes() == b"png-bytes"
    assert (out_dir / "images" / "tbl1.png").is_file()
    assert result.images_dir == out_dir / "images"
    assert {path.name for path in result.extracted_files} == {"somark.md", "somark.json"}
    assert (out_dir / "somark.md").read_text(encoding="utf-8") == result.markdown
    stored = json.loads((out_dir / "somark.json").read_text(encoding="utf-8"))
    assert stored["data"]["task_id"] == "T1"

    assert ("SoMark state: PROCESSING" in logs)
    assert progress[0] == (0.35, "SoMark 任务已提交")
    assert progress[-1] == (0.95, "SoMark 解析完成")


def test_paragraph_inline_math_is_typed(tmp_path):
    """Inline ``$...$`` in prose becomes equation_inline items, not plain text.

    SoMark types display equations as blocks but embeds inline math in the
    text; without the split, `_has_typed_math` sees the interline blocks and
    disables bare-dollar splitting, so the LaTeX would reach the rendered PDF
    as literal ``$...$`` text.
    """
    pdf = tmp_path / "demo.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")

    page = _page()
    page["blocks"][3]["content"] = (
        r"The kernels $G _ {\sigma _ {s}}$ and $G _ {\sigma _ {r}}$ are Gaussians."
    )
    payload = _success_payload()
    payload["data"]["result"]["outputs"]["json"]["pages"] = [page]

    submit = _json_resp(200, {"code": 0, "data": {"task_id": "T1", "status": "QUEUING"}})
    success = _json_resp(200, payload)
    image = _bytes_resp(200, b"png-bytes")

    with patch.object(somark_service.requests, "post", side_effect=[submit, success]), \
         patch.object(somark_service.requests, "get", return_value=image):
        result = somark_service.extract_structured_from_pdf_somark(
            str(pdf), tmp_path / "out", config=_config()
        )

    paragraph = next(b for b in result.content_blocks[0] if b["type"] == "paragraph")
    assert paragraph["content"]["paragraph_content"] == [
        {"type": "text", "content": "The kernels "},
        {"type": "equation_inline", "content": r"G _ {\sigma _ {s}}"},
        {"type": "text", "content": " and "},
        {"type": "equation_inline", "content": r"G _ {\sigma _ {r}}"},
        {"type": "text", "content": " are Gaussians."},
    ]


def test_unreferenced_caption_block_keeps_its_text(tmp_path):
    """A caption no figure claims is emitted as text instead of disappearing."""
    pdf = tmp_path / "demo.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")

    payload = {
        "code": 0,
        "data": {
            "task_id": "T1",
            "status": "SUCCESS",
            "result": {
                "outputs": {
                    "markdown": "orphan caption",
                    "json": {
                        "pages": [
                            {
                                "page_num": 0,
                                "page_size": {"w": 1000, "h": 1000},
                                "blocks": [
                                    {
                                        "idx": 0,
                                        "type": "image_caption",
                                        "bbox": [0, 0, 500, 50],
                                        "content": "Table 1: orphaned caption",
                                        "captions": [],
                                        "img_url": "",
                                    }
                                ],
                            }
                        ]
                    },
                }
            },
        },
    }
    submit = _json_resp(200, {"code": 0, "data": {"task_id": "T1", "status": "QUEUING"}})
    success = _json_resp(200, payload)

    with patch.object(somark_service.requests, "post", side_effect=[submit, success]):
        result = somark_service.extract_structured_from_pdf_somark(
            str(pdf), tmp_path / "out", config=_config()
        )

    assert result.content_blocks[0] == [
        {
            "type": "paragraph",
            "bbox": [0, 0, 500, 50],
            "content": {
                "paragraph_content": [{"type": "text", "content": "Table 1: orphaned caption"}]
            },
        }
    ]


def test_failed_task_raises_with_server_message(tmp_path):
    pdf = tmp_path / "demo.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")

    submit = _json_resp(200, {"code": 0, "data": {"task_id": "T1", "status": "QUEUING"}})
    failed = _json_resp(200, {"code": 0, "data": {"task_id": "T1", "status": "FAILED", "message": "corrupted pdf"}})

    with patch.object(somark_service.requests, "post", side_effect=[submit, failed]):
        with pytest.raises(RuntimeError, match="corrupted pdf"):
            somark_service.extract_structured_from_pdf_somark(
                str(pdf), tmp_path / "out", config=_config()
            )


def test_missing_api_key_raises_without_http(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "somark_api_key", "")
    pdf = tmp_path / "demo.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")

    with patch.object(somark_service.requests, "post") as post:
        with pytest.raises(RuntimeError, match="SOMARK_API_KEY"):
            somark_service.extract_structured_from_pdf_somark(str(pdf), tmp_path / "out")

    post.assert_not_called()


def test_polling_times_out(tmp_path, monkeypatch):
    monkeypatch.setattr(somark_service.time, "sleep", lambda _seconds: None)
    pdf = tmp_path / "demo.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")

    submit = _json_resp(200, {"code": 0, "data": {"task_id": "T1", "status": "QUEUING"}})
    processing = _json_resp(200, {"code": 0, "data": {"task_id": "T1", "status": "PROCESSING"}})

    def fake_post(url, *args, **kwargs):
        return submit if url.endswith("/parse/async") else processing

    with patch.object(somark_service.requests, "post", side_effect=fake_post):
        with pytest.raises(RuntimeError, match="轮询超时"):
            somark_service.extract_structured_from_pdf_somark(
                str(pdf), tmp_path / "out", config=_config(timeout=0.02)
            )


def test_invalid_api_key_is_reported(tmp_path):
    pdf = tmp_path / "demo.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")

    rejected = _json_resp(200, {"code": 1107, "message": "invalid api key", "data": None})

    with patch.object(somark_service.requests, "post", return_value=rejected):
        with pytest.raises(RuntimeError, match="API Key"):
            somark_service.extract_structured_from_pdf_somark(
                str(pdf), tmp_path / "out", config=_config()
            )


def test_unreachable_image_is_skipped(tmp_path):
    pdf = tmp_path / "demo.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")

    submit = _json_resp(200, {"code": 0, "data": {"task_id": "T1", "status": "QUEUING"}})
    success = _json_resp(200, _success_payload())
    failed_download = _bytes_resp(404, b"")

    logs: list[str] = []
    with patch.object(somark_service.requests, "post", side_effect=[submit, success]), \
         patch.object(somark_service.requests, "get", return_value=failed_download):
        result = somark_service.extract_structured_from_pdf_somark(
            str(pdf), tmp_path / "out", log_sink=logs, config=_config()
        )

    assert result.images_dir is None
    assert all(block["type"] != "image" for block in result.content_blocks[0])
    assert any("image download skipped" in line for line in logs)
