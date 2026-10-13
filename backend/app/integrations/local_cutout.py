"""Local original import, then the existing serial split-cutout production path."""
import asyncio
import hashlib
import threading
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, UploadFile

from app.domain import audit, find, new_id, now, task_stage
from app.integrations.asset_registry import file_hash, probe_media
from app.integrations.process_runner import ProcessRunner
from app.integrations.process_probe import probe_process
from app.integrations.production_service import PRODUCTION_STAGES

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"}


class LocalCutoutService:
    def __init__(self, production, uploads):
        self.production, self.uploads = production, uploads
        self.repository, self.settings = production.repository, production.settings
        self.runner, self.jobs, self.cancels = ProcessRunner(), {}, {}

    async def import_video(self, upload, title="", country="未分类", product_id=None):
        if self.settings.app_mode != "integration":
            raise HTTPException(409, "INTEGRATION_MODE_REQUIRED")
        if Path(upload.filename or "").suffix.lower() not in VIDEO_EXTENSIONS:
            raise HTTPException(422, "请选择 MP4/MOV/MKV/WebM 原始视频")
        task_id, run_id = new_id("tsk"), new_id("run")
        label = title.strip() or Path(upload.filename).stem[:300]
        country = country.strip() or "未分类"
        context_id = hashlib.sha256((task_id + ":local-source").encode()).hexdigest()[:24]
        identifier = product_id or new_id("prd")
        def create(state):
            if product_id:
                product = find(state, "products", identifier)
                if product["country"] != country:
                    raise HTTPException(409, "PRODUCT_COUNTRY_MISMATCH")
            else:
                state["products"].append({"id": identifier, "name": "未关联产品 · 本地视频", "country": country,
                    "image": "", "url": "", "binding_reference": "", "description": "NOT_CONFIGURED：仅处理用户导入原片，不进行产品匹配或自动生成背景", "status": "inactive", "keywords": []})
            project_id = state["projects"][0]["id"]
            context = {"task_id": task_id, "project_id": project_id, "country": country, "keyword": label,
                "product_id": identifier, "product_images": [], "source_document": "local_upload", "source_row": 0, "context_id": context_id}
            state["tasks"].append({"id": task_id, "project_id": project_id, "country": country, "keyword": label,
                "product_id": identifier, "source_video_id": None, "current_stage": "download", "status": "CREATED", "progress": 0,
                "thumbnail": "", "created_at": now(), "updated_at": now(), "execution_mode": "integration",
                "context": {"task_context": context, "local_cutout": {"status": "UPLOADING", "original_filename": Path(upload.filename).name}, "production": {}}, "collector_result": {}})
            state["stages"].extend({"id": task_id + "-" + stage, "task_id": task_id, "name": stage,
                "status": "skipped" if stage in {"collector", "analysis"} else "not_connected" if stage == "publish" else "pending",
                "start_time": None, "end_time": None, "duration": 0, "retry_count": 0, "error": None} for stage in PRODUCTION_STAGES)
            state["runs"].append({"id": run_id, "task_id": task_id, "stage": "download", "provider": "FactoryLocalSourceImport",
                "status": "queued", "progress": 0, "logs": [], "created_at": now(), "last_heartbeat": now()})
            audit(state, "local_cutout.import", task_id, "用户导入本地原片；未伪造采集/AI准入结果", task_id)
        self.repository.mutate(create)
        task = find(self.repository.state(), "tasks", task_id)
        directory = self.settings.storage_root.resolve() / "projects" / task["project_id"] / "tasks" / task_id / "source" / run_id
        directory.mkdir(parents=True, exist_ok=False)
        try:
            path = await self.uploads.upload_file(upload, directory, "Original", VIDEO_EXTENSIONS)
            self.repository.mutate(lambda state: find(state, "tasks", task_id)["context"]["local_cutout"].update(
                status="VALIDATING", path=str(path), run_id=run_id, size=path.stat().st_size))
        except (HTTPException, OSError) as exc:
            self.fail(task_id, run_id, "LOCAL_UPLOAD_FAILED", str(exc))
            raise
        return {"accepted": True, "task_id": task_id, "run_id": run_id, "status": "VALIDATING", "message": "原片已保存；自动校验后进入真实抠像"}

    def fail(self, task_id, run_id, code, message):
        def change(state):
            find(state, "runs", run_id).update(status="failed", finished_at=now(), error_code=code, error_message=message)
            task = find(state, "tasks", task_id)
            task.update(status="FAILED", updated_at=now())
            task["context"]["local_cutout"].update(status="FAILED", error_code=code, message=message)
            task_stage(state, task_id, "download").update(status="failed", error=code)
        self.repository.mutate(change)

    async def launch(self, task_id, run_id):
        self.cancels[run_id] = threading.Event()
        self.jobs[run_id] = asyncio.create_task(self.validate(task_id, run_id))

    async def validate(self, task_id, run_id):
        task = find(self.repository.state(), "tasks", task_id)
        path = Path(task["context"]["local_cutout"]["path"])
        if not path.resolve().is_relative_to(self.settings.storage_root.resolve()):
            self.fail(task_id, run_id, "LOCAL_PATH_OUTSIDE_FACTORY", "原片不在 Factory storage 内")
            return
        self.repository.mutate(lambda state: find(state, "runs", run_id).update(status="running", started_at=now(), last_heartbeat=now()))
        loop = asyncio.get_running_loop()
        def event(value):
            asyncio.run_coroutine_threadsafe(self.production._process_event(task_id, run_id, "download", value), loop).result(timeout=30)
        heartbeat = asyncio.create_task(self.heartbeat(task_id, run_id))
        try:
            media = await asyncio.to_thread(probe_media, path, count_frames=True)
            if media["fps"] <= 0 or media["duration"] <= 0 or media["frame_count"] <= 0:
                raise ValueError("LOCAL_SOURCE_METADATA_INVALID")
            process = await asyncio.to_thread(self.runner.run,
                ["ffmpeg", "-v", "error", "-nostdin", "-xerror", "-i", str(path), "-map", "0:v:0", "-f", "null", "-"],
                cwd=self.settings.storage_root.resolve(), log_dir=path.parent / "logs", timeout_s=self.settings.legacy_video_timeout_seconds,
                on_event=event, cancel_event=self.cancels[run_id])
            if process.exit_code or process.cancelled:
                raise ValueError("LOCAL_SOURCE_DECODE_FAILED")
            digest = await asyncio.to_thread(file_hash, path)
            asset = self.production.registry.register(task, {"path": str(path), "sha256": digest, "source_system": "FactoryLocalUpload",
                "legacy_reference": {"local_source_import": True, "full_decode_verified": True, "original_filename": task["context"]["local_cutout"]["original_filename"]}}, "source", run_id)
            def saved(state):
                task = find(state, "tasks", task_id)
                task.update(source_video_id=new_id("source"), status="COLLECTED", current_stage="cutout", updated_at=now())
                task["context"]["local_cutout"].update(status="SOURCE_READY", source_asset_id=asset["id"], sha256=digest,
                    full_decode_verified=True, media=media)
                run = find(state, "runs", run_id)
                run.update(**{key: getattr(process, key) for key in ("pid", "exit_code", "duration_ms", "stdout_log_path", "stderr_log_path", "termination_signal", "timed_out", "resource_info")}, status="success", finished_at=now(), last_heartbeat=now(), progress=100)
                task_stage(state, task_id, "download").update(status="success", end_time=now())
                audit(state, "local_cutout.source_ready", asset["id"], "原片全程解码、ffprobe、SHA256通过；未进行AI内容准入判断", task_id)
            self.repository.mutate(saved)
            await self.start(task_id)
        except (HTTPException, OSError, ValueError, KeyError, RuntimeError) as exc:
            if isinstance(exc, HTTPException) and exc.status_code == 409:
                self.production._wait(task_id, "LOCAL_CUTOUT_DEPENDENCY_OR_QUEUE", "cutout", str(exc.detail))
            else:
                self.fail(task_id, run_id, "LOCAL_SOURCE_VALIDATION_FAILED", str(exc))
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def heartbeat(self, task_id, run_id):
        while True:
            await asyncio.sleep(5)
            self.repository.update_processing_run_event(task_id, run_id, {"last_heartbeat": now()})

    async def start(self, task_id):
        task = find(self.repository.state(), "tasks", task_id)
        if not task["context"].get("local_cutout", {}).get("full_decode_verified"):
            raise HTTPException(409, "LOCAL_SOURCE_NOT_VERIFIED")
        if task["context"].get("production", {}).get("current_person_asset_id"):
            raise HTTPException(409, "PERSON_ALREADY_EXISTS：请审核已有版本，不会重复抠像")
        response = await self.production.start([task_id], stage="cutout")
        await self.production.launch(response["batch_id"])
        return response

    async def recover(self):
        for run in self.repository.state()["runs"]:
            if run["provider"] != "FactoryLocalSourceImport" or run["status"] not in {"queued", "running"}:
                continue
            self.fail(run["task_id"], run["id"], "LOCAL_IMPORT_INTERRUPTED", "导入校验中断；已上传文件保留，继续导入会创建新 Run，不重复上传")

    async def resume_import(self, task_id):
        task = find(self.repository.state(), "tasks", task_id)
        local = task["context"].get("local_cutout", {})
        if local.get("full_decode_verified"):
            return await self.start(task_id)
        path = Path(local.get("path", ""))
        if not path.is_file() or not path.resolve().is_relative_to(self.settings.storage_root.resolve()):
            raise HTTPException(409, "LOCAL_UPLOAD_NOT_FOUND：请重新选择完整原片")
        previous = [row for row in self.repository.state()["runs"] if row["task_id"] == task_id and row["provider"] == "FactoryLocalSourceImport"]
        for run in previous:
            if run["status"] in {"queued", "running"}:
                raise HTTPException(409, "LOCAL_IMPORT_ALREADY_RUNNING")
            if run.get("pid"):
                try:
                    probe_process(run["pid"])
                except ProcessLookupError:
                    continue
                except PermissionError:
                    raise HTTPException(409, "LOCAL_IMPORT_PROCESS_STATUS_UNKNOWN") from None
                raise HTTPException(409, "LOCAL_IMPORT_PROCESS_STILL_ALIVE：等待原校验进程退出后继续")
        run_id = new_id("run")
        def queue(state):
            current = find(state, "tasks", task_id)
            current.update(status="CREATED", current_stage="download", updated_at=now())
            current["context"]["local_cutout"].update(status="VALIDATING", run_id=run_id)
            state["runs"].append({"id": run_id, "task_id": task_id, "stage": "download", "provider": "FactoryLocalSourceImport",
                "status": "queued", "progress": 0, "logs": [], "retry_count": len(previous), "created_at": now(), "last_heartbeat": now()})
        self.repository.mutate(queue)
        return {"accepted": True, "task_id": task_id, "run_id": run_id, "status": "VALIDATING"}

    async def shutdown(self):
        for cancel in self.cancels.values():
            cancel.set()
        if self.jobs:
            await asyncio.gather(*self.jobs.values(), return_exceptions=True)


def create_local_cutout_router(service):
    router = APIRouter(prefix="/api/local-cutout", tags=["local-cutout"])

    @router.post("/import", status_code=202)
    async def upload(background_tasks: BackgroundTasks, video: UploadFile = File(...), title: str = Form(default=""), country: str = Form(default="未分类"), product_id: str | None = Form(default=None)):
        result = await service.import_video(video, title, country, product_id)
        background_tasks.add_task(service.launch, result["task_id"], result["run_id"])
        return result

    @router.post("/tasks/{task_id}/start", status_code=202)
    async def start(task_id: str):
        return await service.start(task_id)

    @router.post("/tasks/{task_id}/resume-import", status_code=202)
    async def resume_import(task_id: str, background_tasks: BackgroundTasks):
        result = await service.resume_import(task_id)
        if result.get("run_id"):
            background_tasks.add_task(service.launch, task_id, result["run_id"])
        return result

    return router
