from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator

from app.integrations.publishing.config import local_endpoint


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Description(Input):
    id: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=2200)
    weight: float = Field(default=1, gt=0, allow_inf_nan=False)
    enabled: bool = True


class DescriptionPolicy(Input):
    mode: Literal["random", "weighted"] = "random"
    exclude_last_n: int = Field(default=3, ge=0, le=100)


class ProductInput(Input):
    name: str = Field(min_length=1, max_length=200)
    country: str = Field(min_length=2, max_length=100)
    image: str = ""
    url: str = ""
    binding_reference: str = Field(default="", max_length=200)
    description: str = ""
    status: Literal["active", "disabled"] = "active"
    keywords: list[str] = Field(default_factory=list)
    platform_product_id: str | None = Field(default=None, max_length=200)
    descriptions: list[Description] = Field(default_factory=list)
    description_selection: DescriptionPolicy = Field(default_factory=DescriptionPolicy)
    publishing_context: dict = Field(default_factory=dict)

    @field_validator("status", mode="before")
    @classmethod
    def factory_catalog_status(cls, value):
        return "disabled" if value in ("draft", "inactive") else value

    @field_validator("descriptions")
    @classmethod
    def unique_ids(cls, values):
        if len({row.id for row in values}) != len(values):
            raise ValueError("DESCRIPTION_IDS_MUST_BE_UNIQUE")
        return values


class AccountInput(Input):
    name: str = Field(min_length=1, max_length=200)
    country: str = Field(min_length=2, max_length=100)
    status: Literal["NOT_CONNECTED", "WAITING_MANUAL", "EXPIRED", "DISABLED"] = "NOT_CONNECTED"
    product_ids: list[str] = Field(default_factory=list)
    hubstudio_env_id: str | None = Field(default=None, max_length=100)
    cdp_endpoint: str | None = None
    expected_username: str | None = Field(default=None, max_length=200)
    enabled: bool = True
    cooldown_seconds: int = Field(default=120, ge=0, le=86400)
    publishing_context: dict = Field(default_factory=dict)

    @field_validator("cdp_endpoint")
    @classmethod
    def validate_cdp(cls, value):
        return local_endpoint(value, websocket=True) if value else None


class AccountConfirmation(Input):
    username: str = Field(min_length=1, max_length=200)
    country: str = Field(min_length=2, max_length=100)
    vpn_country: str = Field(min_length=2, max_length=100)
    note: str = Field(min_length=1, max_length=2000)


class AccountControl(Input):
    enabled: bool


class TaskInput(Input):
    task_id: str
    video_id: str
    account_id: str
    product_id: str
    caption: str = Field(default="", max_length=2200)
    schedule: datetime | None = None
    upload_only: bool = False


class BatchInput(Input):
    task_ids: list[str] = Field(min_length=1, max_length=200)


class PublishConfirmation(Input):
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    confirmed: StrictBool = False
    note: str = Field(default="", max_length=2000)


class DescriptionReuse(Input):
    source_product_id: str = Field(min_length=1)
    source_pool_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_input_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    target_input_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    confirmed: StrictBool = False


class ControlInput(Input):
    action: Literal["pause", "resume", "cancel"]
    batch_id: str
