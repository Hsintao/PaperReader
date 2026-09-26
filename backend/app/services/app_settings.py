"""Application settings for a single local operator.

Everything the pipeline needs — the translation endpoint, reading
preferences — lives in one JSON file under the data directory.
The file is created with owner-only permissions, so the API key stays as
private as the rest of the local data.
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
_PROVIDERS = {"deepseek", "siliconflow", "custom"}

# Fields removed when SoMark / MinerU parsing was replaced by the
# PDFMathTranslate-next worker, and the vision-check settings retired with
# that feature. Dropped from settings.json on startup so the stored file
# never advertises an option that no longer exists.
REMOVED_KEYS = (
    "pdf_parser",
    "somark_api_key",
    "somark_base_url",
    "mineru_api_key",
    "mineru_base_url",
    "mineru_model_version",
    "mineru_language",
    "mineru_enable_formula",
    "mineru_enable_table",
    "mineru_is_ocr",
    "vision_model",
    "vision_enabled",
    "vision_mode",
)


def settings_path() -> Path:
    return settings.data_dir / "settings.json"


@dataclass
class AppSettings:
    api_key: str = ""
    provider: str = "custom"
    base_url: str = ""
    model: str = ""
    theme: str = "light"
    show_annotated_pdf: bool = False
    translation_domain: str = "general"
    favorites: list[str] = field(default_factory=list)


def _defaults() -> AppSettings:
    return AppSettings(
        base_url=settings.openai_base_url,
        model=settings.openai_model,
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


def purge_removed_keys() -> bool:
    """Rewrite settings.json without the retired parser fields.

    Returns whether the stored file changed.
    """
    with _LOCK:
        path = settings_path()
        raw = _read_raw(path)
        if not any(key in raw for key in REMOVED_KEYS):
            return False
        _write(_coerce(raw))
        return True


def update_settings(
    *,
    api_key: str | None = None,
    clear_api_key: bool = False,
    provider: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    theme: str | None = None,
    show_annotated_pdf: bool | None = None,
    translation_domain: str | None = None,
    favorites: list[str] | None = None,
) -> AppSettings:
    current = load_settings()
    if clear_api_key:
        current.api_key = ""
    elif (api_key or "").strip():
        current.api_key = api_key.strip()
    if provider is not None:
        current.provider = provider if provider in _PROVIDERS else "custom"
    if base_url is not None:
        current.base_url = base_url.strip()
    if model is not None:
        current.model = model.strip()
    if theme is not None:
        current.theme = theme
    if show_annotated_pdf is not None:
        current.show_annotated_pdf = bool(show_annotated_pdf)
    if translation_domain is not None:
        current.translation_domain = translation_domain
    if favorites is not None:
        current.favorites = list(favorites)

    if current.theme not in _THEMES:
        current.theme = "light"
    current.translation_domain = normalize_domain(current.translation_domain)
    if not current.base_url:
        current.base_url = settings.openai_base_url
    if not current.model:
        current.model = settings.openai_model
    if not current.base_url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="LLM Base URL must start with http:// or https://")

    _write(current)
    return current


def require_provider_settings() -> AppSettings:
    provider = load_settings()
    if not provider.api_key:
        raise HTTPException(
            status_code=409,
            detail={"code": "config_required", "message": "请先在设置中配置大模型 API Key。"},
        )
    return provider


def serialize_settings(value: AppSettings) -> dict:
    """Never expose the stored key; report only whether it is present."""
    return {
        "api_key_configured": bool(value.api_key),
        "provider": value.provider,
        "base_url": value.base_url,
        "model": value.model,
        "theme": value.theme,
        "show_annotated_pdf": value.show_annotated_pdf,
        "translation_domain": value.translation_domain,
        "favorites": value.favorites,
    }
