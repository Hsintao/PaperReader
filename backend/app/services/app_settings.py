"""Application settings for a single local operator.

Everything the pipeline needs — LLM endpoint, PDF parser, MinerU options,
reading preferences — lives in one JSON file under the data directory. The
file is created with owner-only permissions, so provider keys stay as private
as the rest of the local data.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path

from fastapi import HTTPException

from app.core.config import settings
from app.services.translation_prompts import normalize_domain


_LOCK = threading.RLock()
_THEMES = {"light", "dark"}
_VISION_MODES = {"auto", "manual"}
_PARSERS = {"local", "mineru"}


def settings_path() -> Path:
    return settings.data_dir / "settings.json"


@dataclass
class AppSettings:
    api_key: str = ""
    base_url: str = ""
    model: str = ""
    pdf_parser: str = ""
    mineru_api_key: str = ""
    mineru_base_url: str = ""
    mineru_model_version: str = ""
    mineru_language: str = ""
    mineru_enable_formula: bool = True
    mineru_enable_table: bool = True
    mineru_is_ocr: bool = False
    vision_model: str = ""
    theme: str = "light"
    vision_enabled: bool = False
    vision_mode: str = "auto"
    show_annotated_pdf: bool = False
    translation_domain: str = "general"
    favorites: list[str] = field(default_factory=list)


def _defaults() -> AppSettings:
    return AppSettings(
        base_url=settings.openai_base_url,
        model=settings.openai_model,
        pdf_parser=settings.pdf_parser or "mineru",
        mineru_base_url=settings.mineru_base_url,
        mineru_model_version=settings.mineru_model_version,
        mineru_language=settings.mineru_language,
        mineru_enable_formula=settings.mineru_enable_formula,
        mineru_enable_table=settings.mineru_enable_table,
        mineru_is_ocr=settings.mineru_is_ocr,
        vision_model=settings.vision_model,
    )


def _read_raw(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _coerce(raw: dict) -> AppSettings:
    """Merge stored values over the configured defaults, ignoring bad types."""
    current = _defaults()
    for name, default in asdict(current).items():
        if name not in raw:
            continue
        value = raw[name]
        if isinstance(default, bool):
            value = bool(value)
        elif isinstance(default, list):
            value = [item for item in value if isinstance(item, str)] if isinstance(value, list) else default
        elif isinstance(default, float):
            value = float(value)
        elif isinstance(default, int):
            value = int(value)
        else:
            value = str(value)
        setattr(current, name, value)
    current.translation_domain = normalize_domain(current.translation_domain)
    return current


def _write(values: AppSettings) -> None:
    target = settings_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(asdict(values), ensure_ascii=False, indent=2) + "\n"
    with _LOCK:
        fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(target.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
            os.replace(temporary, target)
            os.chmod(target, stat.S_IRUSR | stat.S_IWUSR)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def load_settings() -> AppSettings:
    with _LOCK:
        return _coerce(_read_raw(settings_path()))


def update_settings(
    *,
    api_key: str | None = None,
    clear_api_key: bool = False,
    base_url: str | None = None,
    model: str | None = None,
    pdf_parser: str | None = None,
    mineru_api_key: str | None = None,
    clear_mineru_api_key: bool = False,
    mineru_base_url: str | None = None,
    mineru_model_version: str | None = None,
    mineru_language: str | None = None,
    mineru_enable_formula: bool | None = None,
    mineru_enable_table: bool | None = None,
    mineru_is_ocr: bool | None = None,
    vision_model: str | None = None,
    theme: str | None = None,
    vision_enabled: bool | None = None,
    vision_mode: str | None = None,
    show_annotated_pdf: bool | None = None,
    translation_domain: str | None = None,
    favorites: list[str] | None = None,
) -> AppSettings:
    current = load_settings()
    if clear_api_key:
        current.api_key = ""
    elif (api_key or "").strip():
        current.api_key = api_key.strip()
    if clear_mineru_api_key:
        current.mineru_api_key = ""
    elif (mineru_api_key or "").strip():
        current.mineru_api_key = mineru_api_key.strip()
    if base_url is not None:
        current.base_url = base_url.strip()
    if model is not None:
        current.model = model.strip()
    if pdf_parser is not None:
        current.pdf_parser = pdf_parser.strip()
    if mineru_base_url is not None:
        current.mineru_base_url = mineru_base_url.strip()
    if mineru_model_version is not None:
        current.mineru_model_version = mineru_model_version.strip()
    if mineru_language is not None:
        current.mineru_language = mineru_language.strip()
    if mineru_enable_formula is not None:
        current.mineru_enable_formula = bool(mineru_enable_formula)
    if mineru_enable_table is not None:
        current.mineru_enable_table = bool(mineru_enable_table)
    if mineru_is_ocr is not None:
        current.mineru_is_ocr = bool(mineru_is_ocr)
    if vision_model is not None:
        current.vision_model = vision_model.strip()
    if theme is not None:
        current.theme = theme
    if vision_enabled is not None:
        current.vision_enabled = bool(vision_enabled)
    if vision_mode is not None:
        current.vision_mode = vision_mode
    if show_annotated_pdf is not None:
        current.show_annotated_pdf = bool(show_annotated_pdf)
    if translation_domain is not None:
        current.translation_domain = translation_domain
    if favorites is not None:
        current.favorites = list(favorites)

    if current.theme not in _THEMES:
        current.theme = "light"
    if current.vision_mode not in _VISION_MODES:
        current.vision_mode = "auto"
    current.translation_domain = normalize_domain(current.translation_domain)
    if current.pdf_parser not in _PARSERS:
        current.pdf_parser = "local"
    if not current.base_url:
        current.base_url = settings.openai_base_url
    if not current.model:
        current.model = settings.openai_model
    if not current.mineru_base_url:
        current.mineru_base_url = settings.mineru_base_url
    if not current.mineru_model_version:
        current.mineru_model_version = settings.mineru_model_version
    if not current.mineru_language:
        current.mineru_language = settings.mineru_language
    if not current.vision_model:
        current.vision_model = settings.vision_model
    if not current.base_url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="LLM Base URL must start with http:// or https://")
    if not current.mineru_base_url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="MinerU Base URL must start with http:// or https://")

    _write(current)
    return current


def require_provider_settings(*, for_pdf: bool = False) -> AppSettings:
    provider = load_settings()
    if not provider.api_key:
        raise HTTPException(
            status_code=409,
            detail={"code": "config_required", "message": "请先在设置中配置大模型 API Key。"},
        )
    if for_pdf and provider.pdf_parser == "mineru" and not provider.mineru_api_key:
        raise HTTPException(
            status_code=409,
            detail={"code": "config_required", "message": "当前选择了 MinerU，请先在设置中配置 MinerU API Key。"},
        )
    return provider


def serialize_settings(value: AppSettings) -> dict:
    """Never expose stored keys; report only whether each one is present."""
    return {
        "api_key_configured": bool(value.api_key),
        "base_url": value.base_url,
        "model": value.model,
        "pdf_parser": value.pdf_parser,
        "mineru_api_key_configured": bool(value.mineru_api_key),
        "mineru_base_url": value.mineru_base_url,
        "mineru_model_version": value.mineru_model_version,
        "mineru_language": value.mineru_language,
        "mineru_enable_formula": value.mineru_enable_formula,
        "mineru_enable_table": value.mineru_enable_table,
        "mineru_is_ocr": value.mineru_is_ocr,
        "vision_model": value.vision_model,
        "theme": value.theme,
        "vision_enabled": value.vision_enabled,
        "vision_mode": value.vision_mode,
        "show_annotated_pdf": value.show_annotated_pdf,
        "translation_domain": value.translation_domain,
        "favorites": value.favorites,
    }
