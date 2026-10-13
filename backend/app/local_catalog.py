from datetime import datetime
from pathlib import Path
import subprocess
from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import Field, model_validator
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app import models as m
from app.domain import audit, new_id, now
from app.integrations.quality_adapter import QUALITY_THRESHOLDS
from app.integrations.asset_registry import probe_media
from app.schemas import ProductView, SettingsView, StrictModel


class LocalProduct(ProductView):
    id: str = Field(default="", pattern=r"^(?:[a-zA-Z0-9_-]{1,100})?$")
    name: str = Field(min_length=1, max_length=200)
    country: str = Field(min_length=1, max_length=100)
    image: str = Field(default="", max_length=2000)
    url: str = Field(default="", max_length=2000)
    binding_reference: str = Field(default="", max_length=200)
    description: str = Field(default="", max_length=2000)
    status: Literal["active", "draft", "inactive"] = "draft"
    keywords: list[str] = Field(default_factory=list, max_length=50)


class LocalAccount(StrictModel):
    name: str = Field(min_length=1, max_length=200)
    country: str = Field(min_length=1, max_length=100)
    status: Literal["CONNECTED", "NOT_CONNECTED", "EXPIRED", "WAITING_MANUAL"] = "NOT_CONNECTED"
    product_ids: list[str] = Field(default_factory=list, max_length=100)


class ProductionPolicy(StrictModel):
    concurrency: int = Field(default=1, ge=1, le=1)
    auto_advance: bool = True
    auto_resume: bool = True
    retry_limit: int = Field(default=2, ge=0, le=5)
    background_strategy: Literal["chatgpt_rpa", "manual"] = "chatgpt_rpa"
    person_review_required: bool = False
    quality_thresholds: dict[str, float] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_quality_thresholds(self):
        if any(key not in QUALITY_THRESHOLDS for key in self.quality_thresholds):
            raise ValueError("存在未连接的质量阈值")
        for key, value in self.quality_thresholds.items():
            if value < 0 or value != value or value == float("inf"):
                raise ValueError("质量阈值必须是非负有限数字")
            if key in {"max_dimension", "alpha_threshold", "min_object_pixels"} and not value.is_integer():
                raise ValueError("分析尺寸、Alpha 阈值及像素数必须为整数")
            if key == "max_dimension" and not 16 <= value <= 1920:
                raise ValueError("分析尺寸必须为16至1920")
            if key == "alpha_threshold" and not 0 <= value <= 255:
                raise ValueError("Alpha 阈值必须为0至255")
            if key not in {"max_dimension", "alpha_threshold", "min_object_pixels", "min_issue_duration_s"} and value > 1:
                raise ValueError("比例阈值必须为0至1")
        return self


class LocalSettings(SettingsView):
    production_policy: ProductionPolicy = Field(default_factory=ProductionPolicy)


def create_local_catalog_router(repository, config, production):
    router = APIRouter(tags=["local-production-catalog"])

    def image_path(value):
        if not value:
            return ""
        path = Path(value).resolve()
        roots = (config.storage_root.resolve(), (config.legacy_tiktok_auto_root / "data/product_references").resolve())
        if not any(path.is_relative_to(root) for root in roots) or path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"} or not path.is_file():
            raise HTTPException(422, "产品图必须通过文件选择器上传，或来自已读取的生产数据源")
        try:
            image = probe_media(path, count_frames=True)
            if image["codec"] not in {"png", "mjpeg", "webp"} or image.get("frame_count") != 1:
                raise ValueError("not a single image")
        except (OSError, ValueError, subprocess.SubprocessError):
            raise HTTPException(422, "产品图片无法解码") from None
        return str(path)

    def require_local(key, identifier):
        row = next((item for item in repository.state(scope="production")[key] if item["id"] == identifier), None)
        if not row:
            raise HTTPException(404, "本地资料不存在")
        return row

    @router.get("/api/products/{product_id}/details")
    def product_details(product_id: str):
        state = repository.state(scope="production")
        product = require_local("products", product_id)
        tasks = [row for row in state["tasks"] if row["product_id"] == product_id]
        task_ids = {row["id"] for row in tasks}
        images = list(dict.fromkeys([value for value in [product["image"], *[value for row in tasks for value in row.get("context", {}).get("task_context", {}).get("product_images", [])]] if value]))
        return {"product": product, "tasks": tasks, "assets": [row for row in state["assets"] if row["task_id"] in task_ids],
                "images": images, "contexts": [row.get("context", {}).get("task_context", {}) for row in tasks], "mode": "REAL_LOCAL"}

    @router.get("/api/products/{product_id}/images/{index}")
    def product_reference_image(product_id: str, index: int):
        details = product_details(product_id)
        if index < 0 or index >= len(details["images"]):
            raise HTTPException(404, "产品参考图不存在")
        return FileResponse(image_path(details["images"][index]), content_disposition_type="inline")

    def save_product(payload, identifier=None):
        values = payload.model_dump()
        values.update(name=values["name"].strip(), country=values["country"].strip(), image=image_path(values["image"]))
        values["keywords"] = list(dict.fromkeys(value.strip() for value in values["keywords"] if value.strip()))
        if not values["name"] or not values["country"]:
            raise HTTPException(422, "产品名称和国家不能为空")
        if identifier:
            require_local("products", identifier)
            if values["id"] not in {"", identifier}:
                raise HTTPException(409, "产品 ID 不可更改")
            values["id"] = identifier
        else:
            values["id"] = values["id"] or new_id("prd")

        def change(state):
            current = next((row for row in state["products"] if row["id"] == values["id"]), None)
            if current and not identifier:
                raise HTTPException(409, "产品 ID 已存在")
            if any(row["product_id"] == values["id"] and row["country"] != values["country"] for row in state["tasks"]):
                raise HTTPException(409, "关联任务的国家不可改写；请为新国家创建独立产品")
            if any(values["id"] in row["product_ids"] and row["country"] != values["country"] for row in state["accounts"]):
                raise HTTPException(409, "关联账号与产品的国家必须一致；请先解除账号关联")
            if current:
                current.update(values)
            else:
                state["products"].append(values)
            audit(state, "product.saved", values["id"], "产品资料已持久化；已有任务的国家、关键词和参考图快照不变")
            return values
        return {"product": repository.mutate(change), "mode": "REAL_LOCAL"}

    @router.post("/api/products", status_code=201)
    def create_product(payload: LocalProduct):
        return save_product(payload)

    @router.put("/api/products/{product_id}")
    def update_product(product_id: str, payload: LocalProduct):
        return save_product(payload, product_id)

    @router.delete("/api/products/{product_id}")
    def delete_product(product_id: str, confirmed: bool = False):
        if not confirmed:
            raise HTTPException(422, "请明确确认删除产品资料；产品图片不会删除")
        with repository.lock, Session(repository.engine) as session:
            product = require_local("products", product_id)
            refs = []
            for model, column, label in ((m.Task, m.Task.product_id, "task"), (m.PublishingTask, m.PublishingTask.product_id, "publishing"),
                    (m.CollectorRule, m.CollectorRule.product_id, "collector"), (m.Keyword, m.Keyword.product_id, "keyword")):
                refs.extend({"type": label, "id": value} for value in session.scalars(select(model.id).where(column == product_id)))
            refs.extend({"type": "account", "id": value} for value in session.scalars(select(m.AccountProduct.account_id).where(m.AccountProduct.product_id == product_id)))
            if refs:
                raise HTTPException(409, {"code": "PRODUCT_REFERENCED", "message": "产品仍被使用，可停用而不能删除", "references": refs})
            session.execute(delete(m.Product).where(m.Product.id == product_id))
            session.add(m.AuditLog(id=new_id("audit"), task_id=None, action="product.deleted", entity_id=product_id,
                message="已删除未关联产品资料；未删除图片：" + product["name"], created_at=datetime.fromisoformat(now())))
            session.commit()
        return {"id": product_id, "status": "DELETED", "images_deleted": False}

    def save_account(payload, identifier=None):
        if payload.status == "CONNECTED":
            raise HTTPException(409, {"code": "AUTHORIZATION_NOT_VERIFIED", "message": "没有已连接的真实授权检测，不能标记已连接；请保存为未连接或等待人工"})
        if identifier:
            require_local("accounts", identifier)
        identifier = identifier or new_id("acc")
        values = payload.model_dump()
        if not values["name"].strip() or not values["country"].strip():
            raise HTTPException(422, "账号名称和国家不能为空")
        values.update(name=values["name"].strip(), country=values["country"].strip(), product_ids=list(dict.fromkeys(values["product_ids"])))

        def change(state):
            for product_id in values["product_ids"]:
                product = next((row for row in state["products"] if row["id"] == product_id), None)
                if not product or product["country"] != values["country"]:
                    raise HTTPException(409, "关联产品必须存在且与账号国家一致")
            current = next((row for row in state["accounts"] if row["id"] == identifier), None)
            authorization = {"NOT_CONNECTED": "未连接真实授权；本地资料不代表登录或发布权限", "EXPIRED": "用户标记授权已过期；未核验平台登录", "WAITING_MANUAL": "等待用户在平台完成登录或授权；发布后端尚未连接"}[values["status"]]
            if current:
                current.update(values, authorization=authorization)
            else:
                state["accounts"].append({"id": identifier, **values, "authorization": authorization,
                    "last_publish": None, "queue": 0, "errors": []})
            audit(state, "account.local_saved", identifier, "已保存本地账号资料；未进行授权、未保存密码/Cookie/Token")
            return next(row for row in state["accounts"] if row["id"] == identifier)
        return {"account": repository.mutate(change), "mode": "REAL_LOCAL", "authorization_verified": False}

    @router.post("/api/accounts", status_code=201)
    def create_account(payload: LocalAccount):
        return save_account(payload)

    @router.get("/api/accounts/{account_id}")
    def account_details(account_id: str):
        account = require_local("accounts", account_id)
        state = repository.state(scope="production")
        return {"account": account, "authorization": {"mode": "REAL_LOCAL_NOT_CONNECTED", "verified": False,
                    "status": account["status"], "publishing_status": "NOT_PRODUCTION_READY"},
                "history": [], "queue": [], "products": [row for row in state["products"] if row["id"] in account["product_ids"]],
                "errors": account["errors"], "audits": [row for row in state["audits"] if row["entity_id"] == account_id]}

    @router.put("/api/accounts/{account_id}")
    def update_account(account_id: str, payload: LocalAccount):
        return save_account(payload, account_id)

    @router.delete("/api/accounts/{account_id}")
    def delete_account(account_id: str, confirmed: bool = False):
        if not confirmed:
            raise HTTPException(422, "请明确确认删除本地账号资料")
        with repository.lock, Session(repository.engine) as session:
            require_local("accounts", account_id)
            refs = list(session.scalars(select(m.PublishingTask.id).where(m.PublishingTask.account_id == account_id)))
            if refs:
                raise HTTPException(409, {"code": "ACCOUNT_REFERENCED", "message": "账号仍有关联发布记录，不能删除", "references": refs})
            session.execute(delete(m.AccountProduct).where(m.AccountProduct.account_id == account_id))
            session.execute(delete(m.PublishingAccount).where(m.PublishingAccount.id == account_id))
            session.add(m.AuditLog(id=new_id("audit"), task_id=None, action="account.local_deleted", entity_id=account_id,
                message="已删除未关联本地账号资料；未修改外部账号", created_at=datetime.fromisoformat(now())))
            session.commit()
        return {"id": account_id, "status": "DELETED", "external_account_changed": False}

    @router.get("/api/settings/production")
    def production_settings():
        value = repository.state()["settings"]
        return {"settings": value, "effective_policy": production.policy(),
                "quality_thresholds": {**QUALITY_THRESHOLDS, **value.get("production_policy", {}).get("quality_thresholds", {})},
                "storage_root": str(config.storage_root.resolve()), "legacy_config_read_only": True, "mode": "REAL_LOCAL"}

    @router.put("/api/settings/production")
    def save_production_settings(payload: LocalSettings):
        values = payload.model_dump()
        values["default_provider"] = values["production_policy"]["background_strategy"]
        def change(state):
            state["settings"] = values
            audit(state, "settings.production_saved", "settings", "已保存 Factory 生产策略；后续调度读取配置，不修改旧生产规则")
        repository.mutate(change)
        return {"settings": values, "effective_policy": production.policy(), "mode": "REAL_LOCAL"}

    return router
