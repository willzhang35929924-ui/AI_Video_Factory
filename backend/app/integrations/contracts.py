from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.integrations.production_rules import factory_rule_snapshot, verified_factory_rules
from app.integrations.collection_identity import identity_for


class FactoryCollectorRules(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min_views: int = Field(ge=0, strict=True)
    min_likes: int = Field(ge=0, strict=True)
    product_match_min_confidence: float = Field(default=.85, ge=0, le=1, strict=True)
    overlay_requirement: Literal["any", "present", "absent"] = "any"
    watermark_requirement: Literal["any", "present", "absent"] = "any"
    subtitle_requirement: Literal["any", "present", "absent"] = "any"

    def snapshot(self) -> dict:
        return factory_rule_snapshot(self.model_dump())


class CollectorInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    country: str = Field(min_length=1, max_length=100)
    keyword: str = Field(min_length=1, max_length=500)
    product_images: list[str] = Field(min_length=1, max_length=6)
    product_name: str | None = None
    product_id: str | None = None
    project_id: str | None = None
    source_document: str = "manual"
    source_row: int = Field(default=0, ge=0)
    context_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{16,64}$")
    factory_rules: FactoryCollectorRules | None = None
    video_id: str | None = Field(default=None, pattern=r"^[0-9]{1,30}$")
    video_url: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def source_identity(self):
        if self.video_url:
            parsed = urlsplit(self.video_url)
            host = parsed.hostname or ""
            if (parsed.scheme != "https" or host != "tiktok.com" and not host.endswith(".tiktok.com")
                    or parsed.username or parsed.password or parsed.port not in {None, 443}
                    or any(ord(char) < 33 for char in self.video_url)):
                raise ValueError("仅允许无凭据的 HTTPS TikTok 视频链接")
        if self.video_id or self.video_url:
            identity_for(self.video_id, self.video_url)
        return self


class TaskContext(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str
    project_id: str
    country: str
    keyword: str
    product_id: str
    product_images: list[str]
    source_document: str
    source_row: int
    context_id: str
    video_id: str | None = None
    video_url: str | None = None
    factory_rules: dict[str, Any] | None = None

    @model_validator(mode="after")
    def immutable_rules(self):
        if self.factory_rules is not None:
            verified_factory_rules(self.factory_rules)
        return self


class ObservationProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["fresh", "cached", "unknown"]
    observed_at: str | None = None
    reused_at: str | None = None
    original_checkpoint: dict[str, Any] = Field(default_factory=dict)


class CollectorResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str
    project_id: str
    country: str
    keyword: str
    product_id: str
    product_images: list[str]
    source_document: str
    source_row: int
    context_id: str
    factory_rules: dict[str, Any] | None = None
    video_id: str | None = None
    video_url: str | None = None
    views: int | None = Field(default=None, ge=0)
    likes: int | None = Field(default=None, ge=0)
    like_view_ratio: float | None = None
    metric_source: str | None = None
    metric_evidence: Any = None
    metrics_provenance: ObservationProvenance | None = None
    reference_provenance: ObservationProvenance | None = None
    semantic_provenance: ObservationProvenance | None = None
    reference_product_match: str = "uncertain"
    product_match: str = "uncertain"
    product_match_confidence: float | None = None
    product_match_reason: str = ""
    ai_origin: str = "uncertain"
    cutout_suitability: str = "uncertain"
    overlay_detected: bool | None = None
    watermark_detected: bool | None = None
    subtitle_detected: bool | None = None
    decision: Literal["accepted", "manual_review", "rejected"]
    decision_reason: str
    status: str = ""
    legacy_csv_path: str
    legacy_result_reference: dict[str, Any]
    legacy_vidbee_task_id: str | None = None
    download_status: str = "not_started"
    source_asset_metadata: dict[str, Any] | None = None
    error_code: str | None = None
    error_message: str | None = None
    retryable: bool = False

    @model_validator(mode="after")
    def backend_ratio(self):
        self.like_view_ratio = (self.likes / self.views * 100 if self.views else 0) if self.likes is not None and self.views is not None else None
        return self
