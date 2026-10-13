from contextlib import asynccontextmanager
from pathlib import Path
from zipfile import BadZipFile
from urllib.parse import unquote

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
from fastapi.responses import FileResponse

from app.integrations.input_service import CollectorInputService, MAX_IMAGE_BYTES, MAX_UPLOAD_BYTES
from app.integrations.contracts import FactoryCollectorRules
from app.integrations.collector_review import create_collector_review_router
from app.integrations.collector_triage import create_collector_triage_router
from app.integrations.excel_mapping import worksheet_info
from app.integrations.production_rules import read_factory_rules, save_factory_rules


async def upload_body(request: Request, limit: int) -> tuple[bytes, str]:
    filename = unquote(request.headers.get("x-filename", ""))
    if not filename or Path(filename).name != filename or "\\" in filename or "\x00" in filename or len(filename) > 240:
        raise HTTPException(422, "请通过文件选择器上传文件")
    payload = bytearray()
    async for chunk in request.stream():
        if len(payload) + len(chunk) > limit:
            raise HTTPException(413, "文件超过大小限制")
        payload.extend(chunk)
    return bytes(payload), filename


def create_input_router(collector_service, settings) -> APIRouter:
    service = CollectorInputService(collector_service, settings)
    @asynccontextmanager
    async def lifespan(_app):
        service.recover()
        yield
    router = APIRouter(prefix="/api/collector", tags=["collector-inputs"], lifespan=lifespan)
    router.input_service = service
    if hasattr(collector_service, "repository"):
        router.include_router(create_collector_review_router(collector_service))
        router.include_router(create_collector_triage_router(collector_service))

    @router.get("/inputs/config")
    def input_config():
        return service.defaults()

    @router.get("/rules")
    def get_rules():
        return read_factory_rules(settings)

    @router.put("/rules")
    def save_rules(payload: FactoryCollectorRules):
        return save_factory_rules(settings, payload.model_dump())

    @router.post("/images", status_code=201)
    async def upload_image(request: Request):
        payload, filename = await upload_body(request, MAX_IMAGE_BYTES)
        return service.save_image(payload, filename)

    @router.get("/files/{import_id}/{filename:path}")
    def product_image(import_id: str, filename: str):
        return FileResponse(service.file(import_id, filename))

    @router.post("/import/excel", status_code=202)
    async def import_excel(request: Request, background_tasks: BackgroundTasks):
        payload, filename = await upload_body(request, MAX_UPLOAD_BYTES)
        job = service.prepare_import("excel", filename=filename, payload=payload)
        background_tasks.add_task(service.launch, job["import_id"])
        return job

    @router.post("/import/tencent", status_code=202)
    async def import_tencent(payload: dict, background_tasks: BackgroundTasks):
        job = service.prepare_import("tencent", url=str(payload.get("url", "")))
        background_tasks.add_task(service.launch, job["import_id"])
        return job

    @router.get("/imports/{import_id}")
    def import_status(import_id: str):
        return service.read_status(import_id)

    @router.post("/imports/{import_id}/mapping", status_code=202)
    async def remap(import_id: str, payload: dict, background_tasks: BackgroundTasks):
        if set(payload) != {"worksheet", "column_mapping"}:
            raise HTTPException(422, "请提交工作表与字段映射")
        job = service.remap(import_id, payload["worksheet"], payload["column_mapping"])
        background_tasks.add_task(service.launch, job["import_id"])
        return job

    @router.get("/imports/{import_id}/worksheet")
    def inspect_worksheet(import_id: str, worksheet: str):
        try:
            return worksheet_info(service.directory(import_id) / "source.xlsx", worksheet)
        except (OSError, ValueError, KeyError, BadZipFile):
            raise HTTPException(422, "工作表无法读取") from None

    @router.get("/imports")
    def recent_imports(limit: int = Query(default=10, ge=1, le=100)):
        return service.recent_imports(limit)

    @router.post("/create-batch", status_code=201)
    async def create_batch(payload: dict):
        rows = payload.get("rows")
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise HTTPException(422, "rows 必须是任务行列表")
        return await service.create_batch(rows, payload.get("rules"))

    @router.get("/rules/draft")
    def get_draft():
        return service.draft()

    @router.post("/rules/draft")
    def save_draft(payload: dict):
        if not isinstance(payload.get("rules"), dict):
            raise HTTPException(422, "rules 必须是筛选规则对象")
        return service.draft(payload["rules"])

    return router
