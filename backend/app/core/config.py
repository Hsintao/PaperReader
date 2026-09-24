import os
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _looks_like_path(value: str) -> bool:
    """Whether a configured executable names a file rather than a PATH entry."""
    return any(separator and separator in value for separator in (os.sep, os.altsep, "/"))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=os.environ.get("PAPERREADER_ENV_FILE")
        or str(Path(__file__).resolve().parents[3] / ".env"),
        case_sensitive=False,
        extra="ignore",
    )

    def model_post_init(self, __context) -> None:
        project_root = Path(__file__).resolve().parents[3]
        if not self.data_dir.is_absolute():
            self.data_dir = (project_root / self.data_dir).resolve()
        if not self.pdfmathtranslate_python:
            self.pdfmathtranslate_python = "python3"
        elif _looks_like_path(self.pdfmathtranslate_python):
            # A bare name is looked up on PATH, but a path is resolved against
            # the project root: the backend's own working directory differs
            # between `make backend` (backend/) and `make web` (also backend/,
            # after the script cd's), so a relative path would name a different
            # interpreter depending on how the server was started.
            interpreter = Path(self.pdfmathtranslate_python)
            if not interpreter.is_absolute():
                self.pdfmathtranslate_python = str(
                    (project_root / interpreter).resolve()
                )
        if not self.pdfmathtranslate_version:
            self.pdfmathtranslate_version = "2.9.0"
        mode = (self.pdfmathtranslate_output_mode or "").strip().lower()
        self.pdfmathtranslate_output_mode = mode if mode in {"mono", "dual"} else "mono"
        worker_dir = self.pdfmathtranslate_working_dir
        # ``Path("")`` is represented as ``.`` and is truthy. Leaving it
        # relative makes the backend write the job relative to its process,
        # while the child worker resolves it relative to the bundle cwd.
        if str(worker_dir) in {"", "."}:
            self.pdfmathtranslate_working_dir = self.data_dir / "worker"
        elif not worker_dir.is_absolute():
            self.pdfmathtranslate_working_dir = (project_root / worker_dir).resolve()

    app_env: str = Field(default="dev", alias="APP_ENV")
    desktop_mode: bool = Field(default=False, alias="PAPERREADER_DESKTOP")

    data_dir: Path = Field(default=Path("../data"), alias="DATA_DIR")
    upload_dir_name: str = Field(default="uploads", alias="UPLOAD_DIR_NAME")
    output_dir_name: str = Field(default="outputs", alias="OUTPUT_DIR_NAME")
    sqlite_db_name: str = Field(default="paperreader.db", alias="SQLITE_DB_NAME")

    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    openai_base_url: str = Field(default="https://api.deepseek.com", alias="OPENAI_BASE_URL")
    openai_model: str = Field(default="deepseek-flash", alias="OPENAI_MODEL")

    # PDFMathTranslate-next worker. The backend never imports the translator; it
    # starts it as a separate process whose interpreter and command are given
    # here, so the heavy dependency stays out of the backend's environment.
    pdfmathtranslate_python: str = Field(default="", alias="PDFMATHTRANSLATE_PYTHON")
    pdfmathtranslate_worker: str = Field(default="", alias="PDFMATHTRANSLATE_WORKER")
    pdfmathtranslate_version: str = Field(default="", alias="PDFMATHTRANSLATE_VERSION")
    pdfmathtranslate_timeout: float = Field(
        default=3600.0, alias="PDFMATHTRANSLATE_TIMEOUT"
    )
    pdfmathtranslate_working_dir: Path = Field(
        default=Path(""), alias="PDFMATHTRANSLATE_WORKING_DIR"
    )
    # Keep the translator's own layout output under
    # outputs/<document>/extraction/debug. It is what the manifest is converted
    # from and runs to hundreds of megabytes per paper, so it is off by default.
    pdfmathtranslate_debug: bool = Field(default=False, alias="PDFMATHTRANSLATE_DEBUG")
    pdfmathtranslate_output_mode: str = Field(
        default="mono", alias="PDFMATHTRANSLATE_OUTPUT_MODE"
    )
    # Paragraphs translated concurrently; the worker's thread pool is sized by
    # this. The translator is the only slow stage of a run and its wall time
    # scales with it. Measured per-request latency is ~7s, so 4 workers left
    # most of the budget idle; 12 keeps the pipeline full without nearing any
    # provider's rate limit.
    pdfmathtranslate_qps: int = Field(default=12, alias="PDFMATHTRANSLATE_QPS")

    redis_url: str = Field(default="redis://localhost:6379/0", alias="REDIS_URL")

    # How often the per-domain terminology glossary folds newly learned terms
    # into the stored glossary. Applied by a background thread; the settings
    # API also catches up on a missed interval after a restart.
    glossary_refresh_interval_minutes: int = Field(
        default=30, alias="GLOSSARY_REFRESH_INTERVAL_MINUTES"
    )

    cors_origins_raw: str = Field(default="http://localhost:5173", alias="CORS_ORIGINS")

    @property
    def cors_origins(self) -> list[str]:
        return [item.strip() for item in self.cors_origins_raw.split(",") if item.strip()]

    @property
    def upload_dir(self) -> Path:
        return self.data_dir / self.upload_dir_name

    @property
    def output_dir(self) -> Path:
        return self.data_dir / self.output_dir_name


settings = Settings()

settings.upload_dir.mkdir(parents=True, exist_ok=True)
settings.output_dir.mkdir(parents=True, exist_ok=True)
