from typing import Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field


class AssetDeleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fingerprint: str = Field(min_length=64, max_length=64)
    confirmed: bool = False
    impact_confirmed: bool = False


class AssetBulkDeleteItem(AssetDeleteRequest):
    asset_id: str = Field(min_length=1, max_length=200)


class AssetBulkDeleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[AssetBulkDeleteItem] = Field(min_length=1, max_length=100)


class AssetFamilyPlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    asset_ids: list[str] = Field(min_length=1, max_length=100)
    scope: Literal["production", "history", "technical"] = "production"


class AssetFamilyDeleteRequest(AssetFamilyPlanRequest, AssetDeleteRequest):
    pass


def create_asset_router(registry):
    router = APIRouter(tags=["asset-registry"])

    @router.get("/api/assets/storage-usage")
    def storage_usage():
        return registry.storage_usage()

    @router.get("/api/assets/media-status")
    def media_status(scope: Literal["production", "history", "technical", "raw"] = "production", task_id: str | None = None):
        return [registry.media_status(asset) for asset in registry.repository.read_assets(scope=scope, task_id=task_id)]

    @router.get("/api/assets/deletion-status")
    def deletion_statuses(scope: Literal["production", "history", "technical"] = "production", asset_ids: list[str] | None = Query(default=None)):
        return registry.deletion_statuses(scope=scope, asset_ids=asset_ids)

    @router.post("/api/assets/bulk-delete")
    def bulk_delete(request: AssetBulkDeleteRequest):
        return registry.bulk_delete(request.items)

    @router.post("/api/assets/family-deletion-plan")
    def family_deletion_plan(request: AssetFamilyPlanRequest):
        return registry.family_deletion_plan(request.asset_ids, scope=request.scope)

    @router.post("/api/assets/family-delete")
    def family_delete(request: AssetFamilyDeleteRequest):
        return registry.delete_family(request.asset_ids, request.fingerprint, scope=request.scope,
            confirmed=request.confirmed, impact_confirmed=request.impact_confirmed)

    @router.get("/api/assets/{asset_id}/deletion-status")
    def deletion_status(asset_id: str, scope: Literal["production", "history", "technical"] = "production"):
        return registry.deletion_status(asset_id, scope=scope)

    @router.delete("/api/assets/{asset_id}")
    def delete_asset(asset_id: str, request: AssetDeleteRequest):
        return registry.delete_asset(asset_id, request.fingerprint, confirmed=request.confirmed, impact_confirmed=request.impact_confirmed)

    @router.get("/api/assets/{asset_id}/registry")
    def asset_registry(asset_id: str, scope: Literal["production", "history", "technical", "raw"] = "production"):
        return registry.details(asset_id, scope=scope)

    return router
