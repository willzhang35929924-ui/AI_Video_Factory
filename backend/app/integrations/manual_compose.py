"""Local composition and immutable placement versions of an existing RGB/Alpha pair."""
import asyncio
import copy
import hashlib
import json
import math
import subprocess
import threading
import time
from pathlib import Path
from typing import Literal

from fastapi import HTTPException
from pydantic import Field

from app.domain import audit, find, new_id, now, task_stage
from app.integrations.asset_registry import file_hash, probe_media
from app.integrations.process_runner import ProcessRunner
from app.integrations.quality_adapter import CutoutQualityAdapter
from app.schemas import StrictModel
from app.storage import StoragePaths

MAX_PIXELS = 8192 * 4096
MANUAL_HEARTBEAT_SECONDS = 5
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}


class TransformRequest(StrictModel):
    person_asset_id: str = Field(min_length=1, max_length=100)
    x: float = Field(default=0, allow_inf_nan=False)
    y: float = Field(default=0, allow_inf_nan=False)
    scale: float = Field(default=1, gt=0, allow_inf_nan=False)


class ComposeRequest(StrictModel):
    task_id: str = Field(min_length=1, max_length=100)
    person_asset_id: str = Field(min_length=1, max_length=100)
    background_asset_id: str = Field(min_length=1, max_length=100)
    width: int | None = Field(default=None, gt=0, le=8192)
    height: int | None = Field(default=None, gt=0, le=8192)
    preview: bool = False


class ComposeSelectionRequest(StrictModel):
    kind: Literal["person", "background"]
    asset_id: str = Field(min_length=1, max_length=100)


class RestoreTransformRequest(StrictModel):
    person_asset_id: str = Field(min_length=1, max_length=100)


class ManualReviewNote(StrictModel):
    note: str = Field(default="", max_length=2000)


class SavedCompositionRetry(StrictModel):
    ordinal: int = Field(ge=2, le=20)


def placement(values):
    return {key: float(values.get(key, 1 if key == "scale" else 0)) for key in ("x", "y", "scale")}


def is_manual_review_asset(asset):
    if not asset or asset.get("type") not in {"person", "compose", "final"}:
        return False
    reference = asset.get("legacy_reference", {})
    return reference.get("execution_scope") != "TECHNICAL_VALIDATION" and (asset.get("source_system") in {
        "FactoryTransform", "FactoryManualUpload", "FactoryManualCompose"} or reference.get("manual_compose") is True
        or reference.get("manual_upload") is True or bool(reference.get("transform_version_id")))


def compose_command(rgb, alpha, background, output, media, transform, width, height, frames):
    fit = min(width / media["width"], height / media["height"]) * transform["scale"]
    if not math.isfinite(fit) or fit > math.sqrt(MAX_PIXELS / (media["width"] * media["height"])):
        raise HTTPException(422, "COMPOSE_SIZE_LIMIT：请减小人物缩放")
    foreground_width, foreground_height = max(1, round(media["width"] * fit)), max(1, round(media["height"] * fit))
    if foreground_width * foreground_height > MAX_PIXELS or width * height > MAX_PIXELS:
        raise HTTPException(422, "COMPOSE_SIZE_LIMIT：请减小画布或人物缩放")
    graph = (f"[0:v]format=rgb24[rgb];[1:v]format=gray[alpha];[rgb][alpha]alphamerge,"
        f"scale={foreground_width}:{foreground_height}:flags=lanczos[person];"
        f"[2:v]scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},setsar=1[background];"
        f"[background][person]overlay=x=(W-w)/2+{transform['x']}:y=(H-h)/2+{transform['y']}:format=auto,format=yuv420p[out]")
    return ["ffmpeg", "-v", "error", "-nostdin", "-n", "-filter_complex_threads", "1", "-i", str(rgb), "-i", str(alpha),
        "-loop", "1", "-framerate", str(media["fps"]), "-i", str(background), "-filter_complex", graph,
        "-map", "[out]", "-map", "0:a?", "-frames:v", str(frames), "-r", str(media["fps"]),
        "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-threads", "1", "-c:a", "aac", "-b:a", "192k",
        "-af", f"apad,atrim=duration={frames / media['fps']}", "-movflags", "+faststart", str(output)]


class ManualComposeService:
    def __init__(self, production):
        self.production = production
        self.repository, self.settings, self.registry = production.repository, production.settings, production.registry
        self.runner = ProcessRunner()
        self.implementation_sha256 = file_hash(__file__)
        self.jobs = {}
        self.cancels = {}
        self.closing = False
        production.manual_compose = self

    def task(self, task_id, *, own_run=None, own_batch=None):
        state = self.repository.state()
        task = find(state, "tasks", task_id)
        if task["execution_mode"] != "integration":
            raise HTTPException(409, "REAL_TASK_REQUIRED")
        if task["status"] in {"PAUSED", "CANCELLED"} or task["context"].get("production", {}).get("requested_action") in {"pause", "cancel"}:
            raise HTTPException(409, "TASK_PAUSED_OR_CANCELLED")
        if any(row["task_id"] == task_id and row["status"] in {"queued", "running"} and row["id"] != own_run for row in state["runs"]):
            raise HTTPException(409, "TASK_ALREADY_RUNNING：等待当前执行结束后再调整")
        if any(batch["id"] != own_batch and batch["status"] in {"QUEUED", "RUNNING"} and any(item["task_id"] == task_id and item["status"] in {"QUEUED", "RUNNING"}
                for item in batch["items"]) for batch in state["batches"]):
            raise HTTPException(409, "TASK_ALREADY_RUNNING：先暂停自动生产队列，再进入手动合成")
        return task

    @staticmethod
    def enter_manual(task):
        context = task["context"]
        manual = context.setdefault("manual_compose", {})
        if context.get("production_strategy") != "manual_compose":
            manual.setdefault("previous_strategy", context.get("production_strategy"))
        manual["auto_resume_required"] = True
        context["production_strategy"] = "manual_compose"

    def checked_asset(self, task_id, asset_id, kind, *, allow_library=False):
        asset = self.repository.read_asset(asset_id)
        if asset is None:
            raise HTTPException(404, "指定资产不存在")
        if ((asset["task_id"] != task_id and not allow_library) or asset["type"] != kind or asset["registry_status"] != "registered"
                or asset["status"].lower() in {"rejected", "failed", "media_deleted"} or not self.production.business_asset(asset)):
            raise HTTPException(409, "MANUAL_COMPOSE_ASSET_NOT_ELIGIBLE")
        owner = self.repository.read_task(asset["task_id"])
        if not owner or owner["execution_mode"] != "integration":
            raise HTTPException(409, "REAL_ASSET_OWNER_REQUIRED")
        path, _ = self.registry.content(asset_id)
        if file_hash(path) != asset["sha256"]:
            raise HTTPException(409, "MANUAL_COMPOSE_INPUT_CHANGED")
        if allow_library and kind == "background":
            media = probe_media(path, count_frames=True)
            if media["codec"] not in {"png", "mjpeg", "webp"} or media["frame_count"] != 1:
                raise HTTPException(422, "BACKGROUND_IMAGE_REQUIRED：背景必须是单帧 PNG、JPEG 或 WebP 图片")
        return asset, path

    def pair(self, task, person_id, *, allow_library=False):
        person, rgb = self.checked_asset(task["id"], person_id, "person", allow_library=allow_library)
        owner = self.repository.read_task(person["task_id"])
        if allow_library and owner["context"].get("production", {}).get("current_person_asset_id") != person_id:
            raise HTTPException(409, "CURRENT_PERSON_CHANGED：只能复用来源任务当前有效的人物版本；不会恢复已拒绝或历史版本")
        reference = person["legacy_reference"]
        if not reference.get("alpha_path") or reference.get("alpha_mode") != "straight":
            raise HTTPException(409, "PERSON_ALPHA_REQUIRED：需要独立 RGB 与真实 Alpha")
        try:
            alpha = self.registry.read_path(reference["alpha_path"])
            manifest = self.registry.read_path(reference["manifest_path"])
            if file_hash(alpha) != reference["alpha_sha256"] or file_hash(manifest) != reference["manifest_sha256"]:
                raise ValueError("PERSON_EVIDENCE_CHANGED")
            verified = (reference.get("full_decode_verified") is True
                and isinstance(person.get("probe_metadata", {}).get("frame_count"), int)
                and isinstance(reference.get("alpha_metadata", {}).get("frame_count"), int))
            media = self.registry._verified_media(rgb, person["probe_metadata"]) if verified else probe_media(rgb, count_frames=True)
            evidence = self.registry._person_pair(owner, {"path": str(rgb), "legacy_reference": reference}, rgb, media, person["sha256"], verified_asset=person if verified else None)
            person = {**person, "legacy_reference": evidence["legacy_reference"]}
            data = json.loads(manifest.read_text())
            if reference.get("manual_upload"):
                if file_hash(self.registry.read_path(data["workflow_path"])) != reference["workflow_sha256"]:
                    raise ValueError("PERSON_IMPORT_RECIPE_CHANGED")
                if data.get("alpha_source_path") and file_hash(self.registry.read_path(data["alpha_source_path"])) != data["alpha_source_sha256"]:
                    raise ValueError("PERSON_ALPHA_SOURCE_CHANGED")
        except (KeyError, OSError, ValueError, subprocess.SubprocessError) as exc:
            raise HTTPException(409, "PERSON_EVIDENCE_INVALID：母资产文件、Alpha 或校验记录已变化") from exc
        if media["width"] * media["height"] > MAX_PIXELS:
            raise HTTPException(422, "COMPOSE_SIZE_LIMIT")
        return person, rgb, alpha, media

    def library(self):
        state = self.repository.state(scope="production")
        tasks = state["tasks"]
        rejected = {asset["id"] for asset in state["assets"] if asset["status"].lower() in {"rejected", "failed", "media_deleted"}}
        return {"current_person_asset_ids": {task["id"]: task["context"].get("production", {}).get("current_person_asset_id")
                    for task in tasks if task["execution_mode"] == "integration"},
                "blocked_task_ids": [task["id"] for task in tasks if task["context"].get("production", {}).get("current_person_asset_id") in rejected]}

    def versions(self, task_id):
        task = self.repository.read_task(task_id)
        if task is None:
            raise HTTPException(404, "任务不存在")
        manual = task["context"].get("manual_compose", {})
        return {"task_id": task_id, "current_person_asset_id": task["context"].get("production", {}).get("current_person_asset_id"),
                "versions": manual.get("transform_versions", []), "job": manual.get("job"),
                "saved_backgrounds_supported": True,
                "selection": manual.get("selection", {}),
                "saved_backgrounds": manual.get("saved_backgrounds"),
                "latest_composition_asset_id": manual.get("latest_composition_asset_id")}

    async def select_asset(self, task_id, request):
        async with self.production.start_lock:
            task = self.task(task_id)
            asset, _ = await asyncio.to_thread(self.checked_asset, task_id, request.asset_id, request.kind, allow_library=True)
            if request.kind == "person":
                await asyncio.to_thread(self.pair, task, asset["id"], allow_library=True)
            def change(state):
                current = find(state, "tasks", task_id)
                registered = find(state, "assets", asset["id"])
                owner = find(state, "tasks", registered["task_id"])
                if request.kind == "person" and (owner["context"].get("production", {}).get("current_person_asset_id") != asset["id"]
                        or registered["status"].lower() in {"rejected", "failed", "media_deleted"}):
                    raise HTTPException(409, "CURRENT_PERSON_CHANGED")
                selection = current["context"].setdefault("manual_compose", {}).setdefault("selection", {})
                selection[request.kind + "_asset_id"] = asset["id"]
                selection[request.kind + "_task_id"] = asset["task_id"]
                current["updated_at"] = now()
                audit(state, "manual_compose.select", asset["id"], request.kind + " 素材已选择；未启动合成、未改变审核", task_id)
            self.repository.mutate(change)
            return {"selection": self.versions(task_id)["selection"], "message": "素材选择已保存；刷新后保留，尚未开始合成"}

    async def apply_transform(self, task_id, request):
        async with self.production.start_lock:
            task = self.task(task_id)
            person, _, _, _ = await asyncio.to_thread(self.pair, task, request.person_asset_id)
            transform = placement(request.model_dump())
            identifier, version_id = new_id("ast"), new_id("transform")
            created = now()
            clone = copy.deepcopy(person)
            clone.update(id=identifier, status="READY", created_at=created, source_system="FactoryTransform")
            clone["legacy_reference"].update(parent_asset_id=person["id"], transform=transform, transform_version_id=version_id)
            record = {"id": version_id, "person_asset_id": identifier, "parent_person_asset_id": person["id"],
                "rgb_sha256": person["sha256"], "alpha_sha256": person["legacy_reference"]["alpha_sha256"], **transform, "created_at": created}
            def change(state):
                current = find(state, "tasks", task_id)
                if current["context"].get("production", {}).get("current_person_asset_id") not in {None, person["id"]}:
                    raise HTTPException(409, "CURRENT_PERSON_CHANGED")
                clone["legacy_reference"]["version"] = self.repository.next_asset_version(task_id, "person")
                state["assets"].append(clone)
                self.enter_manual(current)
                manual = current["context"].setdefault("manual_compose", {})
                manual.setdefault("transform_versions", []).append(record)
                manual["selected_transform_version_id"] = version_id
                current["context"].setdefault("production", {}).update(current_person_asset_id=identifier, review_approved=False)
                current.update(status="WAITING_REVIEW", current_stage="review", updated_at=now())
                task_stage(state, task_id, "review").update(status="waiting_review")
                state["reviews"].append({"id": new_id("review"), "task_id": task_id, "asset_id": identifier,
                    "status": "pending", "issues": ["MANUAL_TRANSFORM"], "note": ""})
                audit(state, "person.transform.apply", identifier, json.dumps(record, ensure_ascii=False), task_id)
            self.repository.mutate(change)
            return {"person": clone, "transform": record, "message": "位置已保存为新人物版本；原 RGB 与 Alpha 保留"}

    async def restore(self, task_id, person_id, *, undo=False):
        async with self.production.start_lock:
            task = self.task(task_id)
            person, _, _, _ = await asyncio.to_thread(self.pair, task, person_id)
            manual = task["context"].get("manual_compose", {})
            versions = manual.get("transform_versions", [])
            if not any(person_id in {row["person_asset_id"], row["parent_person_asset_id"]} for row in versions):
                raise HTTPException(409, "TRANSFORM_VERSION_NOT_FOUND")
            def change(state):
                current = find(state, "tasks", task_id)
                if is_manual_review_asset(person):
                    self.enter_manual(current)
                elif manual.get("previous_strategy") and manual["previous_strategy"] != "manual_compose":
                    current["context"]["production_strategy"] = manual["previous_strategy"]
                else:
                    current["context"].pop("production_strategy", None)
                current["context"]["manual_compose"]["auto_resume_required"] = True
                current["context"].setdefault("production", {}).update(current_person_asset_id=person["id"], review_approved=False)
                version = next((row for row in versions if row["person_asset_id"] == person["id"]), None)
                current["context"]["manual_compose"]["selected_transform_version_id"] = version["id"] if version else None
                current.update(status="WAITING_REVIEW", current_stage="review", updated_at=now())
                task_stage(state, task_id, "review").update(status="waiting_review")
                audit(state, "person.transform.undo" if undo else "person.transform.restore", person_id,
                    "恢复所选位置版本；母资产与历史文件保留", task_id)
            self.repository.mutate(change)
            return {"person": person, "transform": placement(person["legacy_reference"].get("transform", {})), "message": "已恢复位置版本"}

    async def undo(self, task_id):
        task = self.task(task_id)
        selected = task["context"].get("production", {}).get("current_person_asset_id")
        version = next((row for row in task["context"].get("manual_compose", {}).get("transform_versions", []) if row["person_asset_id"] == selected), None)
        if not version:
            raise HTTPException(409, "NO_TRANSFORM_TO_UNDO")
        return await self.restore(task_id, version["parent_person_asset_id"], undo=True)

    def begin(self, task, stage, provider, *, queued=False):
        run_id = new_id("run")
        directory = self.registry.allowed_path(Path(StoragePaths(self.settings.storage_root).task_paths(task["project_id"], task["id"])["metadata" if stage == "quality" else stage]) / run_id)
        if not directory.is_relative_to(self.settings.storage_root.resolve()):
            raise ValueError("MANUAL_OUTPUT_OUTSIDE_FACTORY")
        directory.mkdir(parents=True, exist_ok=False)
        def change(state):
            current = find(state, "tasks", task["id"])
            self.enter_manual(current)
            current["context"].setdefault("manual_compose", {})["job"] = {"run_id": run_id, "status": "QUEUED" if queued else "RUNNING", "stage": stage,
                "previous_status": current["status"], "previous_stage": current["current_stage"]}
            current["context"].setdefault("production", {}).update(active_run_id=run_id, active_stage=stage)
            current.update(status="RUNNING", current_stage=stage, updated_at=now())
            task_stage(state, task["id"], stage).update(status="running", start_time=now(), end_time=None, error=None)
            state["runs"].append({"id": run_id, "task_id": task["id"], "stage": stage, "provider": provider,
                "status": "queued" if queued else "running", "progress": 0, "logs": [], "created_at": now(), "started_at": None if queued else now(), "last_heartbeat": now()})
        self.repository.mutate(change)
        return run_id, directory

    def phase(self, task_id, run_id, value):
        def change(state):
            run = find(state, "runs", run_id)
            run.update(status="running", started_at=run.get("started_at") or now(), last_heartbeat=now())
            run["resource_info"] = {**run.get("resource_info", {}), "manual_compose_phase": value}
            run["logs"].append("manual_compose " + value)
            find(state, "tasks", task_id)["context"]["manual_compose"]["job"].update(status="RUNNING", phase=value)
        self.repository.mutate(change)

    async def heartbeat(self, task_id, run_id):
        while True:
            await asyncio.sleep(MANUAL_HEARTBEAT_SECONDS)
            run = await asyncio.to_thread(self.repository.read_run, run_id)
            if not run or run["status"] not in {"queued", "running"}:
                return
            await asyncio.to_thread(self.repository.update_processing_run_event, task_id, run_id, {"last_heartbeat": now()})

    async def execute(self, task_id, run_id, stage, command, directory):
        await asyncio.to_thread(self.phase, task_id, run_id, "RENDERING")
        loop = asyncio.get_running_loop()
        def callback(value):
            asyncio.run_coroutine_threadsafe(self.production._process_event(task_id, run_id, stage, value), loop).result(timeout=30)
        process = await asyncio.to_thread(self.runner.run, command, cwd=self.settings.storage_root.resolve(), log_dir=directory / "logs",
            timeout_s=self.settings.legacy_video_timeout_seconds, on_event=callback, cancel_event=self.cancels.get(run_id))
        await asyncio.to_thread(self.repository.mutate, lambda state: find(state, "runs", run_id).update(exit_code=process.exit_code, pid=process.pid,
            duration_ms=process.duration_ms, termination_signal=process.termination_signal, timed_out=process.timed_out,
            resource_info=process.resource_info or {}))
        if process.exit_code:
            raise HTTPException(422, "MANUAL_MEDIA_PROCESS_FAILED：请检查输入媒体和本地运行日志")
        return process

    def finish(self, task_id, run_id, *, manifest=None, failure=None):
        def change(state):
            run = find(state, "runs", run_id)
            run.update(status="failed" if failure else "success", finished_at=now(), progress=100 if not failure else run["progress"],
                error_code="MANUAL_COMPOSE_FAILED" if failure else None, error_message=str(failure)[:2000] if failure else None)
            run["resource_info"] = {**run.get("resource_info", {}), "manual_compose_phase": "FAILED" if failure else "READY"}
            if manifest:
                run["resource_info"] = {**run.get("resource_info", {}), "manual_compose": manifest}
            task = find(state, "tasks", task_id)
            previous = task["context"]["manual_compose"]["job"]
            if task["status"] == "RUNNING":
                task.update(status="FAILED" if failure else previous["previous_status"],
                    current_stage=run["stage"] if failure else previous["previous_stage"], updated_at=now())
            production = task["context"].get("production", {})
            if production.get("active_run_id") == run_id:
                production.update(active_run_id=None, active_stage=None)
            task_stage(state, task_id, run["stage"]).update(status="failed" if failure else "success", end_time=now(),
                error=str(failure)[:2000] if failure else None)
            task["context"]["manual_compose"]["job"] = {"run_id": run_id, "status": "FAILED" if failure else "READY", "manifest": manifest}
        self.repository.mutate(change)

    async def launch(self, request):
        if self.closing:
            raise HTTPException(409, "MANUAL_COMPOSE_SHUTTING_DOWN")
        async with self.production.start_lock:
            task = self.task(request.task_id)
            state = self.repository.state()
            for identifier, kind in ((request.person_asset_id, "person"), (request.background_asset_id, "background")):
                asset = find(state, "assets", identifier)
                if (asset["type"] != kind or asset["registry_status"] != "registered" or asset["status"].lower() in {"rejected", "failed", "media_deleted"}
                        or not self.production.business_asset(asset)):
                    raise HTTPException(409, "MANUAL_COMPOSE_ASSET_NOT_ELIGIBLE")
                owner = find(state, "tasks", asset["task_id"])
                if owner["execution_mode"] != "integration":
                    raise HTTPException(409, "REAL_ASSET_OWNER_REQUIRED")
                if kind == "person" and owner["context"].get("production", {}).get("current_person_asset_id") != asset["id"]:
                    raise HTTPException(409, "CURRENT_PERSON_CHANGED")
            run_id, directory = self.begin(task, "compose", "FactoryManualCompose", queued=True)
            self.cancels[run_id] = threading.Event()
            self.jobs[run_id] = asyncio.create_task(self._background_render(request, run_id, directory), name="manual-compose-" + run_id)
        return {"run_id": run_id, "status": "QUEUED", "preview": request.preview, "run_url": f"/api/manual-compose/runs/{run_id}", "message": "本地合成已排队，使用 Run 查看真实执行与校验结果"}

    async def _background_render(self, request, run_id, directory):
        heartbeat = asyncio.create_task(self.heartbeat(request.task_id, run_id), name="manual-heartbeat-" + run_id)
        try:
            await self.render(request, run_id=run_id, directory=directory)
        except HTTPException:
            pass
        except Exception as exc:
            await asyncio.to_thread(self.finish, request.task_id, run_id, failure=f"{type(exc).__name__}: {exc}")
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            self.jobs.pop(run_id, None)
            self.cancels.pop(run_id, None)

    async def shutdown(self):
        self.closing = True
        for cancel in self.cancels.values():
            cancel.set()
        if self.jobs:
            await asyncio.gather(*list(self.jobs.values()), return_exceptions=True)

    def run(self, run_id):
        run = self.repository.read_run(run_id)
        if run is None:
            raise HTTPException(404, "指定记录不存在")
        if run["provider"] != "FactoryManualCompose":
            raise HTTPException(404, "MANUAL_COMPOSE_RUN_NOT_FOUND")
        manifest = run.get("resource_info", {}).get("manual_compose")
        result = None
        if run["status"] == "success" and manifest:
            asset = next((row for row in self.repository.read_assets(task_id=run["task_id"]) if row["type"] == "compose" and row["legacy_reference"].get("processing_run_id") == run_id), None)
            review = next((row for row in self.repository.read_reviews(run["task_id"]) if row["asset_id"] == asset["id"]), None) if asset else None
            result = {"run_id": run_id, "status": "READY", "preview": manifest["mode"] == "PREVIEW", "asset": asset, "review": review,
                "manifest": manifest, "preview_url": f"/api/manual-compose/runs/{run_id}/preview"}
        return {"run_id": run_id, "status": {"queued": "QUEUED", "running": "RUNNING", "success": "READY", "failed": "FAILED"}[run["status"]],
            "phase": run.get("resource_info", {}).get("manual_compose_phase"), "run": run, "result": result}

    async def render(self, request, *, run_id=None, directory=None, own_batch=None, saved_intent_id=None):
        started = time.monotonic()
        try:
            if run_id:
                await asyncio.to_thread(self.phase, request.task_id, run_id, "VALIDATING")
            async with self.production.start_lock:
                task = await asyncio.to_thread(self.task, request.task_id, own_run=run_id, own_batch=own_batch)
                if own_batch:
                    saved = task["context"].get("manual_compose", {}).get("saved_backgrounds", {})
                    if (saved.get("id") != saved_intent_id or saved.get("status") not in {"AUTHORIZED", "RUNNING"}
                            or task["context"]["production"].get("batch_id") != own_batch):
                        raise HTTPException(409, "SAVED_BACKGROUND_INTENT_CHANGED")
                if own_batch and task["context"].get("production", {}).get("current_person_asset_id") != request.person_asset_id:
                    raise HTTPException(409, "CURRENT_PERSON_CHANGED：原自动生产人物版本已变化")
                person, rgb, alpha, media = await asyncio.to_thread(self.pair, task, request.person_asset_id, allow_library=True)
                background, path = await asyncio.to_thread(self.checked_asset, task["id"], request.background_asset_id, "background", allow_library=True)
                if own_batch:
                    expected = next((row for row in saved["items"] if row["background_asset_id"] == background["id"]), None)
                    if not expected or expected["background_sha256"] != background["sha256"]:
                        raise HTTPException(409, "SAVED_BACKGROUND_INPUT_CHANGED")
                image = await asyncio.to_thread(probe_media, path, count_frames=True)
                if image["codec"] not in {"png", "mjpeg", "webp"} or image["frame_count"] != 1:
                    raise HTTPException(422, "BACKGROUND_IMAGE_REQUIRED")
                settings = (await asyncio.to_thread(self.repository.state))["settings"]
                width = request.width or settings["output_width"]
                height = request.height or settings["output_height"]
                width, height = width + width % 2, height + height % 2
                transform = placement(TransformRequest(person_asset_id=person["id"],
                    **(person["legacy_reference"].get("transform") or {})).model_dump())
                frames = min(media["frame_count"], 24) if request.preview else media["frame_count"]
                if not run_id:
                    run_id, directory = await asyncio.to_thread(self.begin, task, "compose", "FactoryManualCompose")
            output = self.registry.allowed_path(directory / ("Preview.mp4" if request.preview else "Composite.mp4"))
            if not output.is_relative_to(self.settings.storage_root.resolve()):
                raise ValueError("MANUAL_OUTPUT_OUTSIDE_FACTORY")
            command = compose_command(rgb, alpha, path, output, media, transform, width, height, frames)
            await self.execute(task["id"], run_id, "compose", command, directory)
            await asyncio.to_thread(self.phase, task["id"], run_id, "VERIFYING_OUTPUT")
            checked = await asyncio.to_thread(probe_media, output, count_frames=True)
            if (checked["frame_count"] != frames or (checked["width"], checked["height"]) != (width, height) or abs(checked["fps"] - media["fps"]) > .001
                    or abs(checked["duration"] - frames / media["fps"]) > 1 / media["fps"]
                    or abs(float(checked.get("video_start_time") or 0)) > 1 / media["fps"]):
                raise ValueError("MANUAL_COMPOSE_OUTPUT_ALIGNMENT_MISMATCH")
            if not request.preview and media["audio"] and not checked["audio"]:
                raise ValueError("MANUAL_COMPOSE_AUDIO_MISSING")
            await asyncio.to_thread(self.phase, task["id"], run_id, "VERIFYING_INPUTS")
            current = await asyncio.to_thread(self.task, task["id"], own_run=run_id, own_batch=own_batch)
            await asyncio.to_thread(self.pair, current, person["id"], allow_library=True)
            await asyncio.to_thread(self.checked_asset, task["id"], background["id"], "background", allow_library=True)
            manifest = {"schema": "factory.manual-composition.v1", "status": "READY", "mode": "PREVIEW" if request.preview else "COMPOSITION",
                "task_id": task["id"], "processing_run_id": run_id, "person_asset_id": person["id"], "background_asset_id": background["id"],
                "person_task_id": person["task_id"], "background_task_id": background["task_id"],
                "rgb_sha256": person["sha256"], "alpha_sha256": person["legacy_reference"]["alpha_sha256"], "background_sha256": background["sha256"],
                "transform": transform, "source_media": media, "output_media": checked, "output_sha256": file_hash(output),
                "output_path": str(output), "full_decode_verified": True, "input_verification": "registered_sha256_and_stream_metadata",
                "saved_background_intent_id": saved_intent_id,
                "saved_background_step_key": expected["step_key"] if own_batch else None,
                "cutout_inference_executions": 0, "command": command}
            manifest_path = self.registry.allowed_path(directory / "manifest.json")
            if not manifest_path.is_relative_to(self.settings.storage_root.resolve()):
                raise ValueError("MANUAL_OUTPUT_OUTSIDE_FACTORY")
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
            asset, review = None, None
            if not request.preview:
                fingerprint = hashlib.sha256(json.dumps({**transform, "person": person["id"], "background": background["sha256"],
                    "width": width, "height": height, "implementation": self.implementation_sha256}, sort_keys=True).encode()).hexdigest()
                asset = await asyncio.to_thread(self.registry.register, task, {"path": str(output), "sha256": manifest["output_sha256"], "source_system": "FactoryManualCompose",
                    "legacy_reference": {"manual_compose": True, "transform": transform, "manifest_path": str(manifest_path),
                        "person_task_id": person["task_id"], "background_task_id": background["task_id"],
                        "manifest_sha256": file_hash(manifest_path), "factory_fingerprint": fingerprint,
                        "saved_background_intent_id": saved_intent_id,
                        "saved_background_step_key": expected["step_key"] if own_batch else None}}, "compose", run_id, person=person, background=background)
                review = {"id": new_id("review"), "task_id": task["id"], "asset_id": asset["id"], "status": "pending", "issues": ["MANUAL_COMPOSITION"], "note": ""}
                def register_review(state):
                    state["reviews"].append(review)
                    current = find(state, "tasks", task["id"])
                    current.update(status="WAITING_REVIEW", current_stage="review", updated_at=now())
                    current["context"]["manual_compose"]["latest_composition_asset_id"] = asset["id"]
                    current["context"]["manual_compose"]["selection"] = {"person_asset_id": person["id"], "background_asset_id": background["id"],
                        "person_task_id": person["task_id"], "background_task_id": background["task_id"]}
                    task_stage(state, task["id"], "compose").update(status="success", end_time=now(), duration=time.monotonic() - started)
                    task_stage(state, task["id"], "review").update(status="waiting_review")
                    audit(state, "manual_compose.render", asset["id"], "真实本地合成已验证，等待成片审核；抠像执行次数 0", task["id"])
                await asyncio.to_thread(self.repository.mutate, register_review)
            await asyncio.to_thread(self.finish, task["id"], run_id, manifest=manifest)
            if not request.preview and not own_batch:
                await self.prepare_saved_backgrounds(task, asset, review, manifest)
            return {"run_id": run_id, "status": "READY", "preview": request.preview, "preview_url": f"/api/manual-compose/runs/{run_id}/preview",
                "asset": asset, "review": review, "manifest": manifest, "message": "短片预览已生成" if request.preview else "合成已完成，等待审核"}
        except Exception as exc:
            if run_id:
                await asyncio.to_thread(self.finish, request.task_id, run_id, failure=exc)
            if isinstance(exc, HTTPException):
                raise
            raise HTTPException(422, "MANUAL_COMPOSE_VERIFICATION_FAILED：请检查运行日志和输入媒体") from exc

    def preview(self, run_id):
        run = self.repository.read_run(run_id)
        if not run:
            raise HTTPException(404, "MANUAL_PREVIEW_NOT_READY")
        manifest = run.get("resource_info", {}).get("manual_compose", {})
        if run["provider"] != "FactoryManualCompose" or run["status"] != "success" or not manifest:
            raise HTTPException(404, "MANUAL_PREVIEW_NOT_READY")
        path = self.registry.allowed_path(manifest["output_path"])
        if not path.is_file() or file_hash(path) != manifest["output_sha256"]:
            raise HTTPException(409, "MANUAL_PREVIEW_CHANGED")
        return path

    def saved_backgrounds(self, task_id):
        return self.versions(task_id)["saved_backgrounds"]

    def _saved_plan(self, task, asset, review, manifest):
        if any(manifest.get(key, task["id"]) != task["id"] for key in ("person_task_id", "background_task_id")):
            return None
        context, result = task["context"].get("task_context", {}), task.get("collector_result", {})
        if (not result.get("video_id") or result.get("context_id") != context.get("context_id")
                or task["context"].get("local_cutout") or not context.get("source_document")
                or not context.get("product_id") or context.get("country") != task["country"]
                or context.get("keyword") != task["keyword"]):
            return None
        state = self.repository.state()
        person = find(state, "assets", manifest["person_asset_id"])
        source = next((row for row in reversed(state["assets"]) if row["task_id"] == task["id"]
            and row["type"] == "source" and row["registry_status"] == "registered"
            and row.get("legacy_video_id") == result["video_id"] and self.production.business_asset(row)), None)
        if not source or person["legacy_reference"].get("source_sha256") != source["sha256"]:
            return None
        saved = task["context"].get("manual_compose", {}).get("saved_backgrounds") or {}
        if saved.get("first_composition_asset_id") == asset["id"] and saved.get("status") != "WAITING_INPUTS":
            if saved.get("implementation_sha256") == self.implementation_sha256:
                return saved
            if any(row.get("attempts") or row.get("asset_id") for row in saved.get("items", [])[1:]):
                raise HTTPException(409, "SAVED_COMPOSE_IMPLEMENTATION_CHANGED：已执行部分背景，保留原检查点；不能自动换版本重跑")
        prompts = {row["ordinal"]: row for row in state["prompts"] if row["task_id"] == task["id"] and row.get("ordinal")}
        prompt_result = task["context"].get("production", {}).get("results", {}).get("prompt", {})
        missing = (set(prompts) != set(range(1, 21)) or prompt_result.get("status") != "READY"
            or prompt_result.get("context_id") != context["context_id"] or any(row["status"] != "READY"
                or row.get("processing_run_id") != prompt_result.get("run_id") for row in prompts.values()))
        items = []
        for ordinal, prompt in sorted(prompts.items()):
            background = next((row for row in reversed(state["assets"]) if row["task_id"] == task["id"]
                and row["type"] == "background" and row["registry_status"] == "registered" and row["status"] not in {"failed", "rejected"}
                and self.production.business_asset(row) and row["legacy_reference"].get("prompt_id") == prompt["id"]
                and row["legacy_reference"].get("context_id") == context["context_id"]
                and row["legacy_reference"].get("video_id") == result["video_id"]), None)
            if background:
                try:
                    self.checked_asset(task["id"], background["id"], "background")
                except (HTTPException, OSError, ValueError):
                    continue
                items.append({"ordinal": ordinal, "prompt_id": prompt["id"], "prompt_fingerprint": prompt["fingerprint"],
                    "background_asset_id": background["id"], "background_sha256": background["sha256"],
                    "status": "READY" if ordinal == 1 else "QUEUED", "attempts": [],
                    **({"run_id": manifest["processing_run_id"], "asset_id": asset["id"], "review_id": review["id"],
                        "output_sha256": asset["sha256"]} if ordinal == 1 else {})})
        if (missing or len(items) != 20 or len({row["background_sha256"] for row in items}) != 20
                or items[0]["background_asset_id"] != manifest["background_asset_id"]):
            return {"id": new_id("saved-compose"), "status": "WAITING_INPUTS", "reason": "SAVED_20_BACKGROUND_SOURCE_ASSOCIATION_REQUIRED",
                "first_composition_asset_id": asset["id"], "first_review_id": review["id"], "person_asset_id": person["id"],
                "items": [{**{key: value for key, value in row.items() if key in {"ordinal", "prompt_id", "background_asset_id", "background_sha256"}}, "status": "WAITING_INPUTS"}
                    for row in items],
                "message": "需要同一正常任务的20条提示词及20张独立、来源关联明确的已登记背景；不会补发付费请求", "created_at": now()}
        basis = {"task_id": task["id"], "context_id": context["context_id"], "video_id": result["video_id"],
            "source_asset_id": source["id"], "source_sha256": source["sha256"], "person_asset_id": person["id"],
            "rgb_sha256": person["sha256"], "alpha_sha256": person["legacy_reference"]["alpha_sha256"],
            "person_manifest_sha256": person["legacy_reference"]["manifest_sha256"], "transform": manifest["transform"],
            "transform_version_id": person["legacy_reference"].get("transform_version_id"),
            "first_composition_asset_id": asset["id"], "first_composition_sha256": asset["sha256"],
            "first_manifest_sha256": asset["legacy_reference"]["manifest_sha256"], "first_review_id": review["id"],
            "width": manifest["output_media"]["width"], "height": manifest["output_media"]["height"],
            "implementation_sha256": self.implementation_sha256}
        fingerprint = hashlib.sha256(json.dumps({**basis, "items": items}, sort_keys=True).encode()).hexdigest()
        for item in items:
            item["step_key"] = hashlib.sha256((fingerprint + ":" + str(item["ordinal"]) + ":" + item["background_sha256"]).encode()).hexdigest()
        return {"id": new_id("saved-compose"), "status": "WAITING_FIRST_REVIEW", "fingerprint": fingerprint, **basis,
            "items": items, "created_at": now(), "external_requests": 0, "cutout_inference_executions": 0,
            "message": "亲自通过首条完整成片后，原生产队列自动复用当前人物及其余19张已保存背景；不付费、不重新抠像"}

    async def prepare_saved_backgrounds(self, task, asset, review, manifest):
        saved = await asyncio.to_thread(self._saved_plan, task, asset, review, manifest)
        if saved is None:
            return None
        def persist(state):
            current = find(state, "tasks", task["id"])
            if (current["context"]["production"].get("current_person_asset_id") != manifest["person_asset_id"]
                    or current["context"]["manual_compose"].get("latest_composition_asset_id") != asset["id"]):
                raise HTTPException(409, "CURRENT_FULL_COMPOSITION_CHANGED：人物版本或最新成片已变化，未保存续跑意图")
            previous = current["context"]["manual_compose"].get("saved_backgrounds")
            if previous and previous.get("id") != saved.get("id"):
                current["context"]["manual_compose"].setdefault("saved_background_history", []).append(previous)
            current["context"]["manual_compose"]["saved_backgrounds"] = saved
            audit(state, "manual_compose.saved_backgrounds_prepared", saved.get("id", asset["id"]), saved["message"], task["id"])
        await asyncio.to_thread(self.repository.mutate, persist)
        if saved.get("status") == "WAITING_FIRST_REVIEW" and not saved.get("quality"):
            await self.schedule_saved_quality(task["id"], saved["id"])
        return saved

    async def prepare_latest(self, task_id):
        async with self.production.start_lock:
            task = await asyncio.to_thread(self.task, task_id)
            asset, _ = await asyncio.to_thread(self.checked_asset, task_id, task["context"].get("manual_compose", {}).get("latest_composition_asset_id"), "compose")
            reference = asset["legacy_reference"]
            path = self.registry.read_path(reference["manifest_path"])
            if file_hash(path) != reference["manifest_sha256"]:
                raise HTTPException(409, "MANUAL_COMPOSITION_MANIFEST_CHANGED")
            manifest = json.loads(path.read_text())
            if (manifest.get("person_asset_id") != task["context"]["production"].get("current_person_asset_id")
                    or manifest.get("mode") != "COMPOSITION"):
                raise HTTPException(409, "CURRENT_FULL_COMPOSITION_REQUIRED")
            review = next((row for row in await asyncio.to_thread(self.repository.read_reviews, task_id) if row["asset_id"] == asset["id"]), None)
            if not review or review["status"] not in {"pending", "approved"}:
                raise HTTPException(409, "CURRENT_FIRST_REVIEW_REQUIRED")
            if review["status"] == "approved":
                await asyncio.to_thread(self._approved_first_evidence, task, asset, review)
        saved = await self.prepare_saved_backgrounds(task, asset, review, manifest)
        if saved is None:
            raise HTTPException(409, "SAVED_BACKGROUND_SOURCE_CONTEXT_REQUIRED：当前成片不是关联正常采集原片的任务；手动导入仍保持单条合成")
        return {"task_id": task_id, "saved_backgrounds": self.saved_backgrounds(task_id),
            "message": "仅核验现有成片、来源与20张背景；质检使用本地真实分析/同Alpha缓存，不重生成成片或背景"}

    def _approved_first_evidence(self, task, asset, review):
        state = self.repository.state()
        final = next((row for row in state["assets"] if row["task_id"] == task["id"] and row["type"] == "final"
            and row["source_system"] == "HumanReview" and row["registry_status"] == "registered"
            and row["legacy_reference"].get("review_id") == review["id"]
            and row["legacy_reference"].get("composition_asset_id") == asset["id"]
            and row["legacy_reference"].get("person_asset_id") == asset["legacy_reference"].get("person_asset_id")
            and row["sha256"] == asset["sha256"]), None)
        run = next((row for row in state["runs"] if final and row["id"] == final["legacy_reference"].get("processing_run_id")
            and row["task_id"] == task["id"] and row["provider"] == "HumanReview" and row["status"] == "success"), None)
        if (review["status"] != "approved" or not final or not run
                or not any(row["action"] == "review.approve" and row["entity_id"] == review["id"] and row["task_id"] == task["id"] for row in state["audits"])):
            raise HTTPException(409, "FIRST_PERSONAL_APPROVAL_EVIDENCE_REQUIRED")
        self.checked_asset(task["id"], final["id"], "final")
        return final

    async def schedule_saved_quality(self, task_id, intent_id):
        async with self.production.start_lock:
            task = await asyncio.to_thread(self.task, task_id)
            saved = task["context"].get("manual_compose", {}).get("saved_backgrounds") or {}
            if saved.get("id") != intent_id or saved.get("status") != "WAITING_FIRST_REVIEW" or not saved.get("alpha_sha256"):
                raise HTTPException(409, "SAVED_BACKGROUND_INPUTS_REQUIRED：续跑输入不完整，未启动质检")
            run_id, directory = await asyncio.to_thread(self.begin, task, "quality", "OpenCVCutoutQualityAnalyzer", queued=True)
            await asyncio.to_thread(self._saved_item_update, task_id, intent_id, quality={"status": "QUEUED", "run_id": run_id})
            self.cancels[run_id] = threading.Event()
            self.jobs[run_id] = asyncio.create_task(self._saved_quality(task_id, intent_id, run_id, directory))

    async def _saved_quality(self, task_id, intent_id, run_id, directory):
        heartbeat = asyncio.create_task(self.heartbeat(task_id, run_id))
        try:
            task = await asyncio.to_thread(self.repository.read_task, task_id)
            saved = task["context"]["manual_compose"]["saved_backgrounds"]
            person = await asyncio.to_thread(self.repository.read_asset, saved["person_asset_id"])
            policy = await asyncio.to_thread(self.production.policy)
            adapter = CutoutQualityAdapter(self.settings, thresholds=policy["quality_thresholds"])
            loop = asyncio.get_running_loop()
            def callback(value):
                asyncio.run_coroutine_threadsafe(self.production._process_event(task_id, run_id, "quality", value), loop).result(timeout=30)
            result = await adapter.analyze(person, directory, callback, self.cancels.get(run_id))
            if result.get("status") not in {"PASS", "FAIL"}:
                raise HTTPException(409, result.get("error_code", "QUALITY_UNKNOWN"))
            report = self.registry.read_path(result["report_path"])
            report_hash = await asyncio.to_thread(file_hash, report)
            evidence = {"status": result["status"], "geometry_status": result.get("geometry_status"),
                "semantic_status": result.get("semantic_status", "UNKNOWN"), "run_id": run_id,
                "fingerprint": result["fingerprint"], "input_sha256": result["input_sha256"],
                "report_path": str(report), "report_sha256": report_hash, "cache_hit": result.get("cache_hit", False)}
            def record(state):
                selected = find(state, "tasks", task_id)
                intent = selected["context"]["manual_compose"]["saved_backgrounds"]
                if intent["id"] != intent_id or result["input_sha256"] != intent["alpha_sha256"]:
                    raise HTTPException(409, "QUALITY_INPUT_CHANGED")
                intent["quality"] = evidence
                review = find(state, "reviews", intent["first_review_id"])
                if review["status"] == "approved":
                    intent.update(status="AUTHORIZED", approved_review_id=review["id"], approved_at=now())
                selected.update(status="WAITING_REVIEW", current_stage="review", updated_at=now())
                selected["context"]["production"].setdefault("results", {})["quality"] = {**result, "run_id": run_id}
                state["quality_assessments"].append({"id": new_id("quality"), "task_id": task_id, "asset_id": person["id"],
                    "processing_run_id": run_id, "status": result["status"], "quality_score": result.get("quality_score"),
                    "metrics": {**result.get("metrics", {}), **evidence, "composition_asset_id": intent["first_composition_asset_id"]},
                    "issue_segments": result.get("issue_segments", []), "fingerprint": result["fingerprint"], "created_at": now()})
                find(state, "runs", run_id).update(**{k: v for k, v in result.get("_process", {}).items()
                    if k in {"pid", "exit_code", "stdout_log_path", "stderr_log_path", "command", "cwd", "duration_ms"}})
            await asyncio.to_thread(self.repository.mutate, record)
            await asyncio.to_thread(self.finish, task_id, run_id, manifest={**result, "execution": {"mode": "CACHE_REUSE" if result.get("cache_hit") else "PROCESS",
                "report_sha256": report_hash}}, failure="QUALITY_GEOMETRY_FAIL" if result["status"] == "FAIL" else None)
        except Exception as exc:
            await asyncio.to_thread(self._saved_item_update, task_id, intent_id, quality={"status": "UNKNOWN", "run_id": run_id, "error": str(exc)[:2000]})
            await asyncio.to_thread(self.finish, task_id, run_id, failure=exc)
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            self.jobs.pop(run_id, None)
            self.cancels.pop(run_id, None)
        task = await asyncio.to_thread(self.repository.read_task, task_id)
        saved = task["context"]["manual_compose"]["saved_backgrounds"]
        if saved.get("id") == intent_id and saved.get("status") == "AUTHORIZED" and saved.get("quality", {}).get("status") in {"PASS", "FAIL"}:
            await self._continue_saved(task_id)

    async def _continue_saved(self, task_id):
        response = await self.production.continue_task(task_id, stage="saved_background_compose")
        if response.get("accepted"):
            await self.production.launch(response["batch_id"])
        return response

    async def retry_saved_background(self, task_id, ordinal):
        async with self.production.start_lock:
            task = await asyncio.to_thread(self.task, task_id)
            saved = await asyncio.to_thread(self.validate_saved_backgrounds, task)
            item = next(row for row in saved["items"] if row["ordinal"] == ordinal)
            if item["status"] != "FAILED":
                raise HTTPException(409, "SAVED_COMPOSITION_NOT_FAILED：完成项不重跑，UNKNOWN需先核实")
            await asyncio.to_thread(self._saved_item_update, task_id, saved["id"], ordinal, status="QUEUED", previous_error=item.get("error"))
            await asyncio.to_thread(self._saved_item_update, task_id, saved["id"], status="AUTHORIZED")
        return await self._continue_saved(task_id)

    def validate_saved_backgrounds(self, task, *, require_approval=True):
        saved = task["context"].get("manual_compose", {}).get("saved_backgrounds", {})
        if (not saved.get("id") or saved.get("status") not in {"WAITING_FIRST_REVIEW", "AUTHORIZED", "RUNNING", "WAITING_FINAL_REVIEW", "COMPLETED_WITH_ERRORS"}
                or task["context"]["production"].get("current_person_asset_id") != saved["person_asset_id"]
                or saved["implementation_sha256"] != self.implementation_sha256):
            raise HTTPException(409, "SAVED_BACKGROUND_IDENTITY_CHANGED")
        person, _, _, _ = self.pair(task, saved["person_asset_id"])
        if (person["sha256"] != saved["rgb_sha256"] or person["legacy_reference"]["alpha_sha256"] != saved["alpha_sha256"]
                or person["legacy_reference"]["manifest_sha256"] != saved["person_manifest_sha256"]
                or placement(person["legacy_reference"].get("transform", {})) != saved["transform"]):
            raise HTTPException(409, "SAVED_PERSON_INPUT_CHANGED")
        source, _ = self.checked_asset(task["id"], saved["source_asset_id"], "source")
        if source["sha256"] != saved["source_sha256"]:
            raise HTTPException(409, "SAVED_SOURCE_INPUT_CHANGED")
        reviews = self.repository.read_reviews(task["id"])
        first_review = next((row for row in reviews if row["id"] == saved["first_review_id"]
            and row["asset_id"] == saved["first_composition_asset_id"]), None)
        if require_approval and (not first_review or first_review["status"] != "approved" or saved.get("approved_review_id") != first_review["id"]):
            raise HTTPException(409, "FIRST_COMPOSITION_REVIEW_REQUIRED")
        if require_approval:
            self._approved_first_evidence(task, self.repository.read_asset(saved["first_composition_asset_id"]), first_review)
        quality = saved.get("quality", {})
        if (quality.get("status") not in {"PASS", "FAIL"} or quality.get("input_sha256") != saved["alpha_sha256"]
                or file_hash(self.registry.read_path(quality["report_path"])) != quality["report_sha256"]):
            raise HTTPException(409, "FIRST_COMPOSITION_QUALITY_REQUIRED：真实质检尚未确认；不把UNKNOWN当通过")
        report = json.loads(self.registry.read_path(quality["report_path"]).read_text())
        adapter = CutoutQualityAdapter(self.settings, thresholds=self.production.policy()["quality_thresholds"])
        if (report.get("mode") != "REAL" or report.get("provider") != adapter.provider
                or report.get("input_sha256") != saved["alpha_sha256"] or report.get("fingerprint") != quality["fingerprint"]
                or report.get("thresholds") != adapter.thresholds or report.get("status") != quality["status"]):
            raise HTTPException(409, "FIRST_COMPOSITION_QUALITY_EVIDENCE_CHANGED")
        first = self.verify_saved_output(task, saved, saved["items"][0])
        if first["sha256"] != saved["first_composition_sha256"] or first["legacy_reference"]["manifest_sha256"] != saved["first_manifest_sha256"]:
            raise HTTPException(409, "FIRST_COMPOSITION_EVIDENCE_CHANGED")
        return saved

    def verify_saved_output(self, task, saved, item):
        asset, output = self.checked_asset(task["id"], item["asset_id"], "compose")
        reference = asset["legacy_reference"]
        manifest_path = self.registry.read_path(reference["manifest_path"])
        if file_hash(manifest_path) != reference["manifest_sha256"]:
            raise HTTPException(409, "SAVED_COMPOSITION_MANIFEST_CHANGED")
        manifest = json.loads(manifest_path.read_text())
        media = probe_media(output, count_frames=True)
        run = self.repository.read_run(reference["processing_run_id"])
        if (manifest.get("schema") != "factory.manual-composition.v1" or manifest.get("mode") != "COMPOSITION"
                or manifest.get("status") != "READY" or manifest.get("full_decode_verified") is not True
                or manifest.get("task_id") != task["id"] or manifest.get("person_asset_id") != saved["person_asset_id"]
                or manifest.get("background_asset_id") != item["background_asset_id"] or manifest.get("transform") != saved["transform"]
                or manifest.get("rgb_sha256") != saved["rgb_sha256"] or manifest.get("alpha_sha256") != saved["alpha_sha256"]
                or manifest.get("background_sha256") != item["background_sha256"] or manifest.get("output_sha256") != asset["sha256"]
                or self.registry.read_path(manifest["output_path"]) != output
                or not run or run["provider"] != "FactoryManualCompose" or run["status"] != "success" or run.get("exit_code") != 0
                or not run.get("pid") or manifest.get("processing_run_id") != run["id"]
                or media != manifest.get("output_media") or (media["width"], media["height"]) != (saved["width"], saved["height"])
                or media["frame_count"] != manifest["source_media"]["frame_count"] or abs(media["fps"] - manifest["source_media"]["fps"]) > .001
                or manifest["source_media"]["audio"] and not media["audio"]
                or item.get("output_sha256") not in {None, asset["sha256"]}):
            raise HTTPException(409, "SAVED_COMPOSITION_EVIDENCE_INVALID")
        return asset

    def _saved_item_update(self, task_id, intent_id, ordinal=None, **values):
        def change(state):
            task = find(state, "tasks", task_id)
            saved = task["context"]["manual_compose"]["saved_backgrounds"]
            if saved.get("id") != intent_id:
                raise HTTPException(409, "SAVED_BACKGROUND_INTENT_CHANGED")
            row = next(row for row in saved["items"] if row["ordinal"] == ordinal) if ordinal else saved
            row.update(**values, updated_at=now())
            task["updated_at"] = now()
        self.repository.mutate(change)

    async def compose_saved_backgrounds(self, batch_id, task_id):
        from app.integrations.production_service import ProductionStopped, TaskStopped

        task = await asyncio.to_thread(self.repository.read_task, task_id)
        saved = await asyncio.to_thread(self.validate_saved_backgrounds, task)
        await self.production._saved_context(task)
        if any(run.get("error_code") == "UNKNOWN_EXTERNAL_REQUEST" and run["task_id"] == task_id for run in (await asyncio.to_thread(self.repository.state))["runs"]):
            await asyncio.to_thread(self.production._wait, task_id, "UNKNOWN_EXTERNAL_REQUEST", "compose", "外部未知请求未确认；只保存本地续跑意图，不重交")
            return
        await asyncio.to_thread(self._saved_item_update, task_id, saved["id"], status="RUNNING")
        for item in saved["items"][1:]:
            await self.production._task_checkpoint(batch_id, task_id)
            current = await asyncio.to_thread(self.repository.read_task, task_id)
            if current["context"]["production"].get("current_person_asset_id") != saved["person_asset_id"]:
                await asyncio.to_thread(self.production._wait, task_id, "CURRENT_PERSON_CHANGED", "compose", "人物版本已变化，原成片保留；不会使用旧位置继续")
                return
            if item["status"] in {"FAILED", "UNKNOWN"}:
                continue
            try:
                if item["status"] == "READY":
                    await asyncio.to_thread(self.verify_saved_output, current, saved, item)
                    continue
                # A registered output after a crash is reconciled, never copied or re-rendered.
                candidates = await asyncio.to_thread(self.repository.read_assets, task_id=task_id)
                recovered = next((row for row in candidates if row["type"] == "compose"
                    and row["legacy_reference"].get("saved_background_intent_id") == saved["id"]
                    and row["legacy_reference"].get("background_asset_id") == item["background_asset_id"]
                    and row["legacy_reference"].get("person_asset_id") == saved["person_asset_id"]), None)
                if recovered:
                    item.update(asset_id=recovered["id"], output_sha256=recovered["sha256"])
                    await asyncio.to_thread(self.verify_saved_output, current, saved, item)
                    result = {"asset": recovered, "run_id": recovered["legacy_reference"]["processing_run_id"],
                        "review": next(row for row in await asyncio.to_thread(self.repository.read_reviews, task_id) if row["asset_id"] == recovered["id"])}
                else:
                    request = ComposeRequest(task_id=task_id, person_asset_id=saved["person_asset_id"],
                        background_asset_id=item["background_asset_id"], width=saved["width"], height=saved["height"])
                    async with self.production.start_lock:
                        current = await asyncio.to_thread(self.task, task_id, own_batch=batch_id)
                        run_id, directory = await asyncio.to_thread(self.begin, current, "compose", "FactoryManualCompose")
                        await asyncio.to_thread(self._saved_item_update, task_id, saved["id"], item["ordinal"],
                            status="RUNNING", run_id=run_id, attempts=[*item["attempts"], run_id])
                        await asyncio.to_thread(self.repository.mutate, lambda state: find(state, "runs", run_id).update(
                            idempotency_key=item["step_key"], retry_count=len(item["attempts"]),
                            resource_info={"saved_background_intent_id": saved["id"], "ordinal": item["ordinal"], "step_key": item["step_key"]}))
                        self.cancels[run_id] = self.production.cancels[batch_id]
                    heartbeat = asyncio.create_task(self.heartbeat(task_id, run_id))
                    try:
                        result = await self.render(request, run_id=run_id, directory=directory, own_batch=batch_id, saved_intent_id=saved["id"])
                    finally:
                        heartbeat.cancel()
                        await asyncio.gather(heartbeat, return_exceptions=True)
                        self.cancels.pop(run_id, None)
                await asyncio.to_thread(self._saved_item_update, task_id, saved["id"], item["ordinal"], status="READY",
                    run_id=result["run_id"], asset_id=result["asset"]["id"], output_sha256=result["asset"]["sha256"], review_id=result["review"]["id"])
                person = await asyncio.to_thread(self.repository.read_asset, saved["person_asset_id"])
                assessment = await self.production._quality(batch_id, task_id, person, result["asset"])
                await asyncio.to_thread(self._saved_item_update, task_id, saved["id"], item["ordinal"], quality={
                    "status": assessment["status"] if assessment else "UNKNOWN", "assessment_id": assessment["id"] if assessment else None,
                    "run_id": assessment["processing_run_id"] if assessment else None})
            except (ProductionStopped, TaskStopped):
                raise
            except Exception as exc:
                await asyncio.to_thread(self._saved_item_update, task_id, saved["id"], item["ordinal"], status="FAILED", error=str(exc)[:2000])
                await self.production.event(task_id, "compose", f"背景{item['ordinal']:02d}失败；保留记录并继续下一项", "FAILED")
        current = await asyncio.to_thread(self.repository.read_task, task_id)
        items = current["context"]["manual_compose"]["saved_backgrounds"]["items"]
        await asyncio.to_thread(self._saved_item_update, task_id, saved["id"], status="COMPLETED_WITH_ERRORS" if any(row["status"] != "READY" for row in items) else "WAITING_FINAL_REVIEW")
        await asyncio.to_thread(self.production._wait, task_id, "SAVED_COMPOSITIONS_FINAL_REVIEW", "review", "现有背景本地合成已结束；查看每条真实成片与质检，等待最终审核。未采集、下载、付费或重新抠像")

    async def upload_file(self, upload, directory, basename, extensions):
        extension = Path(upload.filename or "").suffix.lower()
        if extension not in extensions:
            raise HTTPException(422, "UNSUPPORTED_UPLOAD_FORMAT")
        path = directory / (basename + extension)
        size = 0
        size_limit = self.settings.manual_import_max_bytes
        with path.open("xb") as handle:
            while chunk := await upload.read(min(1024 * 1024, size_limit - size + 1)):
                size += len(chunk)
                if size > size_limit:
                    raise HTTPException(413, f"UPLOAD_TOO_LARGE：单文件上限 {size_limit} 字节")
                handle.write(chunk)
        if not size:
            raise HTTPException(422, "EMPTY_UPLOAD")
        return path

    async def upload_background(self, task_id, upload):
        async with self.production.start_lock:
            task = self.task(task_id)
            run_id, directory = self.begin(task, "background", "FactoryManualUpload")
        cleanup = None

        def record_cleanup(state):
            find(state, "runs", run_id).setdefault("resource_info", {})["background_upload"] = cleanup
            audit(state, "background.upload.cleanup", run_id, json.dumps(cleanup, ensure_ascii=False), task_id)

        try:
            path = await self.upload_file(upload, directory, "Background", IMAGE_EXTENSIONS)
            media = await asyncio.to_thread(probe_media, path, count_frames=True)
            if media["codec"] not in {"png", "mjpeg", "webp"} or media["frame_count"] != 1 or media["width"] * media["height"] > MAX_PIXELS:
                raise HTTPException(422, "BACKGROUND_IMAGE_REQUIRED")
            asset = self.registry.register(task, {"path": str(path), "source_system": "FactoryManualUpload", "legacy_reference": {"manual_upload": True}}, "background", run_id)
            registered_path = self.registry.read_path(asset["storage_path"])
            cleanup = {"asset_id": asset["id"], "uploaded_path": str(path), "retained_path": str(registered_path),
                "sha256": asset["sha256"], "status": "NOT_REQUIRED", "deleted_bytes": 0}
            if registered_path != path.resolve():
                cleanup["status"] = "PENDING"
                self.repository.update_processing_run_event(task_id, run_id, {"resource_info": {"background_upload": cleanup}})
                with self.repository.lock:
                    if (path.is_symlink() or path.parent != directory or not path.resolve().is_relative_to(self.settings.storage_root.resolve())
                            or any(path in self.registry._asset_files(row) for row in self.repository.state()["assets"])
                            or self.registry._file_writers([path])):
                        raise ValueError("BACKGROUND_DUPLICATE_CLEANUP_NOT_EXCLUSIVE")
                    if not registered_path.is_file() or file_hash(registered_path) != asset["sha256"] or file_hash(path) != asset["sha256"]:
                        raise ValueError("BACKGROUND_DUPLICATE_IDENTITY_CHANGED")
                    size = path.stat().st_size
                    path.unlink()
                cleanup.update(status="DELETED", deleted_bytes=size)
            self.repository.mutate(record_cleanup)
            self.finish(task_id, run_id)
            return {"asset": asset, "cleanup": cleanup, "message": "背景图片已复用，重复上传副本已清理" if cleanup["status"] == "DELETED" else "背景图片已登记"}
        except (HTTPException, OSError, ValueError, subprocess.SubprocessError) as exc:
            if cleanup and cleanup["status"] == "PENDING":
                cleanup.update(status="FAILED", error=str(exc), uploaded_file_exists=path.is_file())
                self.repository.mutate(record_cleanup)
            self.finish(task_id, run_id, failure=exc)
            if isinstance(exc, HTTPException):
                raise
            if cleanup and cleanup["status"] == "FAILED":
                raise HTTPException(409, {"code": "BACKGROUND_DUPLICATE_CLEANUP_FAILED",
                    "message": "已登记背景未改动；本次重复副本清理失败，请查看运行记录", "cleanup": cleanup}) from exc
            raise HTTPException(422, "BACKGROUND_IMAGE_INVALID") from exc

    async def upload_person(self, task_id, rgb_file, alpha_file=None):
        async with self.production.start_lock:
            task = self.task(task_id)
            run_id, directory = self.begin(task, "person", "FactoryManualUpload")
        try:
            original = await self.upload_file(rgb_file, directory, "Original", VIDEO_EXTENSIONS)
            media = await asyncio.to_thread(probe_media, original, count_frames=True)
            pixel_format = str(media.get("pix_fmt", ""))
            embedded_alpha = pixel_format in {"rgba", "bgra", "argb", "abgr"} or pixel_format.startswith(("yuva", "gbrap"))
            if not alpha_file and not embedded_alpha:
                raise HTTPException(422, "PERSON_ALPHA_REQUIRED：普通 RGB 视频不能作为透明人物，请补充 Alpha 视频")
            if not media["frame_count"] or media["fps"] <= 0 or media["width"] * media["height"] > MAX_PIXELS:
                raise HTTPException(422, "PERSON_MEDIA_INVALID")
            original_alpha = await self.upload_file(alpha_file, directory, "OriginalAlpha", VIDEO_EXTENSIONS) if alpha_file else original
            if alpha_file:
                mask = await asyncio.to_thread(probe_media, original_alpha, count_frames=True)
                if not str(mask.get("pix_fmt", "")).startswith("gray"):
                    raise HTTPException(422, "ALPHA_MASK_REQUIRED：Alpha 文件必须是单通道遮罩视频")
                if any(media[key] != mask[key] for key in ("width", "height", "frame_count")) or abs(media["fps"] - mask["fps"]) > .001:
                    raise HTTPException(422, "PERSON_ALPHA_ALIGNMENT_MISMATCH")
            rgb, alpha, black = directory / "RGB.mkv", directory / "Alpha.mkv", directory / "Preview_black.mp4"
            common = ["ffmpeg", "-v", "error", "-nostdin", "-n", "-filter_threads", "1"]
            rgb_command = [*common, "-i", str(original), "-map", "0:v:0", "-map", "0:a?", "-vf", "format=gbrp16le", "-c:v", "ffv1", "-level", "3", "-threads", "1", "-c:a", "pcm_s16le", str(rgb)]
            alpha_command = [*common, "-i", str(original_alpha), "-map", "0:v:0", "-vf", ("" if alpha_file else "alphaextract,") + "format=gray16le", "-c:v", "ffv1", "-level", "3", "-threads", "1", "-an", str(alpha)]
            await self.execute(task_id, run_id, "person", rgb_command, directory)
            await self.execute(task_id, run_id, "person", alpha_command, directory)
            rgb_media, alpha_media = await asyncio.to_thread(probe_media, rgb, count_frames=True), await asyncio.to_thread(probe_media, alpha, count_frames=True)
            black_command = [*common, "-i", str(rgb), "-i", str(alpha), "-filter_complex_threads", "1", "-filter_complex",
                f"[0:v]format=rgb24[r];[1:v]format=gray[a];[r][a]alphamerge[p];color=black:s={media['width']}x{media['height']}:r={media['fps']}[b];[b][p]overlay,format=yuv420p[v]",
                "-map", "[v]", "-map", "0:a?", "-frames:v", str(media["frame_count"]), "-c:v", "libx264", "-threads", "1", "-c:a", "aac",
                "-af", f"apad,atrim=duration={media['frame_count'] / media['fps']}", str(black)]
            await self.execute(task_id, run_id, "person", black_command, directory)
            evidence_command = [str(self.settings.quality_python), "-B",
                str(Path(__file__).with_name("legacy") / "manual_import_evidence_worker.py"), "--directory", str(directory)]
            await self.execute(task_id, run_id, "person", evidence_command, directory)
            imported = json.loads((directory / "import_evidence.json").read_text())
            frame_hashes, time_mapping = directory / "frame_hashes.json", directory / "time_mapping.json"
            recipe = directory / "import_recipe.json"
            recipe.write_text(json.dumps({"schema": "factory.manual-import.v1", "commands": [rgb_command, alpha_command, black_command, evidence_command]}))
            manifest = {"schema": "factory.foreground.v1", "status": "READY", "task_id": task_id, "context_id": task["context"]["task_context"]["context_id"],
                "processing_run_id": run_id, "execution_scope": "PRODUCTION", "full_decode_verified": True, "alpha_mode": "straight",
                "precision": {"RGB": "lossless FFV1 RGB16 storage", "Alpha": "supplied alpha normalized to gray16le"},
                "rgb_source": "manual uploaded uncomposited RGB", "source_path": str(original), "source_sha256": file_hash(original),
                "workflow_path": str(recipe), "workflow_sha256": file_hash(recipe), "workflow_source": "manual_import",
                "alpha_source_path": str(original_alpha), "alpha_source_sha256": file_hash(original_alpha),
                "rgb_sha256": file_hash(rgb), "alpha_sha256": file_hash(alpha), "media": imported["media"], "rgb_media": rgb_media, "alpha_media": alpha_media,
                "presentation_timestamps_verified": imported["presentation_timestamps_verified"],
                "rgb_hashes_file": frame_hashes.name, "frame_hashes_sha256": file_hash(frame_hashes),
                "time_mapping_file": time_mapping.name, "time_mapping_sha256": file_hash(time_mapping),
                "previews": [{"background": "black", "filename": black.name, "sha256": file_hash(black), "full_decode_verified": True}]}
            manifest_path = directory / "manifest.json"
            manifest_path.write_text(json.dumps(manifest))
            reference = {"manual_upload": True, "alpha_path": str(alpha), "alpha_sha256": manifest["alpha_sha256"], "alpha_mode": "straight",
                "manifest_path": str(manifest_path), "source_sha256": manifest["source_sha256"], "workflow_sha256": manifest["workflow_sha256"],
                "frame_hashes_path": str(frame_hashes), "frame_hashes_sha256": manifest["frame_hashes_sha256"],
                "time_mapping_path": str(time_mapping), "time_mapping_sha256": manifest["time_mapping_sha256"],
                "preview_path": str(black), "preview_sha256": file_hash(black)}
            asset = self.registry.register(task, {"path": str(rgb), "sha256": manifest["rgb_sha256"], "source_system": "FactoryManualUpload", "legacy_reference": reference}, "person", run_id)
            def select(state):
                current = find(state, "tasks", task_id)
                current["context"].setdefault("production", {}).update(current_person_asset_id=asset["id"], review_approved=False)
                current.update(status="WAITING_REVIEW", current_stage="review", updated_at=now())
                task_stage(state, task_id, "review").update(status="waiting_review")
                state["reviews"].append({"id": new_id("review"), "task_id": task_id, "asset_id": asset["id"], "status": "pending", "issues": ["MANUAL_PERSON_UPLOAD"], "note": ""})
                audit(state, "manual_compose.person_uploaded", asset["id"], "已验证真实 RGB/Alpha 对齐并保存原始上传；未抠像", task_id)
            self.repository.mutate(select)
            self.finish(task_id, run_id)
            return {"asset": asset, "message": "人物 RGB 与 Alpha 已验证并登记"}
        except (HTTPException, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
            self.finish(task_id, run_id, failure=exc)
            if isinstance(exc, HTTPException):
                raise
            raise HTTPException(422, "PERSON_MEDIA_INVALID：上传文件无法完整解码或 RGB/Alpha 不匹配") from exc

    async def reject(self, review_id, note=""):
        async with self.production.start_lock:
            state = self.repository.state()
            review = find(state, "reviews", review_id)
            task = self.task(review["task_id"])
            asset = find(state, "assets", review["asset_id"])
            if review["status"] != "pending" or not is_manual_review_asset(asset) or asset["task_id"] != task["id"] or asset["type"] not in {"person", "compose"} or not self.production.business_asset(asset):
                raise HTTPException(409, "MANUAL_REVIEW_NOT_PENDING")
            selected = task["context"].get("production", {}).get("current_person_asset_id") if asset["type"] == "person" else (
                task["context"].get("manual_compose", {}).get("selection", {}).get("person_asset_id")
                or task["context"].get("production", {}).get("current_person_asset_id"))
            current = asset["id"] == selected if asset["type"] == "person" else (asset["legacy_reference"].get("person_asset_id") == selected
                and task["context"]["manual_compose"].get("latest_composition_asset_id") == asset["id"])
            def decide(value):
                find(value, "reviews", review_id).update(status="rejected", note=note)
                find(value, "assets", asset["id"])["status"] = "rejected"
                if current:
                    row = find(value, "tasks", task["id"])
                    row.update(status="REJECTED", current_stage="review", updated_at=now())
                    row["context"].setdefault("production", {}).update(review_approved=False,
                        rejected_review={"review_id": review_id, "asset_id": asset["id"], "decided_at": now()})
                    task_stage(value, task["id"], "review").update(status="failed", error="MANUAL_REVIEW_REJECTED", end_time=now())
                audit(value, "review.reject", review_id, note or "手动审核拒绝；不推进、不重新采集或抠像", task["id"])
            self.repository.mutate(decide)
            return {"review_id": review_id, "message": "当前版本已拒绝，任务未推进" if current else "历史版本已拒绝；当前人物与任务不变"}

    async def approve(self, review_id, note=""):
        expand_saved = False
        async with self.production.start_lock:
            state = await asyncio.to_thread(self.repository.state)
            review = find(state, "reviews", review_id)
            task = await asyncio.to_thread(self.task, review["task_id"])
            asset = find(state, "assets", review["asset_id"])
            if review["status"] != "pending" or not is_manual_review_asset(asset) or asset["task_id"] != task["id"] or not self.production.business_asset(asset):
                raise HTTPException(409, "MANUAL_REVIEW_NOT_PENDING")
            selected = task["context"].get("production", {}).get("current_person_asset_id") if asset["type"] == "person" else (
                task["context"].get("manual_compose", {}).get("selection", {}).get("person_asset_id")
                or task["context"].get("production", {}).get("current_person_asset_id"))
            if asset["type"] == "person":
                if asset["id"] != selected:
                    raise HTTPException(409, "CURRENT_PERSON_CHANGED")
                await asyncio.to_thread(self.pair, task, asset["id"])
                final = None
            elif asset["type"] == "compose" and asset["source_system"] == "FactoryManualCompose":
                saved = task["context"]["manual_compose"].get("saved_backgrounds", {})
                bounded_asset = any(row.get("asset_id") == asset["id"] and row["status"] == "READY" for row in saved.get("items", []))
                if (asset["legacy_reference"].get("person_asset_id") != selected
                        or not bounded_asset and task["context"]["manual_compose"].get("latest_composition_asset_id") != asset["id"]):
                    raise HTTPException(409, "CURRENT_PERSON_CHANGED")
                expand_saved = saved.get("first_composition_asset_id") == asset["id"] and saved.get("status") == "WAITING_FIRST_REVIEW"
                if expand_saved:
                    await asyncio.to_thread(self.validate_saved_backgrounds, task, require_approval=False)
                person, _, _, media = await asyncio.to_thread(self.pair, task, selected, allow_library=True)
                asset, path = await asyncio.to_thread(self.checked_asset, task["id"], asset["id"], "compose")
                background, _ = await asyncio.to_thread(self.checked_asset, task["id"], asset["legacy_reference"]["background_asset_id"], "background", allow_library=True)
                try:
                    manifest_path = self.registry.allowed_path(asset["legacy_reference"]["manifest_path"])
                    if file_hash(manifest_path) != asset["legacy_reference"]["manifest_sha256"]:
                        raise ValueError("MANUAL_COMPOSITION_MANIFEST_CHANGED")
                    manifest = json.loads(manifest_path.read_text())
                    checked = await asyncio.to_thread(probe_media, path, count_frames=True)
                    if (manifest.get("schema") != "factory.manual-composition.v1" or manifest.get("mode") != "COMPOSITION"
                            or manifest.get("status") != "READY" or manifest.get("full_decode_verified") is not True
                            or manifest.get("task_id") != task["id"] or manifest.get("person_asset_id") != person["id"]
                            or manifest.get("background_asset_id") != background["id"] or manifest.get("rgb_sha256") != person["sha256"]
                            or manifest.get("alpha_sha256") != person["legacy_reference"]["alpha_sha256"]
                            or manifest.get("background_sha256") != background["sha256"] or manifest.get("output_sha256") != asset["sha256"]
                            or self.registry.allowed_path(manifest["output_path"]) != path or checked != manifest.get("output_media")
                            or checked["frame_count"] != media["frame_count"] or media["audio"] and not checked["audio"]):
                        raise ValueError("MANUAL_COMPOSITION_EVIDENCE_INVALID")
                except (KeyError, OSError, ValueError, subprocess.SubprocessError) as exc:
                    raise HTTPException(409, "MANUAL_COMPOSITION_EVIDENCE_INVALID") from exc
                run_id, _ = await asyncio.to_thread(self.begin, task, "final", "HumanReview")
                try:
                    final = await asyncio.to_thread(self.registry.register, task, {"path": str(path), "sha256": asset["sha256"], "source_system": "HumanReview",
                        "legacy_reference": {"composition_asset_id": asset["id"], "review_id": review_id, "manual_compose": True,
                            "manifest_path": str(manifest_path), "manifest_sha256": file_hash(manifest_path), "full_decode_verified": True}},
                        "final", run_id, person=person, background=background)
                except (OSError, ValueError, subprocess.SubprocessError) as exc:
                    self.finish(task["id"], run_id, failure=exc)
                    raise HTTPException(409, "MANUAL_FINAL_REGISTRATION_FAILED") from exc
            else:
                raise HTTPException(409, "MANUAL_REVIEW_ASSET_REQUIRED")
            def decide(current):
                find(current, "reviews", review_id).update(status="approved", note=note)
                find(current, "assets", asset["id"])["status"] = "approved"
                current_task = find(current, "tasks", task["id"])
                current_task["context"].setdefault("production", {})["review_approved"] = True
                manual = current_task["context"].get("manual_compose", {})
                saved = manual.get("saved_backgrounds", {})
                if expand_saved:
                    saved.update(status="AUTHORIZED", approved_review_id=review_id, approved_at=now())
                previous_final = next((row for row in current["assets"] if row["id"] == manual.get("approved_final_asset_id")
                    and row["legacy_reference"].get("person_asset_id") == selected and row["legacy_reference"].get("composition_asset_id") == manual.get("latest_composition_asset_id")), None)
                completed = bool(final or previous_final)
                if saved.get("id"):
                    pending = any(row["status"] != "READY" or not any(item["id"] == row.get("review_id") and item["status"] == "approved"
                        for item in current["reviews"]) for row in saved["items"])
                    completed = completed and not pending
                current_task.update(status="COMPLETED" if completed else "COLLECTED", current_stage="final" if completed else "person", updated_at=now())
                task_stage(current, task["id"], "review").update(status="success", end_time=now())
                if final:
                    current_task["progress"] = 100
                    task_stage(current, task["id"], "final").update(status="success", end_time=now())
                    current_task["context"]["manual_compose"]["approved_final_asset_id"] = final["id"]
                audit(current, "review.approve", review_id, "手动生产审核已保存；未自动采集、抠像、请求背景或发布", task["id"])
            await asyncio.to_thread(self.repository.mutate, decide)
            if final:
                await asyncio.to_thread(self.finish, task["id"], run_id, manifest=manifest)
        response = {"review_id": review_id, "final": final, "message": "成片已批准" if final else "人物版本已批准"}
        if expand_saved:
            response["continuation"] = await self._continue_saved(task["id"])
        return response
