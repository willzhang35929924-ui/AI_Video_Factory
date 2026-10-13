from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ProductionContext(BaseModel):
    model_config = ConfigDict(extra="ignore")
    task_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    country: str = Field(min_length=1)
    keyword: str = Field(min_length=1)
    product_id: str = Field(min_length=1)
    context_id: str = Field(min_length=1)
    product_images: list[str] = Field(min_length=1)
    source_document: str
    source_row: int = Field(ge=0)
    video_id: str = Field(min_length=1)
    source_video: str = Field(min_length=1)
    run_id: str | None = None
    job: dict[str, Any] | None = None
    person_asset: dict[str, Any] | None = None
    background_asset: dict[str, Any] | None = None
    composition_asset: dict[str, Any] | None = None
    cutout_generation: int = Field(default=1, ge=1)
    object_strategy: dict[str, Any] = Field(default_factory=dict)
    execution_scope: Literal["PRODUCTION", "TECHNICAL_VALIDATION"] = "PRODUCTION"
    external_budget_required: bool = False
    external_budget_root: str | None = None


class ProductionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str | None = None
    context_id: str | None = None
    stage: str
    provider: str
    mode: Literal["REAL"] = "REAL"
    status: Literal["READY", "FAILED", "WAITING_REVIEW", "WAITING_MANUAL", "NOT_CONNECTED", "CANCELLED"]
    error_code: str | None = None
    error_message: str | None = None
    retryable: bool = False
    prompts: list[dict[str, Any]] = Field(default_factory=list)
    assets: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class PersonAssetEvidence(BaseModel):
    """Independent RGB + alpha outputs required before a PERSON_VIDEO can be registered."""
    model_config = ConfigDict(extra="forbid")
    task_id: str
    version: int = Field(ge=1)
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    rgb_path: str
    rgb_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    alpha_path: str
    alpha_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    fps: float = Field(gt=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    duration: float = Field(gt=0)
    frame_count: int = Field(gt=0)
    workflow_snapshot_id: str
    processing_run_id: str
