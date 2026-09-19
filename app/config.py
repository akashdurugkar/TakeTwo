from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT_DIR / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    cu_endpoint: str = ""
    cu_key: str = ""
    cu_api_version: str = "2025-11-01"
    cu_analysis_timeout_seconds: int = Field(7200, ge=60, le=7200)
    cu_analyzer_id: str = "MakeItReelAnalyzer"
    cu_base_analyzer_id: str = "prebuilt-video"
    cu_fallback_base_analyzer_id: str = ""
    cu_prebuilt_analyzer_id: str = "prebuilt-videoSearch"

    aoai_endpoint: str = ""
    aoai_key: str = ""
    aoai_deployment: str = "gpt-4.1"
    aoai_api_version: str = "2024-10-21"

    web_search_enabled: bool = True
    web_search_max_calls: int = Field(3, ge=0, le=10)
    web_search_timeout_seconds: float = Field(45, ge=1, le=120)

    max_upload_mb: int = 200
    blob_account_name: str = Field("", pattern=r"^([a-z0-9]{3,24})?$")
    blob_container: str = Field("taketwo-uploads", pattern=r"^[a-z0-9](?:[a-z0-9-]{1,61}[a-z0-9])$")
    blob_tenant_id: str = ""
    blob_use_managed_identity: bool = False
    max_blob_upload_bytes: int = Field(4_000_000_000, ge=1, le=4_000_000_000)

    @property
    def cu_configured(self) -> bool:
        return bool(self.cu_endpoint and self.cu_key)

    @property
    def aoai_configured(self) -> bool:
        return bool(self.aoai_endpoint and self.aoai_key and self.aoai_deployment)

    @property
    def blob_configured(self) -> bool:
        return bool(self.blob_account_name)

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    return Settings()
