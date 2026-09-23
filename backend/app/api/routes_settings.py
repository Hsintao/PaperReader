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
    show_annotated_pdf: bool | None = None
    translation_domain: str | None = None
    favorites: list[str] | None = None


class UpdateProviderSettingsRequest(BaseModel):
    api_key: str | None = None
    clear_api_key: bool = False
    base_url: str | None = None
    model: str | None = None
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
        show_annotated_pdf=payload.show_annotated_pdf,
        translation_domain=payload.translation_domain,
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
        vision_model=payload.vision_model,
    )
    return serialize_settings(values)
