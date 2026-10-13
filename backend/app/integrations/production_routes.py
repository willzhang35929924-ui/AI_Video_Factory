import asyncio
import subprocess
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from app.domain import find, new_id, now
from app.integrations.input_routes import MAX_IMAGE_BYTES, upload_body


class RestoreCancelledBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmed: StrictBool
    fingerprint: str = Field(min_length=64, max_length=64, pattern="^[a-f0-9]+$")


def create_production_router(production):
    router = APIRouter(tags=["real-production"])

    @router.get("/api/production/readiness")
    async def readiness(refresh: bool = False, stage: str | None = None, task_id: str | None = None):
        if task_id:
            find(production.repository.state(), "tasks", task_id)
        return await production.readiness.check(refresh, stage=stage, task_id=task_id)

    @router.get("/api/collector/preflight")
    async def collector_preflight(refresh: bool = False, task_id: str | None = None):
        if task_id:
            find(production.repository.state(), "tasks", task_id)
        return await production.readiness.check(refresh, stage="collector", task_id=task_id)

    @router.post("/api/production/prepare-tiktok", status_code=202)
    async def prepare_tiktok(background_tasks: BackgroundTasks):
        async with production.start_lock:
            state = production.repository.state()
            if any(row["status"] in {"queued", "running"} for row in state["runs"]):
                raise HTTPException(409, "COLLECTOR_ACTIVE：请等待当前采集结束后准备浏览器")
            if any(row["status"] in {"QUEUED", "RUNNING"} or row["status"] == "PAUSED" and (
                    row.get("active_task_id") or any(item["status"] == "RUNNING" for item in row.get("items", [])))
                    for row in state.get("batches", [])):
                raise HTTPException(409, "PRODUCTION_QUEUE_ACTIVE：不会在生产中改变浏览器状态")
            result, created = await production.readiness.queue_tiktok_preparation()
            if created:
                background_tasks.add_task(production.readiness.run_tiktok_preparation, result["operation_id"])
            return result

    @router.get("/api/production/browser-preparations/{operation_id}")
    def browser_preparation(operation_id: str):
        try:
            return production.readiness.tiktok_preparation(operation_id)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except OSError:
            raise HTTPException(404, "BROWSER_PREPARATION_NOT_FOUND") from None

    @router.get("/api/production/queue")
    def queue():
        return production.queue()

    @router.get("/api/production/batches/{batch_id}")
    def batch(batch_id: str):
        value = find(production.repository.state(), "batches", batch_id)
        return {**value, "summary": production.summary(value["task_ids"])}

    @router.get("/api/production/batches/{batch_id}/restore-cancelled")
    def cancelled_restore_plan(batch_id: str):
        return production.cancelled_restore_plan(batch_id)

    @router.get("/api/production/batches/{batch_id}/restore-environment")
    def environment_restore_plan(batch_id: str):
        return production.environment_restore_plan(batch_id)

    @router.post("/api/production/batches/{batch_id}/restore-environment", status_code=202)
    async def restore_environment(batch_id: str, payload: RestoreCancelledBatchRequest):
        return await production.restore_environment(batch_id, confirmed=payload.confirmed, fingerprint=payload.fingerprint)

    @router.post("/api/production/batches/{batch_id}/restore-cancelled", status_code=202)
    async def restore_cancelled(batch_id: str, payload: RestoreCancelledBatchRequest, background_tasks: BackgroundTasks):
        result = await production.restore_cancelled(batch_id, confirmed=payload.confirmed, fingerprint=payload.fingerprint)
        background_tasks.add_task(production.launch, batch_id)
        return result

    @router.get("/api/assets/{asset_id}/content")
    def media(asset_id: str):
        path, mime = production.registry.content(asset_id)
        return FileResponse(path, media_type=mime, content_disposition_type="inline")

    @router.get("/api/assets/{asset_id}/preview")
    def preview(asset_id: str, channel: str = "black"):
        path, mime = production.registry.preview(asset_id, channel)
        return FileResponse(path, media_type=mime, content_disposition_type="inline")

    @router.get("/api/products/{product_id}/image")
    def product_image(product_id: str):
        product = find(production.repository.state(), "products", product_id)
        path = Path(product["image"]).resolve()
        roots = (production.settings.storage_root.resolve(), (production.settings.legacy_tiktok_auto_root / "data/product_references").resolve())
        if not any(path.is_relative_to(root) for root in roots) or not path.is_file() or path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
            raise HTTPException(404, "产品图片不存在或不属于已登记的参考图")
        return FileResponse(path, content_disposition_type="inline")

    @router.get("/api/tasks/{task_id}/workflows")
    def workflows(task_id: str, scope: Literal["production", "history", "technical"] = "production"):
        find(production.repository.state(), "tasks", task_id)
        snapshots = production.repository.workflows(task_id)
        if scope == "history" or production.settings.app_mode == "mock":
            return snapshots
        run_ids = {row["id"] for row in production.repository.state(scope="production")["runs"] if row["task_id"] == task_id}
        return [row for row in snapshots if (row.get("processing_run_id") in run_ids) == (scope == "production")]

    @router.get("/api/quality")
    def quality(task_id: str | None = None):
        return [row for row in production.repository.state(scope="production")["quality_assessments"] if task_id is None or row["task_id"] == task_id]

    @router.post("/api/background/manual", status_code=201)
    async def background(request: Request, background_tasks: BackgroundTasks, task_id: str, prompt_id: str):
        state = production.repository.state()
        task, prompt = find(state, "tasks", task_id), find(state, "prompts", prompt_id)
        if task["execution_mode"] != "integration" or prompt["task_id"] != task_id or prompt["status"] == "MOCK":
            raise HTTPException(409, "请为真实任务选择已保存的提示词")
        if any(row["task_id"] == task_id and row["status"] in {"running", "queued"} for row in state["runs"]):
            raise HTTPException(409, "TASK_BUSY：当前任务正在执行，请完成后上传新版本")
        payload, filename = await upload_body(request, MAX_IMAGE_BYTES)
        if not (payload.startswith(b"\x89PNG\r\n\x1a\n") or payload.startswith(b"\xff\xd8\xff") or (payload[:4] == b"RIFF" and payload[8:12] == b"WEBP")):
            raise HTTPException(422, "请选择 PNG、JPEG 或 WebP 图片")
        run_id = new_id("run")
        directory = production.settings.storage_root / "projects" / task["project_id"] / "tasks" / task_id / "background" / "manual" / run_id
        path = directory / filename
        def begin_upload(current):
            if any(row["task_id"] == task_id and row["status"] in {"running", "queued"} for row in current["runs"]) or any(
                item["task_id"] == task_id and item["status"] in {"QUEUED", "RUNNING"}
                for batch in current["batches"] if batch["status"] in {"QUEUED", "RUNNING", "PAUSED"} for item in batch["items"]
            ):
                raise HTTPException(409, "TASK_BUSY：当前任务正在执行，请完成后上传新版本")
            current["runs"].append({"id": run_id, "task_id": task_id, "stage": "background",
                "provider": "ManualUpload", "status": "running", "progress": 0, "logs": ["人工上传背景，正在验证"], "created_at": now(), "started_at": now()})
        production.repository.mutate(begin_upload)
        try:
            directory.mkdir(parents=True, exist_ok=False)
            path.write_bytes(payload)
            asset = await asyncio.to_thread(production.registry.register, task, {"path": str(path), "source_system": "ManualUpload",
                                             "legacy_reference": {"factory_fingerprint": production._fingerprint(task, "background", prompt_id)}},
                                             "background", run_id, prompt_id=prompt_id)
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            production.repository.mutate(lambda current: find(current, "runs", run_id).update(status="failed", finished_at=now(),
                error_code="INVALID_BACKGROUND", error_message=type(exc).__name__))
            raise HTTPException(422, "背景图片解码失败；失败记录已保留") from None
        production.repository.mutate(lambda current: find(current, "runs", run_id).update(status="success", progress=100, finished_at=now()))
        await production.event(task_id, "background", "背景新版本已保存", "READY", run_id)
        continuation = await production.continue_task(task_id)
        if continuation.get("accepted"):
            background_tasks.add_task(production.launch, continuation["batch_id"])
        return {"message": "背景图片已登记，历史版本保留", "asset": asset, "continuation": continuation}

    return router
