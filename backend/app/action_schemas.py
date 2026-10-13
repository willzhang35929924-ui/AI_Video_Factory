from datetime import datetime

from pydantic import Field, model_validator

from app.schemas import BackgroundKind, CollectorView, ProductView, PublishingView, SettingsView, StrictModel


class EmptyPayload(StrictModel):
    pass


class CreateTask(StrictModel):
    country: str = Field(min_length=1, max_length=100)
    keyword: str = Field(min_length=1, max_length=300)
    product_id: str = Field(min_length=1, max_length=100)


class SaveCollector(StrictModel):
    rows: list[CollectorView] = Field(max_length=200)


class Collect(StrictModel):
    row_ids: list[str] = Field(default_factory=list, max_length=200)


class ImportCollector(StrictModel):
    source: str = Field(pattern=r"^(tencent|excel|batch)$")
    url: str | None = Field(default=None, max_length=2000)
    filename: str | None = Field(default=None, max_length=200)
    lines: str | None = Field(default=None, max_length=30000)


class Interval(StrictModel):
    start: float = Field(default=0, ge=0)
    end: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def valid_interval(self):
        if self.end is not None and self.end <= self.start:
            raise ValueError("异常区间结束时间必须晚于开始时间")
        return self


class PromptOptions(StrictModel):
    scene_type: str = Field(default="简约生活空间", min_length=1, max_length=100)
    style: str = Field(default="真实摄影", min_length=1, max_length=100)
    lighting: str = Field(default="柔和侧光", min_length=1, max_length=100)
    color: str = Field(default="中性暖灰", min_length=1, max_length=100)
    aspect_ratio: str = Field(default="9:16", pattern=r"^(9:16|1:1|16:9)$")
    provider: BackgroundKind = "chatgpt_rpa"


class BackgroundOptions(StrictModel):
    prompt_id: str | None = None
    provider: BackgroundKind = "chatgpt_rpa"


class UploadBackground(StrictModel):
    filename: str = Field(min_length=1, max_length=200)


class ComposeOptions(StrictModel):
    exposure: float = Field(default=0, ge=-5, le=5)
    contrast: float = Field(default=1, ge=0, le=4)
    saturation: float = Field(default=1, ge=0, le=4)
    crop: str = Field(default="center", pattern=r"^(center|fit|fill|top|bottom)$")
    width: int = Field(default=1080, gt=0)
    height: int = Field(default=1920, gt=0)
    fps: float = Field(default=30, gt=0)


class ReviewNote(StrictModel):
    note: str = Field(default="", max_length=2000)
    confirmed_full_video_watermark_free: bool = Field(default=False, strict=True)


class SavePublishing(StrictModel):
    rows: list[PublishingView] = Field(max_length=200)


class SchedulePublishing(StrictModel):
    schedule: datetime | None = None


class AccountStatus(StrictModel):
    status: str = Field(pattern=r"^(authorized|expired|pending|error)$")


class SaveProduct(ProductView):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,100}$")
    name: str = Field(min_length=1, max_length=200)
    country: str = Field(min_length=1, max_length=100)
    description: str = Field(max_length=2000)
    status: str = Field(pattern=r"^(active|draft)$")
    keywords: list[str] = Field(max_length=50)


ACTION_SCHEMAS = {
    "create_task": CreateTask, "retry_task": EmptyPayload, "pause_task": EmptyPayload,
    "save_collector": SaveCollector, "collect": Collect, "import_collector": ImportCollector,
    "start_cutout": EmptyPayload, "retry_interval": Interval, "regenerate_mask": Interval,
    "manual_fix": Interval, "accept_cutout": EmptyPayload, "generate_prompt": PromptOptions,
    "generate_background": BackgroundOptions, "upload_background": UploadBackground,
    "compose": ComposeOptions, "update_composition": ComposeOptions,
    "review_approve": ReviewNote, "review_reject": ReviewNote, "review_retry": ReviewNote, "review_manual": ReviewNote,
    "save_publishing": SavePublishing, "generate_caption": EmptyPayload, "schedule_publish": SchedulePublishing,
    "publish_now": EmptyPayload, "authorize_account": EmptyPayload, "update_account_status": AccountStatus,
    "save_product": SaveProduct, "save_settings": SettingsView, "reset_demo": EmptyPayload,
}
