from datetime import datetime, timezone

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint, false, true
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Entity:
    id: Mapped[str] = mapped_column(String(100), primary_key=True)


class User(Entity, Base):
    __tablename__ = "users"
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class Project(Entity, Base):
    __tablename__ = "projects"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(String(200))


class Country(Entity, Base):
    __tablename__ = "countries"
    name: Mapped[str] = mapped_column(String(100), unique=True)


class Product(Entity, Base):
    __tablename__ = "products"
    country_id: Mapped[str] = mapped_column(ForeignKey("countries.id"))
    name: Mapped[str] = mapped_column(String(200))
    country: Mapped[str] = mapped_column(String(100))
    image: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text)
    binding_reference: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30))
    keywords: Mapped[list] = mapped_column(JSON)
    platform_product_id: Mapped[str | None] = mapped_column(String(200))
    descriptions: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")
    description_selection: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    publishing_context: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")


class DescriptionGeneration(Entity, Base):
    __tablename__ = "description_generations"
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"), index=True)
    country: Mapped[str] = mapped_column(String(100))
    keyword: Mapped[str] = mapped_column(String(300))
    model: Mapped[str] = mapped_column(String(200))
    prompt_version: Mapped[str] = mapped_column(String(100))
    input_fingerprint: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(String(40))
    request: Mapped[dict] = mapped_column(JSON)
    descriptions: Mapped[list] = mapped_column(JSON, default=list)
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Keyword(Entity, Base):
    __tablename__ = "keywords"
    country_id: Mapped[str] = mapped_column(ForeignKey("countries.id"))
    product_id: Mapped[str | None] = mapped_column(ForeignKey("products.id"))
    text: Mapped[str] = mapped_column(String(300))


class Task(Entity, Base):
    __tablename__ = "tasks"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"))
    country_id: Mapped[str] = mapped_column(ForeignKey("countries.id"))
    keyword_id: Mapped[str] = mapped_column(ForeignKey("keywords.id"))
    country: Mapped[str] = mapped_column(String(100))
    keyword: Mapped[str] = mapped_column(String(300))
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"))
    source_video_id: Mapped[str | None] = mapped_column(ForeignKey("source_videos.id", name="fk_task_source", use_alter=True))
    current_stage: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(30))
    progress: Mapped[float] = mapped_column(Float)
    thumbnail: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    execution_mode: Mapped[str] = mapped_column(String(30), default="mock", server_default="mock")
    context: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    collector_result: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")


class CollectionIdentity(Entity, Base):
    __tablename__ = "collection_identities"
    __table_args__ = (UniqueConstraint("platform", "native_video_id", name="uq_collection_platform_video"),)
    platform: Mapped[str] = mapped_column(String(30))
    native_video_id: Mapped[str] = mapped_column(String(100))
    canonical_task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    canonical_url: Mapped[str] = mapped_column(Text)
    confidence: Mapped[str] = mapped_column(String(30))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class TaskStage(Entity, Base):
    __tablename__ = "task_stages"
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    name: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(30))
    start_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration: Mapped[float] = mapped_column(Float)
    retry_count: Mapped[int] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)


class SourceVideo(Entity, Base):
    __tablename__ = "source_videos"
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    original_filename: Mapped[str] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(Text)
    fps: Mapped[float] = mapped_column(Float)
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    duration: Mapped[float] = mapped_column(Float)
    codec: Mapped[str] = mapped_column(String(50))
    views: Mapped[int] = mapped_column(Integer, default=0)
    likes: Mapped[int] = mapped_column(Integer, default=0)
    ai_match_score: Mapped[float | None] = mapped_column(Float)
    product_match_score: Mapped[float | None] = mapped_column(Float)
    overlay_detected: Mapped[bool | None] = mapped_column(Boolean)
    watermark_detected: Mapped[bool | None] = mapped_column(Boolean)
    external_path: Mapped[str | None] = mapped_column(Text)
    sha256: Mapped[str | None] = mapped_column(String(64))
    size: Mapped[int | None] = mapped_column(BigInteger)
    mime_type: Mapped[str | None] = mapped_column(String(100))
    source_system: Mapped[str | None] = mapped_column(String(100))
    legacy_video_id: Mapped[str | None] = mapped_column(String(100))
    legacy_context_id: Mapped[str | None] = mapped_column(String(100))
    download_task_id: Mapped[str | None] = mapped_column(String(100))
    r_frame_rate: Mapped[str | None] = mapped_column(String(50))
    avg_frame_rate: Mapped[str | None] = mapped_column(String(50))
    legacy_reference: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    probe_metadata: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")


class AssetIdentity(Entity, Base):
    __tablename__ = "asset_identities"
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deletion_snapshot: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")


class VideoAsset(Entity, Base):
    __tablename__ = "video_assets"
    id: Mapped[str] = mapped_column(ForeignKey("asset_identities.id"), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    type: Mapped[str] = mapped_column(String(30))
    name: Mapped[str] = mapped_column(Text)
    thumbnail: Mapped[str] = mapped_column(Text)
    duration: Mapped[float] = mapped_column(Float)
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    fps: Mapped[float] = mapped_column(Float)
    codec: Mapped[str] = mapped_column(String(50))
    country: Mapped[str] = mapped_column(String(100))
    keyword: Mapped[str] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(30))
    storage_path: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    asset_type: Mapped[str | None] = mapped_column(String(50))
    external_path: Mapped[str | None] = mapped_column(Text)
    sha256: Mapped[str | None] = mapped_column(String(64))
    size: Mapped[int | None] = mapped_column(BigInteger)
    mime_type: Mapped[str | None] = mapped_column(String(100))
    source_system: Mapped[str | None] = mapped_column(String(100))
    source_url: Mapped[str | None] = mapped_column(Text)
    legacy_video_id: Mapped[str | None] = mapped_column(String(100))
    legacy_context_id: Mapped[str | None] = mapped_column(String(100))
    registry_status: Mapped[str] = mapped_column(String(30), default="mock", server_default="mock")
    legacy_reference: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    probe_metadata: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")


class WorkflowSnapshot(Entity, Base):
    __tablename__ = "workflow_snapshots"
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    version: Mapped[str] = mapped_column(String(80))
    workflow: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    processing_run_id: Mapped[str | None] = mapped_column(String(100))
    prompt_id: Mapped[str | None] = mapped_column(String(100))
    sha256: Mapped[str | None] = mapped_column(String(64))
    execution: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")


class CutoutAsset(Entity, Base):
    __tablename__ = "cutout_assets"
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    asset_id: Mapped[str] = mapped_column(ForeignKey("asset_identities.id"))
    workflow_id: Mapped[str] = mapped_column(ForeignKey("workflow_snapshots.id"))
    model: Mapped[str] = mapped_column(String(80))
    model_version: Mapped[str] = mapped_column(String(80))
    alpha_quality: Mapped[float] = mapped_column(Float)
    temporal_stability: Mapped[float] = mapped_column(Float)
    missing_body_score: Mapped[float] = mapped_column(Float)
    flicker_score: Mapped[float] = mapped_column(Float)
    intervals: Mapped[list] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class Prompt(Entity, Base):
    __tablename__ = "prompts"
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    prompt: Mapped[str] = mapped_column(Text)
    negative_prompt: Mapped[str] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(String(80))
    scene_type: Mapped[str] = mapped_column(String(100))
    style: Mapped[str] = mapped_column(String(100))
    lighting: Mapped[str] = mapped_column(String(100))
    color: Mapped[str] = mapped_column(String(100))
    aspect_ratio: Mapped[str] = mapped_column(String(30))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    ordinal: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(30), default="MOCK", server_default="MOCK")
    processing_run_id: Mapped[str | None] = mapped_column(String(100))
    fingerprint: Mapped[str | None] = mapped_column(String(64))


class ProductionBatch(Entity, Base):
    __tablename__ = "production_batches"
    status: Mapped[str] = mapped_column(String(40))
    task_ids: Mapped[list] = mapped_column(JSON)
    items: Mapped[list] = mapped_column(JSON)
    active_task_id: Mapped[str | None] = mapped_column(String(100))
    summary: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class QualityAssessment(Entity, Base):
    __tablename__ = "quality_assessments"
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    asset_id: Mapped[str] = mapped_column(ForeignKey("asset_identities.id"))
    processing_run_id: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(30))
    quality_score: Mapped[float | None] = mapped_column(Float)
    metrics: Mapped[dict] = mapped_column(JSON)
    issue_segments: Mapped[list] = mapped_column(JSON)
    fingerprint: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class BackgroundAsset(Entity, Base):
    __tablename__ = "background_assets"
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    asset_id: Mapped[str] = mapped_column(ForeignKey("asset_identities.id"))
    prompt_id: Mapped[str | None] = mapped_column(ForeignKey("prompts.id"))
    provider: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class CompositionAsset(Entity, Base):
    __tablename__ = "composition_assets"
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    asset_id: Mapped[str] = mapped_column(ForeignKey("asset_identities.id"))
    source_video_id: Mapped[str | None] = mapped_column(ForeignKey("source_videos.id"))
    background_id: Mapped[str | None] = mapped_column(ForeignKey("background_assets.id"))
    person_asset_id: Mapped[str | None] = mapped_column(ForeignKey("asset_identities.id"))
    parameters: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class Review(Entity, Base):
    __tablename__ = "reviews"
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    asset_id: Mapped[str] = mapped_column(ForeignKey("asset_identities.id"))
    reviewer_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(String(30))
    issues: Mapped[list] = mapped_column(JSON)
    note: Mapped[str] = mapped_column(Text)


class PublishingAccount(Entity, Base):
    __tablename__ = "publishing_accounts"
    country_id: Mapped[str] = mapped_column(ForeignKey("countries.id"))
    name: Mapped[str] = mapped_column(String(200))
    country: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(30))
    authorization: Mapped[str] = mapped_column(String(200))
    secret_ref: Mapped[str | None] = mapped_column(String(200))
    last_publish: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    queue: Mapped[int] = mapped_column(Integer)
    errors: Mapped[list] = mapped_column(JSON)
    hubstudio_env_id: Mapped[str | None] = mapped_column(String(100))
    cdp_endpoint: Mapped[str | None] = mapped_column(Text)
    expected_username: Mapped[str | None] = mapped_column(String(200))
    identity_verification: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    vpn_verification: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    cooldown_seconds: Mapped[int] = mapped_column(Integer, default=120, server_default="120")
    publishing_context: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")


class AccountProduct(Base):
    __tablename__ = "account_products"
    account_id: Mapped[str] = mapped_column(ForeignKey("publishing_accounts.id"), primary_key=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"), primary_key=True)


class PublishingTask(Entity, Base):
    __tablename__ = "publishing_tasks"
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    video_id: Mapped[str] = mapped_column(ForeignKey("asset_identities.id"))
    account_id: Mapped[str | None] = mapped_column(ForeignKey("publishing_accounts.id"))
    country: Mapped[str] = mapped_column(String(100))
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"))
    caption: Mapped[str] = mapped_column(Text)
    product_binding: Mapped[str] = mapped_column(String(200))
    schedule: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(30))
    error: Mapped[str | None] = mapped_column(Text)
    batch_id: Mapped[str | None] = mapped_column(ForeignKey("publishing_batches.id", name="fk_publishing_task_batch"), index=True)
    selected_description: Mapped[dict | None] = mapped_column(JSON)
    input_sha256: Mapped[str | None] = mapped_column(String(64))
    current_step: Mapped[str] = mapped_column(String(40), default="PREFLIGHT", server_default="PREFLIGHT")
    checkpoint: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    submission_state: Mapped[str] = mapped_column(String(40), default="NOT_SUBMITTED", server_default="NOT_SUBMITTED")
    platform_video_id: Mapped[str | None] = mapped_column(String(200))
    platform_url: Mapped[str | None] = mapped_column(Text)
    moderation_status: Mapped[str] = mapped_column(String(40), default="NOT_CHECKED", server_default="NOT_CHECKED")
    product_link_status: Mapped[str] = mapped_column(String(40), default="NOT_CHECKED", server_default="NOT_CHECKED")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class PublishingBatch(Entity, Base):
    __tablename__ = "publishing_batches"
    __table_args__ = (CheckConstraint("concurrency = 1", name="ck_publishing_batch_serial"),)
    status: Mapped[str] = mapped_column(String(40))
    concurrency: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    settings_snapshot: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    active_task_id: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class PublishRun(Entity, Base):
    __tablename__ = "publish_runs"
    __table_args__ = (CheckConstraint("attempt >= 1", name="ck_publish_run_attempt"),)
    publishing_task_id: Mapped[str] = mapped_column(ForeignKey("publishing_tasks.id"), index=True)
    batch_id: Mapped[str] = mapped_column(ForeignKey("publishing_batches.id"), index=True)
    status: Mapped[str] = mapped_column(String(40))
    attempt: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_heartbeat: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    logs: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")
    evidence: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")


class StepExecution(Entity, Base):
    __tablename__ = "publishing_step_executions"
    publish_run_id: Mapped[str] = mapped_column(ForeignKey("publish_runs.id"), index=True)
    publishing_task_id: Mapped[str] = mapped_column(ForeignKey("publishing_tasks.id"), index=True)
    name: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(40))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    checkpoint: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)


class ResourceLock(Base):
    __tablename__ = "publishing_resource_locks"
    resource_key: Mapped[str] = mapped_column(String(200), primary_key=True)
    publishing_task_id: Mapped[str] = mapped_column(ForeignKey("publishing_tasks.id"), index=True)
    publish_run_id: Mapped[str | None] = mapped_column(ForeignKey("publish_runs.id"))
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DescriptionUsage(Entity, Base):
    __tablename__ = "publishing_description_usage"
    __table_args__ = (UniqueConstraint("publishing_task_id", name="uq_description_usage_task"),)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"), index=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("publishing_accounts.id"), index=True)
    publishing_task_id: Mapped[str] = mapped_column(ForeignKey("publishing_tasks.id"))
    description_id: Mapped[str] = mapped_column(String(100))
    description_sha256: Mapped[str] = mapped_column(String(64))
    selected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ProcessingRun(Entity, Base):
    __tablename__ = "processing_runs"
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    stage: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(30))
    progress: Mapped[float] = mapped_column(Float)
    logs: Mapped[list] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    provider: Mapped[str] = mapped_column(String(100), default="mock", server_default="mock")
    command: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")
    cwd: Mapped[str | None] = mapped_column(Text)
    pid: Mapped[int | None] = mapped_column(Integer)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    exit_code: Mapped[int | None] = mapped_column(Integer)
    stdout_log_path: Mapped[str | None] = mapped_column(Text)
    stderr_log_path: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    idempotency_key: Mapped[str | None] = mapped_column(String(200), index=True)
    last_heartbeat: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    termination_signal: Mapped[int | None] = mapped_column(Integer)
    timed_out: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    resource_info: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")


class LegacyBinding(Entity, Base):
    __tablename__ = "legacy_bindings"
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"))
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"))
    source_system: Mapped[str] = mapped_column(String(100), default="TikTok_Auto", server_default="TikTok_Auto")
    context_id: Mapped[str | None] = mapped_column(String(100), index=True)
    source_document: Mapped[str | None] = mapped_column(Text)
    source_row: Mapped[int | None] = mapped_column(Integer)
    video_id: Mapped[str | None] = mapped_column(String(100))
    download_task_id: Mapped[str | None] = mapped_column(String(100))
    source_path: Mapped[str | None] = mapped_column(Text)
    sha256: Mapped[str | None] = mapped_column(String(64))
    legacy_reference: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    @property
    def legacy_project(self):
        return self.source_system

    @property
    def legacy_context_id(self):
        return self.context_id

    @property
    def legacy_video_id(self):
        return self.video_id

    @property
    def legacy_download_task_id(self):
        return self.download_task_id


class ErrorLog(Entity, Base):
    __tablename__ = "error_logs"
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    stage: Mapped[str] = mapped_column(String(30))
    code: Mapped[str] = mapped_column(String(100))
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    provider: Mapped[str] = mapped_column(String(100), default="mock", server_default="mock")
    details: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    retryable: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())

    @property
    def error_code(self):
        return self.code


class AuditLog(Entity, Base):
    __tablename__ = "audit_logs"
    task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id"), index=True)
    action: Mapped[str] = mapped_column(String(100))
    entity_id: Mapped[str] = mapped_column(String(100))
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CollectorRule(Entity, Base):
    __tablename__ = "collector_rules"
    country_id: Mapped[str] = mapped_column(ForeignKey("countries.id"))
    keyword_id: Mapped[str] = mapped_column(ForeignKey("keywords.id"))
    country: Mapped[str] = mapped_column(String(100))
    keyword: Mapped[str] = mapped_column(String(300))
    product_id: Mapped[str | None] = mapped_column(ForeignKey("products.id"))
    product_image: Mapped[str] = mapped_column(Text)
    min_views: Mapped[int] = mapped_column(Integer)
    min_likes: Mapped[int] = mapped_column(Integer)
    min_ratio: Mapped[float] = mapped_column(Float)
    match_threshold: Mapped[float] = mapped_column(Float)
    filter_overlay: Mapped[bool] = mapped_column(Boolean)
    filter_watermark: Mapped[bool] = mapped_column(Boolean)
    filter_subtitle: Mapped[bool] = mapped_column(Boolean)
    views: Mapped[int] = mapped_column(Integer)
    likes: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(30))
    input_source: Mapped[str] = mapped_column(String(30), default="manual", server_default="manual")
    video_url: Mapped[str | None] = mapped_column(Text)
    raw_keyword: Mapped[str | None] = mapped_column(String(300))
    product_images: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")
    context_id: Mapped[str | None] = mapped_column(String(100))
    source_document: Mapped[str | None] = mapped_column(Text)
    source_row: Mapped[int | None] = mapped_column(Integer)
    last_task_id: Mapped[str | None] = mapped_column(String(100))
    last_run_id: Mapped[str | None] = mapped_column(String(100))
    result: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")


class FactorySettings(Entity, Base):
    __tablename__ = "factory_settings"
    theme: Mapped[str] = mapped_column(String(20))
    language: Mapped[str] = mapped_column(String(20))
    output_width: Mapped[int] = mapped_column(Integer)
    output_height: Mapped[int] = mapped_column(Integer)
    output_fps: Mapped[float] = mapped_column(Float)
    default_provider: Mapped[str] = mapped_column(String(80))
    mock_delay_ms: Mapped[int] = mapped_column(Integer)
    review_required: Mapped[bool] = mapped_column(Boolean)
    production_policy: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
