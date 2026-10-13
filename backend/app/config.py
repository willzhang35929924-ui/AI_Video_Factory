from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, field_validator, model_validator
from app.runtime_paths import venv_python

BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent


class Settings(BaseSettings):
    app_mode: Literal["mock", "integration"] = "integration"
    app_name: str = "AI Video Factory"
    frontend_origin: str = "http://localhost:3000"
    database_url: str = "sqlite:///" + str(BACKEND_ROOT / "factory.db")
    storage_root: Path = PROJECT_ROOT / "storage"
    readonly_asset_roots: list[Path] = Field(default_factory=list)
    manual_import_max_bytes: int = Field(default=2 * 1024 ** 3, gt=0)
    integration_dry_run: bool = True
    collector_unattended_enabled: bool = True
    production_recovery_enabled: bool = True
    external_recovery_enabled: bool = True
    legacy_tiktok_auto_root: Path = PROJECT_ROOT.parent / "TikTok_Auto"
    legacy_tiktok_auto_python: Path | None = None
    legacy_collector_timeout_seconds: int = 900
    legacy_process_terminate_grace_seconds: int = 5
    legacy_video_auto_root: Path = PROJECT_ROOT.parent / "video_auto"
    legacy_video_auto_python: Path | None = None
    legacy_video_timeout_seconds: int = 7200
    matte_repair_timeout_seconds: int = 7200
    quality_python: Path = Field(default_factory=lambda: venv_python(PROJECT_ROOT.parent / "ComfyUI"))
    factory_comfy_cutout_workflow: Path = PROJECT_ROOT / "workflows/cutout_rgb_alpha_v2.api.json"
    factory_comfy_compose_workflow: Path = PROJECT_ROOT / "workflows/compose_person_v2.api.json"
    factory_comfy_chunk_frames: int = 8

    @model_validator(mode="after")
    def resolve_legacy_python(self):
        if self.legacy_tiktok_auto_python is None:
            self.legacy_tiktok_auto_python = venv_python(self.legacy_tiktok_auto_root)
        if self.legacy_video_auto_python is None:
            self.legacy_video_auto_python = venv_python(self.legacy_video_auto_root)
        if self.legacy_collector_timeout_seconds <= 0 or self.legacy_process_terminate_grace_seconds <= 0:
            raise ValueError("旧采集程序的 timeout 与退出等待必须大于零")
        if self.matte_repair_timeout_seconds <= 0:
            raise ValueError("Factory 局部修复 timeout 必须大于零")
        return self

    @field_validator("database_url")
    @classmethod
    def local_database_only(cls, value: str) -> str:
        if not value.startswith("sqlite:///"):
            raise ValueError("本地集成只允许 SQLite；PostgreSQL 模型与迁移保留，不连接真实数据库")
        return value

    model_config = SettingsConfigDict(
        env_file=(PROJECT_ROOT / ".env", BACKEND_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
