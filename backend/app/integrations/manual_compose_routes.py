from fastapi import APIRouter, File, UploadFile
from fastapi.responses import FileResponse

from app.integrations.manual_compose import ComposeRequest, ComposeSelectionRequest, ManualReviewNote, RestoreTransformRequest, SavedCompositionRetry, TransformRequest


def create_manual_compose_router(service):
    router = APIRouter(prefix="/api/manual-compose", tags=["manual-compose"])

    @router.get("/config")
    def config():
        return {"manual_import_max_bytes": service.settings.manual_import_max_bytes}

    @router.get("/library")
    def library():
        return service.library()

    @router.get("/tasks/{task_id}/transforms")
    def transforms(task_id: str):
        return service.versions(task_id)

    @router.put("/tasks/{task_id}/selection")
    async def select_asset(task_id: str, request: ComposeSelectionRequest):
        return await service.select_asset(task_id, request)

    @router.get("/tasks/{task_id}/saved-backgrounds")
    def saved_backgrounds(task_id: str):
        return {"task_id": task_id, "saved_backgrounds": service.saved_backgrounds(task_id)}

    @router.post("/tasks/{task_id}/saved-backgrounds/prepare", status_code=202)
    async def prepare_saved_backgrounds(task_id: str):
        return await service.prepare_latest(task_id)

    @router.post("/tasks/{task_id}/saved-backgrounds/retry", status_code=202)
    async def retry_saved_background(task_id: str, request: SavedCompositionRetry):
        return await service.retry_saved_background(task_id, request.ordinal)

    @router.post("/tasks/{task_id}/transforms")
    async def apply_transform(task_id: str, request: TransformRequest):
        return await service.apply_transform(task_id, request)

    @router.post("/tasks/{task_id}/transforms/restore")
    async def restore_transform(task_id: str, request: RestoreTransformRequest):
        return await service.restore(task_id, request.person_asset_id)

    @router.post("/tasks/{task_id}/transforms/undo")
    async def undo_transform(task_id: str):
        return await service.undo(task_id)

    @router.post("/render", status_code=202)
    async def render(request: ComposeRequest):
        return await service.launch(request)

    @router.get("/runs/{run_id}")
    def run(run_id: str):
        return service.run(run_id)

    @router.get("/runs/{run_id}/preview")
    def preview(run_id: str):
        return FileResponse(service.preview(run_id), media_type="video/mp4")

    @router.post("/tasks/{task_id}/upload-person", status_code=201)
    async def upload_person(task_id: str, rgb_file: UploadFile = File(...), alpha_file: UploadFile | None = File(default=None)):
        return await service.upload_person(task_id, rgb_file, alpha_file)

    @router.post("/tasks/{task_id}/upload-background", status_code=201)
    async def upload_background(task_id: str, image: UploadFile = File(...)):
        return await service.upload_background(task_id, image)

    @router.post("/reviews/{review_id}/approve")
    async def approve(review_id: str, request: ManualReviewNote | None = None):
        return await service.approve(review_id, request.note if request else "")

    @router.post("/reviews/{review_id}/reject")
    async def reject(review_id: str, request: ManualReviewNote | None = None):
        return await service.reject(review_id, request.note if request else "")

    return router
