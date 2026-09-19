from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from app.services.app_settings import load_settings, serialize_settings, update_settings


router = APIRouter()


class UpdateSettingsRequest(BaseModel):
    # Kept for clients from PaperReader 2.0; new clients use /providers.
    api_key: str | None = None
    base_url: str | None = None
    model: str | None = None
    theme: str | None = None
    vision_enabled: bool | None = None
    vision_mode: str | None = None
    favorites: list[str] | None = None


class UpdateProviderSettingsRequest(BaseModel):
    api_key: str | None = None
    clear_api_key: bool = False
    base_url: str | None = None
    model: str | None = None
    pdf_parser: str | None = None
    mineru_api_key: str | None = None
    clear_mineru_api_key: bool = False
    mineru_base_url: str | None = None
    mineru_model_version: str | None = None
    mineru_language: str | None = None
    mineru_enable_formula: bool | None = None
    mineru_enable_table: bool | None = None
    mineru_is_ocr: bool | None = None
    vision_model: str | None = None


@router.get("/settings/me")
def get_settings() -> dict:
    return serialize_settings(load_settings())


@router.put("/settings/me")
def put_settings(payload: UpdateSettingsRequest) -> dict:
    values = update_settings(
        api_key=payload.api_key,
        base_url=payload.base_url,
        model=payload.model,
        theme=payload.theme,
        vision_enabled=payload.vision_enabled,
        vision_mode=payload.vision_mode,
        favorites=payload.favorites,
    )
    return serialize_settings(values)


@router.put("/settings/me/providers")
def put_provider_settings(payload: UpdateProviderSettingsRequest) -> dict:
    values = update_settings(
        api_key=payload.api_key,
        clear_api_key=payload.clear_api_key,
        base_url=payload.base_url,
        model=payload.model,
        pdf_parser=payload.pdf_parser,
        mineru_api_key=payload.mineru_api_key,
        clear_mineru_api_key=payload.clear_mineru_api_key,
        mineru_base_url=payload.mineru_base_url,
        mineru_model_version=payload.mineru_model_version,
        mineru_language=payload.mineru_language,
        mineru_enable_formula=payload.mineru_enable_formula,
        mineru_enable_table=payload.mineru_enable_table,
        mineru_is_ocr=payload.mineru_is_ocr,
        vision_model=payload.vision_model,
    )
    return serialize_settings(values)
