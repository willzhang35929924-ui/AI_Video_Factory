from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

StageName = Literal["collector", "analysis", "download", "cutout", "person", "prompt", "background", "compose", "quality", "review", "final", "publish"]
TaskStatus = Literal["pending", "running", "waiting", "review", "completed", "failed", "CREATED", "COLLECTING", "COLLECTED", "WAITING_REVIEW", "FAILED", "QUEUED", "RUNNING", "PAUSED", "CANCELLED", "SKIPPED", "COMPLETED", "REJECTED", "QUARANTINED"]
StageStatus = Literal["pending", "running", "success", "failed", "waiting_review", "skipped", "not_connected"]
BackgroundKind = Literal["chatgpt_rpa", "manual", "local_comfyui", "other"]
STAGES = ("collector", "analysis", "cutout", "person", "background", "compose", "review", "publish")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProjectView(StrictModel):
    id: str
    name: str


class TaskView(StrictModel):
    id: str
    project_id: str
    country: str
    keyword: str
    product_id: str
    source_video_id: str | None
    current_stage: StageName
    status: TaskStatus
    progress: float = Field(ge=0, le=100)
    thumbnail: str
    created_at: datetime
    updated_at: datetime
    execution_mode: Literal["mock", "integration"] = "mock"
    context: dict[str, Any] = Field(default_factory=dict)
    collector_result: dict[str, Any] = Field(default_factory=dict)


class CollectionIdentityView(StrictModel):
    id: str
    platform: Literal["tiktok"]
    native_video_id: str
    canonical_task_id: str
    canonical_url: str
    confidence: Literal["NATIVE_ID"]
    first_seen_at: datetime


class StageView(StrictModel):
    id: str
    task_id: str
    name: StageName
    status: StageStatus
    start_time: datetime | None
    end_time: datetime | None
    duration: float = Field(ge=0)
    retry_count: int = Field(ge=0)
    error: str | None


class ProductView(StrictModel):
    id: str
    name: str
    country: str
    image: str
    url: str
    binding_reference: str
    description: str
    status: str
    keywords: list[str]
    platform_product_id: str | None = None
    descriptions: list[dict[str, Any]] = Field(default_factory=list)
    description_selection: dict[str, Any] = Field(default_factory=dict)
    publishing_context: dict[str, Any] = Field(default_factory=dict)


class AssetView(StrictModel):
    id: str
    task_id: str
    type: Literal["source", "source_candidate", "person", "background", "compose", "final", "exception"]
    name: str
    thumbnail: str
    duration: float = Field(ge=0)
    width: int = Field(ge=0)
    height: int = Field(ge=0)
    fps: float = Field(ge=0)
    codec: str
    country: str
    keyword: str
    status: str
    storage_path: str
    created_at: datetime
    asset_type: Literal["SOURCE_VIDEO", "SOURCE_CANDIDATE", "PERSON_VIDEO", "ALPHA_VIDEO", "BACKGROUND_IMAGE", "COMPOSITION_VIDEO", "FINAL_VIDEO"] | None = None
    external_path: str | None = None
    sha256: str | None = None
    size: int | None = Field(default=None, ge=0)
    mime_type: str | None = None
    source_system: str | None = None
    source_url: str | None = None
    legacy_video_id: str | None = None
    legacy_context_id: str | None = None
    registry_status: Literal["mock", "registered", "missing", "unverified"] = "mock"
    legacy_reference: dict[str, Any] = Field(default_factory=dict)
    probe_metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def known_dimensions_for_media(self):
        if self.type != "source_candidate" and (self.width == 0 or self.height == 0):
            raise ValueError("仅未下载的 source_candidate 可以使用未知的零尺寸")
        return self


class CollectorView(StrictModel):
    id: str
    country: str
    keyword: str
    product_id: str
    product_image: str
    min_views: int = Field(ge=0)
    min_likes: int = Field(ge=0)
    min_ratio: float = Field(ge=0, le=100)
    match_threshold: float = Field(ge=0, le=1)
    filter_overlay: bool
    filter_watermark: bool
    filter_subtitle: bool
    views: int = Field(ge=0)
    likes: int = Field(ge=0)
    status: str
    input_source: Literal["manual", "tiktok_url", "tencent_docs", "excel"] = "manual"
    video_url: str | None = None
    raw_keyword: str | None = None
    product_images: list[str] = Field(default_factory=list)
    context_id: str | None = None
    source_document: str | None = None
    source_row: int | None = Field(default=None, ge=1)
    last_task_id: str | None = None
    last_run_id: str | None = None
    result: dict[str, Any] = Field(default_factory=dict)
    like_view_ratio_percent: float = 0

    @model_validator(mode="after")
    def calculate_ratio(self):
        self.like_view_ratio_percent = self.likes / self.views * 100 if self.views else 0
        return self


class AccountView(StrictModel):
    id: str
    name: str
    country: str
    status: Literal["authorized", "expired", "pending", "error", "CONNECTED", "NOT_CONNECTED", "EXPIRED", "WAITING_MANUAL", "DISABLED"]
    authorization: str
    last_publish: datetime | None
    queue: int = Field(ge=0)
    errors: list[str]
    product_ids: list[str]
    hubstudio_env_id: str | None = None
    cdp_endpoint: str | None = None
    expected_username: str | None = None
    identity_verification: dict[str, Any] = Field(default_factory=dict)
    vpn_verification: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    cooldown_seconds: int = Field(default=120, ge=0)
    publishing_context: dict[str, Any] = Field(default_factory=dict)


class PublishingView(StrictModel):
    id: str
    task_id: str
    video_id: str
    account_id: str
    country: str
    product_id: str
    caption: str
    product_binding: str
    schedule: datetime | None
    status: str
    error: str | None
    batch_id: str | None = None
    selected_description: dict[str, Any] | None = None
    input_sha256: str | None = None
    current_step: str = "PREFLIGHT"
    checkpoint: dict[str, Any] = Field(default_factory=dict)
    submission_state: str = "NOT_SUBMITTED"
    platform_video_id: str | None = None
    platform_url: str | None = None
    moderation_status: str = "NOT_CHECKED"
    product_link_status: str = "NOT_CHECKED"
    created_at: datetime | None = None
    updated_at: datetime | None = None


class PromptView(StrictModel):
    id: str
    task_id: str
    prompt: str
    negative_prompt: str
    provider: BackgroundKind
    scene_type: str
    style: str
    lighting: str
    color: str
    aspect_ratio: str
    created_at: datetime | None = None
    ordinal: int | None = None
    status: str = "MOCK"
    processing_run_id: str | None = None
    fingerprint: str | None = None


class ProductionBatchView(StrictModel):
    id: str
    status: str
    task_ids: list[str]
    items: list[dict[str, Any]]
    active_task_id: str | None = None
    summary: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class QualityAssessmentView(StrictModel):
    id: str
    task_id: str
    asset_id: str
    processing_run_id: str
    status: str
    quality_score: float | None = None
    metrics: dict[str, Any]
    issue_segments: list[dict[str, Any]]
    fingerprint: str
    created_at: datetime


class ReviewView(StrictModel):
    id: str
    task_id: str
    asset_id: str
    status: str
    issues: list[str]
    note: str


class RunView(StrictModel):
    id: str
    task_id: str
    stage: StageName
    status: Literal["queued", "running", "success", "failed"]
    progress: float = Field(ge=0, le=100)
    logs: list[str]
    created_at: datetime
    run_id: str | None = None
    provider: str = "mock"
    command: list[str] = Field(default_factory=list)
    cwd: str | None = None
    pid: int | None = Field(default=None, ge=1)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    exit_code: int | None = None
    stdout_log_path: str | None = None
    stderr_log_path: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    retry_count: int = Field(default=0, ge=0)
    idempotency_key: str | None = None
    last_heartbeat: datetime | None = None
    termination_signal: int | None = None
    timed_out: bool = False
    resource_info: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def same_run_identity(self):
        if self.run_id is not None and self.run_id != self.id:
            raise ValueError("run_id 必须与 id 相同")
        self.run_id = self.id
        return self


class LegacyBindingView(StrictModel):
    id: str
    task_id: str
    project_id: str
    product_id: str
    source_system: str = "TikTok_Auto"
    context_id: str | None = None
    source_document: str | None = None
    source_row: int | None = None
    video_id: str | None = None
    download_task_id: str | None = None
    source_path: str | None = None
    sha256: str | None = None
    legacy_reference: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    legacy_project: str | None = None
    legacy_context_id: str | None = None
    legacy_video_id: str | None = None
    legacy_download_task_id: str | None = None

    @model_validator(mode="after")
    def explicit_legacy_names(self):
        for alias, name in (("legacy_project", "source_system"), ("legacy_context_id", "context_id"), ("legacy_video_id", "video_id"), ("legacy_download_task_id", "download_task_id")):
            value = getattr(self, alias)
            original = getattr(self, name)
            if value is not None and original is not None and value != original:
                raise ValueError(alias + " 与 " + name + " 必须一致")
            setattr(self, name, original if original is not None else value)
            setattr(self, alias, getattr(self, name))
        return self


class ErrorView(StrictModel):
    id: str
    task_id: str
    stage: str
    code: str
    message: str
    created_at: datetime
    provider: str = "mock"
    error_code: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    retryable: bool = False

    @model_validator(mode="after")
    def same_error_code(self):
        if self.error_code is not None and self.error_code != self.code:
            raise ValueError("error_code 必须与 code 相同")
        self.error_code = self.code
        return self


class AuditView(StrictModel):
    id: str
    task_id: str | None
    action: str
    entity_id: str
    message: str
    created_at: datetime


class SettingsView(StrictModel):
    theme: Literal["light", "dark", "system"]
    language: Literal["zh-CN"]
    output_width: int = Field(gt=0)
    output_height: int = Field(gt=0)
    output_fps: float = Field(gt=0)
    default_provider: BackgroundKind
    mock_delay_ms: int = Field(ge=100, le=10000)
    review_required: bool
    production_policy: dict[str, Any] = Field(default_factory=dict)


class FactoryState(StrictModel):
    projects: list[ProjectView]
    tasks: list[TaskView]
    stages: list[StageView]
    products: list[ProductView]
    assets: list[AssetView]
    collector: list[CollectorView]
    accounts: list[AccountView]
    publishing: list[PublishingView]
    prompts: list[PromptView]
    reviews: list[ReviewView]
    runs: list[RunView]
    errors: list[ErrorView]
    audits: list[AuditView]
    settings: SettingsView
    legacy_bindings: list[LegacyBindingView] = Field(default_factory=list)
    batches: list[ProductionBatchView] = Field(default_factory=list)
    quality_assessments: list[QualityAssessmentView] = Field(default_factory=list)
    collection_identities: list[CollectionIdentityView] = Field(default_factory=list)


class ActionRequest(StrictModel):
    action: str = Field(min_length=1, max_length=100)
    entity_id: str | None = Field(default=None, max_length=100)
    payload: dict[str, Any] = Field(default_factory=dict)


class SourceMetadata(StrictModel):
    original_filename: str | None = None
    fps: float | None = None
    width: int | None = None
    height: int | None = None
    duration: float | None = None
    codec: str | None = None
    source_url: str | None = None
    external_path: str | None = None
    sha256: str | None = None
    size: int | None = None
    mime_type: str | None = None
    source_system: str | None = None
    legacy_video_id: str | None = None
    legacy_context_id: str | None = None
    download_task_id: str | None = None
    r_frame_rate: str | None = None
    avg_frame_rate: str | None = None
    legacy_reference: dict[str, Any] = Field(default_factory=dict)
    probe_metadata: dict[str, Any] = Field(default_factory=dict)


class CutoutMetadata(StrictModel):
    model: str | None = None
    model_version: str | None = None
    workflow_version: str | None = None
    created_at: datetime | None = None


class BackgroundMetadata(StrictModel):
    provider: BackgroundKind | None = None
    prompt: str | None = None
    negative_prompt: str | None = None
    image_filename: str | None = None


class CompositionMetadata(StrictModel):
    fps: float | None = None
    width: int | None = None
    height: int | None = None
    codec: str | None = None


class QualityMetadata(StrictModel):
    ai_match_score: float | None = Field(default=None, ge=0, le=1)
    product_match_score: float | None = Field(default=None, ge=0, le=1)
    overlay_detected: bool | None = None
    watermark_detected: bool | None = None
    mask_flicker_score: float | None = Field(default=None, ge=0, le=1)
    missing_body_score: float | None = Field(default=None, ge=0, le=1)


class PublishingMetadata(StrictModel):
    account_id: str | None = None
    caption: str | None = None
    product_binding: str | None = None
    status: str | None = None


class VideoMetadata(StrictModel):
    task_id: str
    country: str
    keyword: str
    product_id: str
    source_video: SourceMetadata
    cutout: CutoutMetadata
    background: BackgroundMetadata
    composition: CompositionMetadata
    quality: QualityMetadata
    publishing: PublishingMetadata


class WorkflowView(StrictModel):
    id: str
    task_id: str
    version: str
    workflow: dict[str, Any]
    created_at: datetime
    processing_run_id: str | None = None
    prompt_id: str | None = None
    sha256: str | None = None
    execution: dict[str, Any] = Field(default_factory=dict)


class TaskDetail(StrictModel):
    task: TaskView
    stages: list[StageView]
    assets: list[AssetView]
    prompts: list[PromptView]
    runs: list[RunView]
    errors: list[ErrorView]
    audits: list[AuditView]
    reviews: list[ReviewView] = Field(default_factory=list)
    quality_assessments: list[QualityAssessmentView] = Field(default_factory=list)
    workflows: list[WorkflowView] = Field(default_factory=list)
    batch: ProductionBatchView | None = None
    lineage: list[dict[str, Any]] = Field(default_factory=list)


class AccountDetail(StrictModel):
    account: AccountView
    authorization: dict[str, str | None]
    history: list[PublishingView]
    products: list[ProductView]
    queue: list[PublishingView]
    errors: list[str]


class ProductionEvent(StrictModel):
    type: str = Field(min_length=1)
    task_id: str | None = None
    run_id: str | None = None
    stage: StageName | None = None
    progress: float | None = None
    status: str | None = None
    message: str | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now().astimezone())
    data: dict[str, Any] | None = None
