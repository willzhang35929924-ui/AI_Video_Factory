import asyncio
import hashlib
import json
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException

from app.domain import audit, find, new_id, now, task_stage
from app.integrations.asset_registry import AssetRegistry, file_hash
from app.integrations.collector_service import record_collector_environment
from app.integrations.legacy.video_adapter import BackgroundRPAAdapter, LegacyComfyUIAdapter, LegacyComposeAdapter, LegacyPromptAdapter
from app.integrations.legacy.vidbee_adapter import VidbeeDownloaderAdapter
from app.integrations.legacy.video_worker import credential_field
from app.integrations.manual_compose import is_manual_review_asset
from app.integrations.production_readiness import ProductionReadiness, STAGE_REQUIREMENTS
from app.integrations.source_review import SourceWatermarkReview
from app.integrations.process_probe import probe_process
from app.schemas import ProductionEvent
from app.storage import StoragePaths

PRODUCTION_STAGES = ("collector", "analysis", "download", "cutout", "person", "prompt", "background", "compose", "quality", "review", "final", "publish")
ACTIVE_BATCHES = {"QUEUED", "RUNNING", "PAUSED"}
DEFAULT_POLICY = {"concurrency": 1, "auto_advance": True, "auto_resume": True,
                  "retry_limit": 2, "background_strategy": "chatgpt_rpa", "person_review_required": False,
                  "quality_thresholds": {}}


class ProductionStopped(Exception):
    pass


class TaskStopped(Exception):
    pass


class ProductionService:
    """One serial queue; persisted checkpoints and providers can later move to a worker."""
    def __init__(self, repository, hub, settings, collector, providers=None):
        self.repository, self.hub, self.settings, self.collector = repository, hub, settings, collector
        options = {"legacy_root": settings.legacy_video_auto_root, "storage_root": settings.storage_root,
                   "python_path": settings.legacy_video_auto_python, "timeout_s": settings.legacy_video_timeout_seconds}
        asset_options = {**options, "cutout_workflow": settings.factory_comfy_cutout_workflow,
                         "compose_workflow": settings.factory_comfy_compose_workflow,
                         "chunk_frames": settings.factory_comfy_chunk_frames,
                         "asset_python_path": settings.quality_python}
        self.providers = providers or {"prompt": LegacyPromptAdapter(**options), "background": BackgroundRPAAdapter(**options),
                                      "cutout": LegacyComfyUIAdapter(**asset_options), "compose": LegacyComposeAdapter(**asset_options)}
        self.vidbee = VidbeeDownloaderAdapter(settings)
        self.source_review = SourceWatermarkReview(repository, settings, collector, self.vidbee)
        self.registry = AssetRegistry(repository, settings)
        self.readiness = ProductionReadiness(settings, collector, self.vidbee, self.providers["cutout"], self.providers["prompt"])
        self.jobs = {}
        self.cancels = {}
        self.start_lock = asyncio.Lock()
        self.manual_compose = None
        self.closing = False
        self.recovered_batches = []
        self.recovered_run_ids = set()
        self.recovered_item_ids = set()

    def policy(self):
        return {**DEFAULT_POLICY, **self.repository.state().get("settings", {}).get("production_policy", {})}

    def next_stage(self, task):
        if task.get("context", {}).get("local_cutout"):
            return "quality" if task["context"].get("production", {}).get("current_person_asset_id") else "cutout"
        if not task.get("collector_result") or task["collector_result"].get("error_code"):
            return "collector"
        if not task.get("context", {}).get("production", {}).get("current_person_asset_id"):
            return "cutout"
        prompts = [row for row in self.repository.state()["prompts"] if row["task_id"] == task["id"] and row["status"] == "READY"]
        return "prompt" if len({row.get("ordinal") for row in prompts}) < 20 else "background"

    def _wait(self, task_id, reason, stage, message):
        def change(state):
            task = find(state, "tasks", task_id)
            if task["status"] == "QUARANTINED":
                return
            task["context"].setdefault("production", {})["waiting"] = {"status": "WAITING_MANUAL", "reason": reason,
                "stage": stage, "message": message, "next_step": stage, "saved_at": now(),
                "resume": "处理后继续原任务；已核实的采集、下载、RGB/Alpha、提示词和背景不会重复执行"}
            task.update(status="WAITING_REVIEW", current_stage=stage, updated_at=now())
        self.repository.mutate(change)

    def _pause_dependency(self, batch_id, task_id, stage, details):
        waiting = {"status": "WAITING_MANUAL", "reason": details["code"], "stage": stage,
            "message": details.get("message", "当前阶段依赖未就绪；修复后继续原队列"),
            "checks": details.get("checks", []), "next_step": stage, "saved_at": now(),
            "resume": "依赖就绪后恢复原批次；未执行项、历史 Run 和已有资产保持不变"}
        def pause(state):
            batch, task = find(state, "batches", batch_id), find(state, "tasks", task_id)
            if batch["status"] == "CANCELLED" or task["status"] in {"CANCELLED", "SKIPPED", "REJECTED", "QUARANTINED"}:
                return
            for item in batch["items"]:
                if item["task_id"] == task_id and item["status"] == "RUNNING":
                    item["status"] = "QUEUED"
            task["context"].setdefault("production", {})["waiting"] = waiting
            task.update(status="WAITING_REVIEW", current_stage=stage, updated_at=now())
            batch.update(status="PAUSED", active_task_id=None, updated_at=now())
            batch.setdefault("summary", {})["environment_wait"] = waiting
            audit(state, "production.environment_pause", batch_id, waiting["message"], task_id)
        self.repository.mutate(pause)

    async def continue_task(self, task_id, *, stage=None, deferred_id=None, recovered_batch_id=None, explicit=False):
        """A resolved human prerequisite re-enters the same serial scheduler."""
        async with self.start_lock:
            state = self.repository.state()
            task = find(state, "tasks", task_id)
            production = task["context"].get("production", {})
            if task["status"] == "QUARANTINED":
                raise HTTPException(409, "COLLECTOR_QUARANTINED：采集证据不确定，已隔离且不会自动续跑或重复外部请求")
            if production.get("asset_family_deleted"):
                raise HTTPException(409, "ASSET_FAMILY_DELETED：素材家族已明确删除，原任务仅保留记录；不会重新生成已删除媒体，请创建新任务")
            if task["context"].get("local_cutout"):
                person = self._asset(task_id, "person")
                if person and self._person_review_approved(task_id, person):
                    def local_ready(current):
                        selected = find(current, "tasks", task_id)
                        selected.update(status="COLLECTED", current_stage="person", updated_at=now())
                        selected["context"]["local_cutout"]["status"] = "PERSON_READY"
                    self.repository.mutate(local_ready)
                    return {"task_id": task_id, "status": "PERSON_READY", "message": "本地原片的人像审核已保存；请复用当前人物选择背景合成，不进入采集或付费背景请求"}
                return {"task_id": task_id, "status": "WAITING_MANUAL", "message": "本地人物版本仍需审核；原片、RGB与Alpha已保留"}
            manual = task["context"].get("manual_compose", {})
            saved = manual.get("saved_backgrounds", {})
            bounded_saved = bool(saved.get("status") in {"AUTHORIZED", "RUNNING"} and self.manual_compose
                and saved.get("approved_review_id") == saved.get("first_review_id") and stage in {None, "saved_background_compose"})
            if bounded_saved:
                await asyncio.to_thread(self.manual_compose.validate_saved_backgrounds, task)
                stage = "saved_background_compose"
            selected_person = next((row for row in state["assets"] if row["id"] == production.get("current_person_asset_id") and row["task_id"] == task_id and row["type"] == "person"), None)
            manual_hold = manual.get("auto_resume_required") or task["context"].get("production_strategy") == "manual_compose"
            normal_person = bool(selected_person and self.business_asset(selected_person) and selected_person["registry_status"] == "registered"
                and selected_person["status"] not in {"rejected", "failed"} and not selected_person["legacy_reference"].get("superseded_by_person_asset_id"))
            if not bounded_saved and (is_manual_review_asset(selected_person) or manual_hold and (not explicit or not normal_person)):
                return {"task_id": task_id, "status": "WAITING_MANUAL", "reason": "MANUAL_COMPOSE_REQUIRES_EXPLICIT_RESUME",
                    "message": "审核已保存；手动位置版本只能在手动合成中渲染。恢复普通母资产后，需明确继续自动生产；不会自动请求付费背景"}
            intent = production.get("deferred_continue", {})
            if deferred_id and (intent.get("id") != deferred_id or intent.get("status") != "PENDING"
                    or production.get("current_person_asset_id") != intent.get("person_asset_id")
                    or production.get("first_composition", {}).get("fingerprint") != intent.get("first_fingerprint")
                    or production.get("first_composition", {}).get("human_review") != intent.get("first_human_review")):
                return {"task_id": task_id, "message": "待续跑版本已变化，保留当前任务与所有版本"}
            origin = next((row for row in state["batches"] if row["id"] == production.get("batch_id")), None)
            if task["status"] in {"REJECTED", "CANCELLED", "SKIPPED"} or origin and origin["status"] == "CANCELLED" or not self.policy()["auto_advance"]:
                return {"task_id": task_id, "message": "审核已保存；自动续跑未启用，或任务已拒绝、取消或跳过"}
            if task["status"] == "PAUSED" or origin and origin["status"] == "PAUSED" and not (
                    origin["id"] == recovered_batch_id and origin["id"] in self.recovered_batches):
                return {"task_id": task_id, "message": "审核已保存；生产已暂停，恢复队列后继续"}
            if any(row["task_id"] == task_id and row.get("error_code") == "UNKNOWN_EXTERNAL_REQUEST" for row in state["runs"]):
                self._wait(task_id, "UNKNOWN_EXTERNAL_REQUEST", task["current_stage"], "外部请求状态未确认；审核已保存，不自动重新提交已付费请求")
                return {"task_id": task_id, "message": "审核已保存；需核实外部未知请求后继续"}
            active = [row for row in state["runs"] if row["task_id"] == task_id and row["status"] in {"running", "queued"}]
            if active and all(row["provider"] == "FactoryMatteRepair" and row["stage"] == "person" for row in active):
                person = await asyncio.to_thread(self._asset, task_id, "person")
                repairs = task["context"].get("matte_repairs", [])
                if person and all(any(record.get("run_id") == row["id"] and record.get("person_asset_id") == person["id"]
                        and record.get("execution_scope", "PRODUCTION") == "PRODUCTION"
                        and record.get("status") in {"QUEUED", "PROPAGATING"}
                        and record.get("parent_rgb_sha256") == person["sha256"]
                        and record.get("parent_alpha_sha256") == person["legacy_reference"].get("alpha_sha256")
                        for record in repairs) for row in active):
                    first = production.get("first_composition", {})
                    saved = {"id": new_id("continue"), "status": "PENDING", "stage": stage,
                        "person_asset_id": person["id"], "rgb_sha256": person["sha256"],
                        "alpha_sha256": person["legacy_reference"]["alpha_sha256"],
                        "first_human_review": first.get("human_review"), "first_fingerprint": first.get("fingerprint"),
                        "first_composition_asset_id": first.get("composition_asset_id"),
                        "repair_run_ids": [row["id"] for row in active], "created_at": now()}
                    def defer(current):
                        selected = find(current, "tasks", task_id)
                        checkpoint = selected["context"].setdefault("production", {})
                        checkpoint["deferred_continue"] = saved
                        checkpoint["waiting"] = {"status": "WAITING_LOCAL_WORK", "reason": "LOCAL_REPAIR_PREVIEW_RUNNING",
                            "stage": "person", "next_step": stage or "background", "saved_at": now(),
                            "message": "审核已保存；等待正在运行的局部修复预览结束，随后自动继续生产。预览不会自动应用，当前母资产不变。",
                            "resume": "无需再次点击；预览完成后复用当前 RGB/Alpha、首条审核和已有背景继续"}
                        selected.update(status="waiting", updated_at=now())
                        audit(current, "production.continue_deferred", saved["id"], "审核完成，等待真实局部预览释放执行槽后自动续跑", task_id)
                    self.repository.mutate(defer)
                    await self.event(task_id, "person", "审核已保存；等待局部修复预览结束后自动继续，预览不会自动应用", "WAITING_LOCAL_WORK")
                    return {"task_id": task_id, "deferred": True, "status": "WAITING_LOCAL_WORK",
                        "reason": "LOCAL_REPAIR_PREVIEW_RUNNING", "message": "审核已保存；局部修复预览结束后自动继续生产"}
            if active:
                return {"task_id": task_id, "message": "当前任务已在执行，不重复排队"}
            batch = next((row for row in state["batches"] if row["status"] in ACTIVE_BATCHES), None)
            batch_id = batch["id"] if batch else new_id("batch")
            def change(current):
                task = find(current, "tasks", task_id)
                if task["status"] == "QUARANTINED":
                    raise HTTPException(409, "COLLECTOR_QUARANTINED：采集证据已隔离，未重新加入生产队列")
                if explicit and not bounded_saved and task["context"].get("manual_compose"):
                    manual = task["context"]["manual_compose"]
                    manual["auto_resume_required"] = False
                    if task["context"].get("production_strategy") == "manual_compose":
                        if manual.get("previous_strategy") and manual["previous_strategy"] != "manual_compose":
                            task["context"]["production_strategy"] = manual["previous_strategy"]
                        else:
                            task["context"].pop("production_strategy", None)
                production = task["context"].setdefault("production", {})
                production.update(batch_id=batch_id, previous_status=task["status"], requested_stage=stage,
                                  requested_prompt_id=None, requested_person_asset_id=None, retry=False, automatic=True,
                                  policy_snapshot=self.policy(), queued_at=now())
                if bounded_saved:
                    task["context"]["manual_compose"]["saved_backgrounds"]["batch_id"] = batch_id
                production.pop("waiting", None)
                if production.get("deferred_continue", {}).get("status") == "PENDING":
                    production["deferred_continue"].update(status="CONSUMED", batch_id=batch_id, consumed_at=now())
                task.update(status="QUEUED", updated_at=now())
                if batch:
                    selected = find(current, "batches", batch_id)
                    if selected["status"] == "PAUSED" and selected["id"] == recovered_batch_id:
                        selected.update(status="QUEUED", updated_at=now())
                    item = next((row for row in selected["items"] if row["task_id"] == task_id), None)
                    if item:
                        item.update(status="QUEUED", stage=stage or task["current_stage"])
                    else:
                        selected["task_ids"].insert(0, task_id)
                        selected["items"].insert(0, {"task_id": task_id, "status": "QUEUED", "stage": stage or task["current_stage"]})
                else:
                    current["batches"].append({"id": batch_id, "status": "QUEUED", "task_ids": [task_id],
                        "items": [{"task_id": task_id, "status": "QUEUED", "stage": stage or task["current_stage"]}],
                        "active_task_id": None, "summary": {}, "created_at": now(), "updated_at": now()})
                audit(current, "production.auto_continue", batch_id, "人工前置条件已处理，继续已保存步骤；不重复抠像或外部请求", task_id)
            self.repository.mutate(change)
            return {"accepted": True, "task_id": task_id, "batch_id": batch_id, "message": "已自动进入生产队列"}

    async def continue_after_repair(self, task_id):
        return await self.continue_task(task_id, stage="quality")

    async def resume_cutout_checkpoint(self, task_id):
        """Pin a verified partial cutout and put it back in its original serial queue."""
        async with self.start_lock:
            state = await asyncio.to_thread(self.repository.state)
            task = find(state, "tasks", task_id)
            production = task["context"].get("production", {})
            batch = find(state, "batches", production.get("batch_id"))
            runs = [row for row in state["runs"] if row["task_id"] == task_id]
            failed = runs[-1] if runs else {}
            if (task["execution_mode"] != "integration" or task["status"] != "FAILED"
                    or failed.get("stage") != "cutout" or failed.get("provider") != "LegacyComfyUIAdapter"
                    or failed.get("error_code") != "STAGE_RESULT_INVALID" or failed.get("error_message") != "TimeoutError"
                    or production.get("current_person_asset_id")):
                raise HTTPException(409, "CUTOUT_CHECKPOINT_NOT_ELIGIBLE：只有尚未完成母资产的真实抠像回调中断可以续跑")
            if any(row["status"] in {"queued", "running"} for row in state["runs"]) or any(
                    item["status"] == "RUNNING" for row in state["batches"] for item in row["items"]):
                raise HTTPException(409, "ALREADY_RUNNING：请等待当前真实执行结束")
            if batch["status"] == "CANCELLED" or any(row["id"] != batch["id"] and row["status"] in ACTIVE_BATCHES for row in state["batches"]):
                raise HTTPException(409, "ORIGINAL_BATCH_NOT_AVAILABLE：保留原队列，不创建并发批次")
            if any(row.get("error_code") == "UNKNOWN_EXTERNAL_REQUEST" for row in runs):
                raise HTTPException(409, "UNKNOWN_EXTERNAL_REQUEST：先核实外部回执，不重复提交")
            if failed.get("pid"):
                try:
                    probe_process(failed["pid"])
                except ProcessLookupError:
                    pass
                except PermissionError:
                    raise HTTPException(409, "WORKER_STATUS_UNKNOWN：原进程状态尚未核实") from None
                else:
                    raise HTTPException(409, "WORKER_STILL_RUNNING：原进程仍存在，不重复启动")
            source, context = await self._saved_context(task)
            generation = production.get("cutout_generation", 1)
            context = {**context, "cutout_generation": generation, "object_strategy": production.get("object_strategy", {}),
                "execution_scope": "PRODUCTION"}
            paths = StoragePaths(self.settings.storage_root).task_paths(task["project_id"], task_id)
            directory = Path(paths["metadata"]) / "production" / f"v{generation}" / "cutout"
            result = await self.providers["cutout"].assets.checkpoint(context, directory)
            plan = result.get("metadata", {}).get("checkpoint", {})
            if (result["status"] != "READY" or plan.get("task_id") != task_id or plan.get("context_id") != context["context_id"]
                    or plan.get("source_sha256") != source["sha256"] or plan.get("generation") != generation
                    or plan.get("business_executed") is not False or plan.get("paid_requests") != 0 or plan.get("comfy_submissions") != 0
                    or not plan.get("completed_frames") or any(row.get("status") == "RUNNING" for row in plan.get("chunks", []))):
                raise HTTPException(409, {"code": "CUTOUT_CHECKPOINT_NOT_VERIFIED", "message": "未核实原片、分块回执或当前外部执行；未提交抠像", "error_code": result.get("error_code")})
            pinned = {key: plan[key] for key in ("generation", "input_fingerprint", "checkpoint_fingerprint")}
            def requeue(current):
                current_task = find(current, "tasks", task_id)
                current_batch = find(current, "batches", batch["id"])
                item = next(row for row in current_batch["items"] if row["task_id"] == task_id)
                current_production = current_task["context"].get("production", {})
                if (current_task["status"] != "FAILED" or current_batch["status"] != batch["status"]
                        or item["status"] not in {"FAILED", "WAITING_REVIEW"}
                        or current_production.get("cutout_generation", 1) != generation
                        or current_production.get("current_person_asset_id")
                        or current_task["context"].get("task_context") != task["context"].get("task_context")
                        or any(row["status"] in {"queued", "running"} for row in current["runs"])
                        or any(item["status"] == "RUNNING" for row in current["batches"] for item in row["items"])
                        or any(row["id"] != batch["id"] and row["status"] in ACTIVE_BATCHES for row in current["batches"])):
                    raise HTTPException(409, "CUTOUT_RECOVERY_CHANGED：检查期间状态变化，未恢复或重做")
                current_production.update(requested_stage="cutout", retry=False, cutout_checkpoint_resume={
                    "status": "AUTHORIZED", "pinned": pinned, "origin_run_id": failed["id"], "source_asset_id": source["id"],
                    "checkpoint": plan, "created_at": now()})
                current_production.pop("waiting", None)
                current_task.update(status="QUEUED", current_stage="cutout", updated_at=now())
                item.update(status="QUEUED", stage="cutout")
                paused = current_batch["status"] == "PAUSED"
                current_batch.update(status="PAUSED" if paused else "QUEUED", active_task_id=None, updated_at=now(),
                    summary=self._summary(current, current_batch["task_ids"]))
                audit(current, "production.cutout_checkpoint_resume", failed["id"],
                    f"核实并续接同v{generation}：复用{plan['completed_frames']}帧，仅恢复未完成分块；旧失败Run和媒体保留", task_id)
                return paused
            paused = await asyncio.to_thread(self.repository.mutate, requeue)
            return {"accepted": True, "task_id": task_id, "batch_id": batch["id"], "status": "PAUSED" if paused else "queued",
                "launch": not paused, "generation": generation, "completed_frames": plan["completed_frames"],
                "receipt_lost_frames": plan["receipt_lost_frames"], "origin_run_id": failed["id"],
                "message": "同版本检查点已保存；原批次仍暂停，请恢复原队列" if paused else "已续接同版本检查点，保留已完成分块"}

    def _deferred_identity_valid(self, task_id, intent):
        task = find(self.repository.state(), "tasks", task_id)
        production = task["context"].get("production", {})
        first = production.get("first_composition", {})
        if (task["execution_mode"] != "integration" or production.get("current_person_asset_id") != intent.get("person_asset_id")
                or first.get("human_review") != intent.get("first_human_review")
                or first.get("fingerprint") != intent.get("first_fingerprint")
                or first.get("composition_asset_id") != intent.get("first_composition_asset_id")
                or production.get("rejected_review", {}).get("asset_id") in {
                    intent.get("person_asset_id"), intent.get("first_composition_asset_id")} and production.get("rejected_review")):
            return False
        try:
            person = self._asset(task_id, "person")
            if (not person or person["id"] != intent.get("person_asset_id") or person["sha256"] != intent.get("rgb_sha256")
                    or person["legacy_reference"].get("alpha_sha256") != intent.get("alpha_sha256")):
                return False
            return intent.get("first_human_review") != "APPROVED" or self._first_review_approved(task_id, person)
        except (OSError, ValueError, KeyError, HTTPException):
            return False

    async def consume_deferred_continue(self, task_id, *, recovered_batch_id=None):
        if self.closing or not self.policy()["auto_advance"]:
            return
        state = self.repository.state()
        task = find(state, "tasks", task_id)
        production = task["context"].get("production", {})
        intent = production.get("deferred_continue", {})
        origin = next((row for row in state["batches"] if row["id"] == production.get("batch_id")), None)
        if intent.get("status") != "PENDING" or task["status"] in {"PAUSED", "CANCELLED", "REJECTED", "SKIPPED", "QUARANTINED"} or origin and (
                origin["status"] == "CANCELLED" or origin["status"] == "PAUSED" and not (
                    origin["id"] == recovered_batch_id and origin["id"] in self.recovered_batches)):
            return
        if any(row["task_id"] == task_id and row["status"] in {"queued", "running"} for row in state["runs"]):
            return
        if any(row["task_id"] == task_id and row.get("error_code") == "UNKNOWN_EXTERNAL_REQUEST" for row in state["runs"]):
            self._wait(task_id, "UNKNOWN_EXTERNAL_REQUEST", task["current_stage"], "待续跑前需核实外部未知请求；不会重复付费")
            return
        if not await asyncio.to_thread(self._deferred_identity_valid, task_id, intent):
            def changed(current):
                selected = find(current, "tasks", task_id)["context"]["production"]["deferred_continue"]
                if selected.get("id") == intent["id"]:
                    selected.update(status="BLOCKED", reason="DEFERRED_INPUT_CHANGED", checked_at=now())
            self.repository.mutate(changed)
            self._wait(task_id, "DEFERRED_INPUT_CHANGED", "review", "等待期间母资产或审核证据已变化；请确认当前版本，未应用预览或复用旧批准")
            return
        outcome = await self.continue_task(task_id, stage=intent.get("stage"), deferred_id=intent["id"], recovered_batch_id=recovered_batch_id)
        if outcome.get("accepted"):
            await self.launch(outcome["batch_id"])

    def queue(self):
        batch = self.repository.read_latest_batch(ACTIVE_BATCHES)
        return {"concurrency": 1, "status": "IDLE", "task_ids": [], "items": [], "active_task_id": None, "summary": {}} if batch is None else {**batch, "batch_id": batch["id"], "concurrency": 1}

    def _download_resume(self, task_id):
        check = getattr(self.collector, "can_resume_download", None)
        return bool(check and check(task_id))

    def _local_resume(self, task_id):
        runs = [row for row in self.repository.state()["runs"] if row["task_id"] == task_id]
        return bool(runs and not any(row.get("error_code") == "UNKNOWN_EXTERNAL_REQUEST" for row in runs)
                    and runs[-1].get("error_code") in {"LOCAL_EXECUTION_INTERRUPTED", "PROCESS_CANCELLED", "PROCESS_TIMEOUT"}
                    and runs[-1]["provider"] in {"LegacyComfyUIAdapter", "LegacyComposeAdapter", "CutoutQualityAnalyzer", "OpenCVCutoutQualityAnalyzer"})

    @staticmethod
    def business_asset(asset):
        return asset.get("legacy_reference", {}).get("execution_scope") != "TECHNICAL_VALIDATION"

    async def start(self, task_ids, *, retry=False, stage=None, prompt_id=None, person_asset_id=None):
        if self.settings.app_mode != "integration":
            raise HTTPException(409, "INTEGRATION_MODE_REQUIRED")
        if not isinstance(task_ids, list) or not task_ids or len(task_ids) > 200 or any(not isinstance(value, str) for value in task_ids):
            raise HTTPException(422, "请选择 1–200 个已创建的正常任务")
        task_ids = list(dict.fromkeys(task_ids))
        if stage is not None and stage not in STAGE_REQUIREMENTS:
            raise HTTPException(422, "INVALID_PRODUCTION_STAGE")
        if (prompt_id or person_asset_id) and len(task_ids) != 1:
            raise HTTPException(422, "ASSET_SELECTION_REQUIRES_SINGLE_TASK")
        if stage == "background" and not prompt_id:
            raise HTTPException(422, "BACKGROUND_PROMPT_SELECTION_REQUIRED")
        async with self.start_lock:
            state = self.repository.state()
            if any(batch["status"] in ACTIVE_BATCHES for batch in state["batches"]):
                raise HTTPException(409, "BATCH_ALREADY_ACTIVE")
            for task_id in task_ids:
                task = find(state, "tasks", task_id)
                if task["status"] == "QUARANTINED":
                    raise HTTPException(409, "COLLECTOR_QUARANTINED：采集证据不确定，已隔离且不会自动续跑或重复外部请求")
                if task["context"].get("production", {}).get("asset_family_deleted"):
                    raise HTTPException(409, "ASSET_FAMILY_DELETED：素材家族已明确删除，原任务仅保留记录；不会重新生成已删除媒体，请创建新任务")
                if task["execution_mode"] != "integration":
                    raise HTTPException(409, "MOCK_TASK_NOT_ALLOWED")
                selected_person = next((row for row in state["assets"] if row["id"] == task["context"].get("production", {}).get("current_person_asset_id") and row["task_id"] == task_id and row["type"] == "person"), None)
                if is_manual_review_asset(selected_person):
                    raise HTTPException(409, "MANUAL_PERSON_REQUIRES_MANUAL_COMPOSE：先恢复普通母资产；旧合成器不会忽略位置版本")
                if (task["context"].get("manual_compose", {}).get("auto_resume_required") or task["context"].get("production_strategy") == "manual_compose") and stage not in {"cutout", "person", "quality", "compose"}:
                    raise HTTPException(409, "MANUAL_COMPOSE_REQUIRES_EXPLICIT_RESUME：恢复普通母资产后明确继续自动生产；未启动采集或付费请求")
                if any(run["task_id"] == task_id and run["status"] in {"queued", "running"} for run in state["runs"]):
                    raise HTTPException(409, "ALREADY_RUNNING")
                if task["status"] in {"COMPLETED", "REJECTED"} and not retry:
                    raise HTTPException(409, "ALREADY_COMPLETED")
                if stage != "compose" and any(run["task_id"] == task_id and run.get("error_code") == "UNKNOWN_EXTERNAL_REQUEST" for run in state["runs"]):
                    raise HTTPException(409, "UNKNOWN_EXTERNAL_REQUEST：确认外部结果后再恢复，禁止重复计费")
                if stage == "background":
                    person = self._asset(task_id, "person")
                    if person is None or not self._person_review_approved(task_id, person):
                        raise HTTPException(409, "PERSON_REVIEW_REQUIRED：先审核当前有效 RGB + Alpha 母资产，尚未请求付费背景")
                if stage not in {"prompt", "background"} and task["status"] == "WAITING_REVIEW" and not retry and not task["context"].get("production", {}).get("review_approved") and not self._download_resume(task_id):
                    raise HTTPException(409, "WAITING_REVIEW：请先在审核中心处理")
                if task["status"] == "FAILED" and not retry:
                    raise HTTPException(409, "FAILED_TASK_REQUIRES_RETRY")
                if prompt_id:
                    prompt = find(state, "prompts", prompt_id)
                    if prompt["task_id"] != task_id or prompt["status"] != "READY":
                        raise HTTPException(409, "PROMPT_NOT_READY_FOR_TASK")
                    if stage in {"background", "compose"} and prompt.get("ordinal", 1) > 1 and not self._first_review_approved(task_id):
                        raise HTTPException(409, "FIRST_COMPOSITION_REVIEW_REQUIRED：先审核背景01成片，再扩展02–20")
                if person_asset_id:
                    person = find(state, "assets", person_asset_id)
                    if (person["task_id"] != task_id or person["type"] != "person" or not self.business_asset(person)
                            or person["registry_status"] != "registered" or person["status"] in {"rejected", "failed"}
                            or person.get("legacy_reference", {}).get("superseded_by_person_asset_id")
                            or task["context"].get("production", {}).get("current_person_asset_id", person_asset_id) != person_asset_id):
                        raise HTTPException(409, "PERSON_ASSET_NOT_ELIGIBLE")
                    self.registry.content(person_asset_id)
                    if not self._alpha_available(person):
                        raise HTTPException(409, "PERSON_ALPHA_NOT_VERIFIED")
            required, ready = [], True
            for task_id in task_ids:
                selected_stage = stage or self.next_stage(find(state, "tasks", task_id))
                required_names = STAGE_REQUIREMENTS[selected_stage]
                readiness = await self.readiness.check(refresh=True, stage=selected_stage, task_id=task_id)
                if selected_stage in {"collector", "analysis"}:
                    record_collector_environment(self.repository, task_id, readiness)
                current_checks = [row for row in readiness["checks"] if row.get("name") in required_names]
                required.extend({**row, "task_id": task_id} for row in current_checks)
                ready = ready and required_names <= {row["name"] for row in current_checks} and all(row["status"] == "READY" for row in current_checks)
            if not ready:
                raise HTTPException(409, {"code": "PRODUCTION_NOT_READY", "message": "缺失当前操作所需依赖，尚未启动任何任务", "checks": required})
            if stage == "background":
                for task_id in task_ids:
                    person = self._asset(task_id, "person")
                    if person is None or not self._person_review_approved(task_id, person):
                        raise HTTPException(409, "PERSON_REVIEW_REQUIRED：当前母资产审核或文件已变化，尚未请求付费背景")
            batch_id = new_id("batch")
            def change(current):
                if any(batch["status"] in ACTIVE_BATCHES for batch in current["batches"]):
                    raise HTTPException(409, "BATCH_ALREADY_ACTIVE")
                if any(run["provider"] != "mock" and run["status"] in {"queued", "running"} for run in current["runs"]):
                    raise HTTPException(409, "ALREADY_RUNNING")
                for task_id in task_ids:
                    previous = find(state, "tasks", task_id)
                    if find(current, "tasks", task_id)["status"] != previous["status"]:
                        raise HTTPException(409, "TASK_CHANGED：任务状态已更新，请刷新后再试")
                current["batches"].append({"id": batch_id, "status": "QUEUED", "task_ids": task_ids,
                    "items": [{"task_id": task_id, "status": "QUEUED", "stage": "collector"} for task_id in task_ids],
                    "active_task_id": None, "summary": {}, "created_at": now(), "updated_at": now()})
                for task_id in task_ids:
                    task = find(current, "tasks", task_id)
                    production = task["context"].setdefault("production", {})
                    production.update(batch_id=batch_id, previous_status=task["status"], requested_stage=stage,
                                      requested_prompt_id=prompt_id, requested_person_asset_id=person_asset_id, retry=retry, queued_at=now())
                    production["automatic"] = (stage is None or bool(self.policy()["auto_advance"])) and not task["context"].get("manual_compose", {}).get("auto_resume_required")
                    production["policy_snapshot"] = self.policy()
                    production.pop("waiting", None)
                    if retry and stage in {"cutout", "person", "compose"} and production.get("rejected_review"):
                        production.setdefault("rejected_review_history", []).append(production.pop("rejected_review"))
                    if retry and stage in {"cutout", "person", "compose"}:
                        production["compose_generation"] = production.get("compose_generation", 1) + 1
                        if stage in {"cutout", "person"}:
                            production["cutout_generation"] = production.get("cutout_generation", 1) + 1
                            if production.get("cutout_checkpoint_resume"):
                                production["cutout_checkpoint_resume"]["status"] = "SUPERSEDED"
                    task.update(status="QUEUED", updated_at=now())
                    present = {row["name"] for row in current["stages"] if row["task_id"] == task_id}
                    for name in PRODUCTION_STAGES:
                        if name not in present:
                            current["stages"].append({"id": task_id + "-" + name, "task_id": task_id, "name": name, "status": "not_connected" if name == "publish" else "pending",
                                "start_time": None, "end_time": None, "duration": 0, "retry_count": 0, "error": None})
                    audit(current, "production.queued", batch_id, "正常生产任务进入串行队列；并发数 1", task_id)
            self.repository.mutate(change)
            return {"accepted": True, "batch_id": batch_id, "task_ids": task_ids, "status": "queued", "message": "已加入生产队列"}

    async def launch(self, batch_id):
        if batch_id in self.jobs:
            return
        batch = find((await asyncio.to_thread(self.repository.state)), "batches", batch_id)
        if batch_id in self.jobs or batch["status"] == "CANCELLED":
            return
        self.cancels[batch_id] = threading.Event()
        job = asyncio.create_task(self.run(batch_id))
        self.jobs[batch_id] = job
        job.add_done_callback(lambda _: self.jobs.pop(batch_id, None))

    async def event(self, task_id, stage, message, status=None, run_id=None, data=None, *, progress=None):
        await self.hub.broadcast(ProductionEvent(type="STAGE_PROGRESS", task_id=task_id, stage=stage, status=status,
                                                message=message, run_id=run_id, data=data, progress=progress))

    async def run(self, batch_id):
        initial = find((await asyncio.to_thread(self.repository.state)), "batches", batch_id)
        if initial["status"] == "CANCELLED":
            return
        try:
            while True:
                await self._checkpoint(batch_id)
                task_id = await asyncio.to_thread(self._claim_next, batch_id)
                if task_id is None:
                    await self._checkpoint(batch_id)
                    batch = find((await asyncio.to_thread(self.repository.state)), "batches", batch_id)
                    if any(item["status"] == "QUEUED" for item in batch["items"]):
                        continue
                    break
                try:
                    await self._task_checkpoint(batch_id, task_id)
                    await self._task(batch_id, task_id)
                except ProductionStopped:
                    raise
                except TaskStopped:
                    pass
                except HTTPException as exc:
                    if isinstance(exc.detail, dict) and exc.detail.get("code") in {"INTEGRATION_NOT_READY", "PRODUCTION_NOT_READY"}:
                        task = find((await asyncio.to_thread(self.repository.state)), "tasks", task_id)
                        if task["status"] == "QUARANTINED":
                            await asyncio.to_thread(self._item_update, batch_id, task_id, "QUARANTINED", task["current_stage"])
                            continue
                        await asyncio.to_thread(self._pause_dependency, batch_id, task_id, task["current_stage"], exc.detail)
                        await self.event(task_id, task["current_stage"], exc.detail.get("message", "等待生产依赖"),
                            "WAITING_MANUAL", data={"checks": exc.detail.get("checks", [])})
                        return
                    await self._checkpoint(batch_id)
                    code = exc.detail.get("code", "PRODUCTION_ERROR") if isinstance(exc.detail, dict) else "PRODUCTION_ERROR"
                    await asyncio.to_thread(self._task_failure, task_id, code, str(exc.detail))
                except Exception as exc:
                    await self._checkpoint(batch_id)
                    await asyncio.to_thread(self._task_failure, task_id, "PRODUCTION_ERROR", "生产阶段异常：" + type(exc).__name__)
                task = find((await asyncio.to_thread(self.repository.state)), "tasks", task_id)
                await asyncio.to_thread(self._item_update, batch_id, task_id, task["status"], task["current_stage"])
                await self.event(task_id, task["current_stage"], "当前任务结束，继续队列", task["status"])
            await self._checkpoint(batch_id)
            batch = find((await asyncio.to_thread(self.repository.state)), "batches", batch_id)
            issues = any(item["status"] in {"FAILED", "WAITING_REVIEW", "CANCELLED", "QUARANTINED"} for item in batch["items"])
            await asyncio.to_thread(self._batch_update, batch_id, status="COMPLETED_WITH_ERRORS" if issues else "COMPLETED", active_task_id=None,
                               summary=await asyncio.to_thread(self.summary, batch["task_ids"]))
        except (ProductionStopped, asyncio.CancelledError):
            batch = find((await asyncio.to_thread(self.repository.state)), "batches", batch_id)
            shutdown_pause = self.closing and batch["status"] != "CANCELLED"
            for item in batch["items"]:
                if item["status"] in {"QUEUED", "RUNNING"}:
                    if shutdown_pause and item["status"] == "QUEUED":
                        continue
                    task = find((await asyncio.to_thread(self.repository.state)), "tasks", item["task_id"])
                    if task["status"] == "QUARANTINED":
                        await asyncio.to_thread(self._item_update, batch_id, task["id"], "QUARANTINED", task["current_stage"])
                        continue
                    if shutdown_pause:
                        runs = [row for row in (await asyncio.to_thread(self.repository.state))["runs"] if row["task_id"] == task["id"]]
                        safe_boundary = not any(row["status"] in {"queued", "running"} or row.get("error_code") == "UNKNOWN_EXTERNAL_REQUEST" for row in runs)
                        status = "QUEUED" if task["status"] == "RUNNING" and safe_boundary else task["status"]
                        if status == "RUNNING":
                            status = "WAITING_REVIEW"
                    else:
                        status = "WAITING_REVIEW" if task["status"] == "WAITING_REVIEW" else "CANCELLED"
                    await asyncio.to_thread(self._item_update, batch_id, task["id"], status, task["current_stage"])
                    if task["status"] != status:
                        await asyncio.to_thread(self._task_status, task["id"], status)
            if shutdown_pause:
                await asyncio.to_thread(self.repository.mutate, lambda state: audit(state, "production.shutdown_pause", batch_id,
                    "服务受控关闭；阶段边界保留原队列和已验证资产，暂停批次需显式恢复"))
            await asyncio.to_thread(self._batch_update, batch_id, status="PAUSED" if shutdown_pause else "CANCELLED", active_task_id=None,
                               summary=await asyncio.to_thread(self.summary, batch["task_ids"]))
        finally:
            self.cancels.pop(batch_id, None)

    async def _checkpoint(self, batch_id):
        while True:
            batch = find((await asyncio.to_thread(self.repository.state)), "batches", batch_id)
            if self.closing or self.cancels[batch_id].is_set() or batch["status"] == "CANCELLED":
                raise ProductionStopped()
            if batch["status"] != "PAUSED":
                return
            await asyncio.sleep(.25)

    async def _task_checkpoint(self, batch_id, task_id):
        await self._checkpoint(batch_id)
        task = find((await asyncio.to_thread(self.repository.state)), "tasks", task_id)
        if task["status"] in {"SKIPPED", "CANCELLED", "QUARANTINED"}:
            raise TaskStopped()
        production = task["context"].get("production", {})
        rejected = production.get("rejected_review", {})
        current = production.get("current_person_asset_id")
        active_rejection = bool(rejected and (not current or rejected.get("asset_id") == current
            or rejected.get("asset_id") == production.get("first_composition", {}).get("composition_asset_id")))
        if task["status"] == "REJECTED" or active_rejection:
            await asyncio.to_thread(self._task_status, task_id, "REJECTED", "review")
            raise TaskStopped()

    async def _task(self, batch_id, task_id):
        await self._task_checkpoint(batch_id, task_id)
        state = (await asyncio.to_thread(self.repository.state))
        task = find(state, "tasks", task_id)
        production = task["context"]["production"]
        if task["context"].get("collection_identity", {}).get("status") == "DUPLICATE_SKIPPED":
            return
        if production.get("asset_family_deleted"):
            await asyncio.to_thread(self._item_update, batch_id, task_id, "CANCELLED")
            return
        selected_person = next((row for row in state["assets"] if row["id"] == production.get("current_person_asset_id") and row["task_id"] == task_id and row["type"] == "person"), None)
        if production.get("requested_stage") == "saved_background_compose":
            if not self.manual_compose:
                await asyncio.to_thread(self._wait, task_id, "LOCAL_COMPOSE_NOT_CONNECTED", "compose", "本地保存背景合成器未连接；未调用旧合成器")
                return
            await self.manual_compose.compose_saved_backgrounds(batch_id, task_id)
            return
        manual_hold = task["context"].get("manual_compose", {}).get("auto_resume_required") or task["context"].get("production_strategy") == "manual_compose"
        if is_manual_review_asset(selected_person) or manual_hold and production.get("requested_stage") not in {"cutout", "person", "quality", "compose"}:
            await asyncio.to_thread(self._wait, task_id, "MANUAL_COMPOSE_REQUIRES_EXPLICIT_RESUME", task["current_stage"], "手动编辑后保持原队列等待；恢复普通母资产并明确继续，位置版本不交给旧合成器，未采集或请求付费背景")
            return
        if production.get("requested_stage") == "compose":
            await self._recompose_saved(batch_id, task)
            return
        if production.get("requested_stage") in {"cutout", "person", "prompt", "background", "quality"}:
            await self._requested_stage(batch_id, task)
            return
        result = task["collector_result"]
        if not result and not task["context"].get("task_context", {}).get("video_id"):
            expanded = await self.collector.expand_search(task_id, cancel_event=self.cancels[batch_id])
            if expanded.get("status") != "READY":
                await asyncio.to_thread(self._wait, task_id, expanded.get("error_code", "SEARCH_NOT_READY"), "collector",
                           expanded.get("error_message", "搜索尚未返回可处理来源；保留发现记录"))
                return
            await asyncio.to_thread(self._append_discovered, batch_id, task_id, expanded["task_ids"])
            task = find((await asyncio.to_thread(self.repository.state)), "tasks", task_id)
            production = task["context"]["production"]
            if task["context"].get("collection_identity", {}).get("status") == "DUPLICATE_SKIPPED":
                return
        semantic_check = getattr(self.collector, "can_retry_semantic", None)
        if production.get("retry") and semantic_check and await asyncio.to_thread(semantic_check, task_id):
            await self._task_checkpoint(batch_id, task_id)
            queued = await self.collector.queue(task_id, semantic_retry=True, cancel_event=self.cancels[batch_id])
            if queued.get("status") == "duplicate":
                return
            await self.collector.run(task_id, queued["run_id"])
            task = find((await asyncio.to_thread(self.repository.state)), "tasks", task_id)
            result = task["collector_result"]
        elif await asyncio.to_thread(self._download_resume, task_id):
            await self._task_checkpoint(batch_id, task_id)
            queued = await self.collector.queue(task_id, resume_download=True, cancel_event=self.cancels[batch_id])
            if queued.get("status") == "duplicate":
                return
            await self.collector.run(task_id, queued["run_id"])
            task = find((await asyncio.to_thread(self.repository.state)), "tasks", task_id)
            result = task["collector_result"]
        elif not result or result.get("error_code"):
            await asyncio.to_thread(self._task_status, task_id, "FAILED" if production["previous_status"] == "FAILED" else "CREATED")
            await self._task_checkpoint(batch_id, task_id)
            queued = await self.collector.queue(task_id, production["previous_status"] == "FAILED", cancel_event=self.cancels[batch_id])
            if queued.get("status") == "duplicate":
                return
            await self.collector.run(task_id, queued["run_id"])
            task = find((await asyncio.to_thread(self.repository.state)), "tasks", task_id)
            result = task["collector_result"]
        if task["status"] == "QUARANTINED" or task["context"].get("collection_identity", {}).get("status") == "DUPLICATE_SKIPPED":
            return
        approved_source = False
        if result.get("decision") == "manual_review" and result.get("decision_reason") == "watermark_unverified" and await asyncio.to_thread(self.source_review.approval_record, task_id):
            try:
                approved_source = await self.source_review.validate_approval(task_id, allow_active=True)
            except (HTTPException, OSError, ValueError, KeyError, TypeError):
                await asyncio.to_thread(self._task_status, task_id, "WAITING_REVIEW")
                await self.event(task_id, "review", "原片人工水印证据失效；未重复采集、下载或调用付费接口", status="WAITING_REVIEW")
                return
        if result.get("error_code") or result.get("decision") == "manual_review" and not approved_source:
            await asyncio.to_thread(self._task_status, task_id, "WAITING_REVIEW" if task["status"] != "FAILED" else "FAILED")
            return
        if result.get("decision") == "rejected":
            await asyncio.to_thread(self._task_status, task_id, "REJECTED")
            return
        if result.get("decision") != "accepted" and not approved_source:
            await asyncio.to_thread(self._task_failure, task_id, "DECISION_NOT_CONFIRMED", "旧采集结果未确认通过")
            return
        await asyncio.to_thread(self._stage_status, task_id, "analysis", "success")
        if production.get("requested_stage") in {"collector", "analysis"}:
            await asyncio.to_thread(self._task_status, task_id, "COLLECTED", "collector")
            return
        await self._checkpoint(batch_id)
        source = next((asset for asset in reversed(self.repository.state()["assets"]) if asset["task_id"] == task_id and asset["type"] == "source" and asset["registry_status"] == "registered" and self.business_asset(asset)), None)
        if not source:
            self._task_failure(task_id, "OUTPUT_NOT_FOUND", "通过的结果没有已验证原始视频")
            return
        row = {"video_id": result["video_id"], "url": result["video_url"], "context_id": result["context_id"],
               "vidbee_task_id": result["legacy_vidbee_task_id"], "download_path": source["external_path"] or source["storage_path"],
               "download_source_sha256": source["sha256"], "download_source_type": "vidbee_automation"}
        context = {**self.collector.context(task_id), "video_id": result["video_id"], "source_video": row["download_path"]}
        checked = await self._stage(batch_id, task_id, "download", "VidbeeDownloaderAdapter",
            lambda run_id, directory, callback: self.vidbee.verify(context, row, directory / run_id, callback, self.cancels[batch_id]))
        if not checked or checked["status"] != "READY":
            return
        if production.get("requested_stage") == "download":
            self._task_status(task_id, "COLLECTED", "download")
            return
        requested = production.get("requested_stage")
        person = self._asset(task_id, "person")
        if not person or requested in {"cutout", "person"}:
            outcome = await self._video_stage(batch_id, task_id, "cutout", context)
            if not outcome or outcome["status"] != "READY":
                return
            person = self._asset(task_id, "person")
            if not person:
                self._task_failure(task_id, "CUTOUT_OUTPUT_BOUNDARY", "工作流没有输出可复用的人像及 Alpha")
                return
        self._stage_status(task_id, "person", "success")
        quality = self._saved_person_quality(task_id, person)
        if quality is None:
            quality = await self._quality(batch_id, task_id, person)
        if not self._person_review_approved(task_id, person):
            self._ensure_person_review(task_id, person, quality)
            self._wait(task_id, "PERSON_QUALITY_REVIEW_REQUIRED", "review", "当前 Alpha 质量不确定或有异常；请修复或审核当前母资产版本")
            await self.event(task_id, "review", "母资产与真实 Alpha 质检已保存；等待当前 Person 版本人工审核，未调用提示词、背景或合成", status="WAITING_REVIEW")
            return
        saved_prompts = {row.get("ordinal"): row for row in self.repository.state()["prompts"] if row["task_id"] == task_id and row["status"] == "READY"}
        if set(saved_prompts) != set(range(1, 21)):
            prompt_result = await self._video_stage(batch_id, task_id, "prompt", context)
            if not prompt_result or prompt_result["status"] != "READY":
                return
        prompts = sorted((row for row in self.repository.state()["prompts"] if row["task_id"] == task_id and row["status"] != "MOCK"), key=lambda row: row.get("ordinal") or 0)
        latest = {row["ordinal"]: row for row in prompts}
        if set(latest) != set(range(1, 21)) or any(row["status"] != "READY" for row in latest.values()):
            self._task_failure(task_id, "PROMPT_SET_INVALID", "正常生产只允许已保存的 Prompt01–20；未生成背景")
            return
        for ordinal, prompt in sorted(latest.items()):
            await self._task_checkpoint(batch_id, task_id)
            job = {"id": f"BG{ordinal:02d}", "prompt": prompt["prompt"], "negative_prompt": prompt["negative_prompt"], "scene": prompt["scene_type"]}
            background = self._asset(task_id, "background", prompt["id"])
            if not background:
                outcome = await self._video_stage(batch_id, task_id, "background", {**context, "job": job}, prompt=prompt)
                if not outcome or outcome["status"] != "READY":
                    if ordinal == 1 or (outcome and outcome.get("error_code") == "UNKNOWN_EXTERNAL_REQUEST"):
                        self._hold_first_composition(task_id, "background", prompt, outcome)
                        return
                    continue
                background = self._asset(task_id, "background", prompt["id"])
            if background is None:
                if ordinal == 1:
                    self._hold_first_composition(task_id, "background", prompt, {"error_code": "BACKGROUND_ASSET_NOT_VERIFIED"})
                    return
                continue
            composition = self._asset(task_id, "compose", prompt["id"], person=person, background=background)
            if composition is None or requested == "compose":
                outcome = await self._video_stage(batch_id, task_id, "compose", {**context, "job": job, "person_asset": person, "background_asset": background}, prompt=prompt)
                if not outcome or outcome["status"] != "READY":
                    if ordinal == 1:
                        self._hold_first_composition(task_id, "compose", prompt, outcome)
                        return
                    continue
                composition = self._asset(task_id, "compose", prompt["id"], person=person, background=background)
            if composition:
                quality = await self._quality(batch_id, task_id, person, composition)
                if ordinal == 1:
                    if not quality or quality["status"] not in {"PASS", "FAIL"} or quality["status"] == "FAIL" and not self._person_review_approved(task_id, person):
                        self._hold_first_composition(task_id, "quality", prompt, {"error_code": "FIRST_COMPOSITION_QUALITY_NOT_PASS"})
                        return
                    try:
                        await asyncio.to_thread(self._first_composition_checkpoint, task_id, source, person, background, composition, quality)
                    except (OSError, ValueError, KeyError) as exc:
                        self._hold_first_composition(task_id, "compose", prompt, {"error_code": "FIRST_COMPOSITION_NOT_VERIFIED", "error_type": type(exc).__name__})
                        return
                    if not self._first_review_approved(task_id, person):
                        self._wait(task_id, "FIRST_COMPOSITION_REVIEW_REQUIRED", "review", "首条真实成片已保存；请验收，批准后自动生成并合成背景02–20")
                        await self.event(task_id, "review", "首条真实成片已保存；等待人工审核后才扩展同一母资产的背景02–20", status="WAITING_REVIEW")
                        return
            elif ordinal == 1:
                self._hold_first_composition(task_id, "compose", prompt, {"error_code": "COMPOSITION_ASSET_NOT_VERIFIED"})
                return
        reviews = [row for row in self.repository.state()["reviews"] if row["task_id"] == task_id]
        self._task_status(task_id, "WAITING_REVIEW" if reviews else "FAILED", "review")

    def _append_discovered(self, batch_id, parent_id, task_ids):
        def change(state):
            batch = find(state, "batches", batch_id)
            parent = find(state, "tasks", parent_id)
            for identifier in task_ids:
                if identifier in batch["task_ids"]:
                    continue
                task = find(state, "tasks", identifier)
                if task["status"] not in {"CREATED", "FAILED", "CANCELLED"}:
                    continue
                task["context"]["production"] = {**parent["context"]["production"], "previous_status": task["status"], "retry": False}
                task.update(status="QUEUED", updated_at=now())
                batch["task_ids"].append(identifier)
                batch["items"].append({"task_id": identifier, "status": "QUEUED", "stage": "collector"})
                present = {row["name"] for row in state["stages"] if row["task_id"] == identifier}
                for name in PRODUCTION_STAGES:
                    if name not in present:
                        state["stages"].append({"id": identifier + "-" + name, "task_id": identifier, "name": name,
                            "status": "not_connected" if name == "publish" else "pending", "start_time": None,
                            "end_time": None, "duration": 0, "retry_count": 0, "error": None})
            batch["updated_at"] = now()
        self.repository.mutate(change)

    async def _saved_context(self, task):
        task_id = task["id"]
        result = task.get("collector_result") or {}
        local = task.get("context", {}).get("local_cutout")
        approved = False
        if result.get("decision") == "manual_review" and result.get("decision_reason") == "watermark_unverified":
            approved = await self.source_review.validate_approval(task_id, allow_active=True)
        if result.get("decision") != "accepted" and not approved and not (local and local.get("full_decode_verified") is True):
            raise HTTPException(409, "SOURCE_NOT_APPROVED：请先审核真实原片，不会重新采集")
        source = next((asset for asset in reversed(self.repository.state()["assets"]) if asset["task_id"] == task_id
            and asset["type"] == "source" and asset["registry_status"] == "registered" and self.business_asset(asset)), None)
        if source is None:
            raise HTTPException(409, "SOURCE_VIDEO_NOT_REGISTERED")
        path, _ = self.registry.content(source["id"])
        if file_hash(path) != source["sha256"]:
            raise HTTPException(409, "SOURCE_VIDEO_HASH_CHANGED")
        if local:
            if (source["id"] != local.get("source_asset_id") or source["sha256"] != local.get("sha256")
                    or source.get("source_system") != "FactoryLocalUpload" or not path.is_relative_to(self.settings.storage_root.resolve())
                    or source["legacy_reference"].get("full_decode_verified") is not True):
                raise HTTPException(409, "LOCAL_SOURCE_PROOF_CHANGED")
            return source, {**task["context"]["task_context"], "video_id": "local_" + source["sha256"][:20], "source_video": str(path)}
        return source, {**self.collector.context(task_id), "video_id": source.get("legacy_video_id") or result.get("video_id"), "source_video": str(path)}

    def _saved_person_quality(self, task_id, person):
        state = self.repository.state()
        result = find(state, "tasks", task_id)["context"].get("production", {}).get("results", {}).get("quality", {})
        assessment = next((row for row in reversed(state["quality_assessments"]) if row["task_id"] == task_id
            and row["asset_id"] == person["id"] and row["processing_run_id"] == result.get("run_id")), None)
        run = next((row for row in state["runs"] if assessment and row["id"] == assessment["processing_run_id"]), None)
        if (not assessment or assessment["status"] not in {"PASS", "FAIL"} or not run
                or run["task_id"] != task_id or run["stage"] != "quality" or run["provider"] != "OpenCVCutoutQualityAnalyzer"
                or run["status"] != ("success" if assessment["status"] == "PASS" else "failed")
                or result.get("status") != assessment["status"] or not assessment["fingerprint"]
                or result.get("fingerprint") != assessment["fingerprint"] or not self._alpha_available(person)):
            return None
        try:
            path = self.registry.allowed_path(result["report_path"])
            report = json.loads(path.read_text())
            if (not path.is_relative_to(self.settings.storage_root.resolve()) or report.get("mode") != "REAL"
                    or report.get("provider") != "OpenCVCutoutQualityAnalyzer" or report.get("status") != assessment["status"]
                    or report.get("fingerprint") != assessment["fingerprint"]
                    or report.get("input_sha256") != person["legacy_reference"]["alpha_sha256"]
                    or result.get("input_sha256") != person["legacy_reference"]["alpha_sha256"]):
                return None
        except (OSError, ValueError, KeyError, HTTPException):
            return None
        return assessment

    def _ensure_person_review(self, task_id, person, quality):
        def change(state):
            if not any(row["task_id"] == task_id and row["asset_id"] == person["id"] for row in state["reviews"]):
                state["reviews"].append({"id": new_id("review"), "task_id": task_id, "asset_id": person["id"],
                    "status": "pending", "issues": [row.get("issue", row.get("type", "UNKNOWN")) for row in (quality or {}).get("issue_segments", [])],
                    "note": "当前母资产需人工确认；几何质检 " + (quality or {}).get("status", "UNKNOWN") + "，保留原始质检记录"})
            task_stage(state, task_id, "review").update(status="waiting_review")
        self.repository.mutate(change)

    def _person_review_approved(self, task_id, person):
        state = self.repository.state()
        task = find(state, "tasks", task_id)
        production = task["context"].get("production", {})
        rejection = production.get("rejected_review", {})
        if (task["status"] == "REJECTED" or rejection.get("asset_id") == person["id"] or not self.business_asset(person)
                or person["task_id"] != task_id or person["type"] != "person" or person["registry_status"] != "registered"
                or person["status"] in {"rejected", "failed"} or person["legacy_reference"].get("superseded_by_person_asset_id")
                or production.get("current_person_asset_id", person["id"]) != person["id"] or not self._alpha_available(person)):
            return False
        try:
            path, _ = self.registry.content(person["id"])
            manifest = self.registry.allowed_path(person["legacy_reference"]["manifest_path"])
            if file_hash(path) != person["sha256"] or file_hash(manifest) != person["legacy_reference"]["manifest_sha256"]:
                return False
        except (OSError, ValueError, KeyError, HTTPException):
            return False
        reviews = [row for row in state["reviews"] if row["task_id"] == task_id and row["asset_id"] == person["id"]]
        if reviews:
            def review_order(pair):
                parts = pair[1].get("id", "").split("-")
                timestamp = parts[1] if len(parts) > 1 and parts[1].isdigit() else ""
                created = "".join(character for character in str(pair[1].get("created_at") or "") if character.isdigit())[:20]
                return created or timestamp, pair[0]
            latest = max(enumerate(reviews), key=review_order)[1]
            return latest["status"] == "approved"
        quality = self._saved_person_quality(task_id, person)
        return bool(quality and quality["status"] == "PASS" and not self.policy()["person_review_required"])

    async def _requested_stage(self, batch_id, task):
        task_id = task["id"]
        production = task["context"]["production"]
        stage = production["requested_stage"]
        try:
            _, context = await self._saved_context(task)
        except (HTTPException, OSError, ValueError, KeyError) as exc:
            self._task_failure(task_id, "SAVED_SOURCE_NOT_VERIFIED", str(exc.detail) if isinstance(exc, HTTPException) else "原片证据不可用；不会重新采集或下载")
            return
        if stage in {"cutout", "person"}:
            person = None if production.get("retry") else self._asset(task_id, "person")
            if person is None:
                outcome = await self._video_stage(batch_id, task_id, "cutout", context)
                if not outcome or outcome["status"] != "READY":
                    return
                person = self._asset(task_id, "person")
            if person is None:
                self._task_failure(task_id, "CUTOUT_OUTPUT_BOUNDARY", "未登记真实 RGB + Alpha 母资产")
                return
            self._stage_status(task_id, "person", "success")
            await self._quality(batch_id, task_id, person)
            if task["context"].get("local_cutout"):
                self._ensure_person_review(task_id, person, self._saved_person_quality(task_id, person))
                self._wait(task_id, "LOCAL_PERSON_REVIEW_REQUIRED", "review", "本地原片抠像及质量检测已完成；审核人物后可在合成工作台复用，未请求任何付费背景")
                return
            if production.get("automatic") and self._person_review_approved(task_id, person):
                self.repository.mutate(lambda state: find(state, "tasks", task_id)["context"]["production"].update(requested_stage=None, retry=False))
                await self._task(batch_id, task_id)
            else:
                self._wait(task_id, "PERSON_QUALITY_REVIEW_REQUIRED", "review", "质检异常区间已保存；修复或审核后自动继续背景和合成")
            return
        if stage == "quality":
            person = self._asset(task_id, "person")
            if person is None:
                self._task_failure(task_id, "PERSON_ASSET_REQUIRED", "需要已保存 RGB + Alpha 母资产；未自动抠像")
                return
            await self._quality(batch_id, task_id, person)
            if production.get("automatic") and self._person_review_approved(task_id, person):
                self.repository.mutate(lambda state: find(state, "tasks", task_id)["context"]["production"].update(requested_stage=None, retry=False))
                await self._task(batch_id, task_id)
            else:
                self._wait(task_id, "PERSON_QUALITY_REVIEW_REQUIRED", "review", "质检已保存；需要审核当前母资产版本")
            return
        if stage == "prompt":
            outcome = await self._video_stage(batch_id, task_id, "prompt", context)
            if outcome and outcome["status"] == "READY":
                prompts = {row.get("ordinal"): row for row in self.repository.state()["prompts"] if row["task_id"] == task_id and row["status"] != "MOCK"}
                if set(prompts) != set(range(1, 21)) or any(row["status"] != "READY" for row in prompts.values()):
                    self._task_failure(task_id, "PROMPT_SET_INVALID", "未保存完整的20组真实提示词；不会自动生成背景")
                else:
                    current = find(self.repository.state(), "tasks", task_id)
                    if current["status"] == "REJECTED" or current["context"]["production"].get("rejected_review"):
                        self._task_status(task_id, "REJECTED", "review")
                        return
                    person = self._asset(task_id, "person")
                    if (person is not None and not self._person_review_approved(task_id, person)
                            or person is None and current["context"]["production"].get("current_person_asset_id")):
                        self._task_status(task_id, "WAITING_REVIEW", "review")
                        await self.event(task_id, "review", "20组提示词已保存；当前母资产仍待人工审核，未生成背景或合成", status="WAITING_REVIEW")
                    else:
                        self._task_status(task_id, "COLLECTED", "prompt")
            return
        person = self._asset(task_id, "person")
        if person is None or not self._person_review_approved(task_id, person):
            current = find(self.repository.state(), "tasks", task_id)
            status = "REJECTED" if current["status"] == "REJECTED" or current["context"]["production"].get("rejected_review") else "WAITING_REVIEW"
            self._task_status(task_id, status, "review")
            await self.event(task_id, "review", "PERSON_REVIEW_REQUIRED：当前母资产未批准或证据已变化，未请求付费背景", status=status)
            return
        prompt_id = production.get("requested_prompt_id")
        prompts = [row for row in self.repository.state()["prompts"] if row["task_id"] == task_id and row["status"] == "READY"]
        prompt = next((row for row in prompts if row["id"] == prompt_id), None) if prompt_id else next((row for row in prompts if row.get("ordinal") == 1), None)
        if prompt is None:
            self._task_failure(task_id, "PROMPT_REQUIRED", "请选择该任务已保存的提示词；未自动生成提示词")
            return
        job = {"id": f"BG{prompt['ordinal']:02d}", "prompt": prompt["prompt"], "negative_prompt": prompt["negative_prompt"], "scene": prompt["scene_type"]}
        background = self._asset(task_id, "background", prompt["id"])
        if background is None:
            outcome = await self._video_stage(batch_id, task_id, "background", {**context, "job": job}, prompt=prompt)
            if not outcome or outcome["status"] != "READY":
                return
            background = self._asset(task_id, "background", prompt["id"])
        if background is None:
            self._task_failure(task_id, "BACKGROUND_ASSET_NOT_VERIFIED", "请求没有真实背景文件，未登记完成")
            return
        self._task_status(task_id, "COLLECTED", "background")

    def _hold_first_composition(self, task_id, stage, prompt, outcome):
        code = (outcome or {}).get("error_code") or "FIRST_COMPOSITION_FAILED"
        def change(state):
            task = find(state, "tasks", task_id)
            task["context"]["production"]["first_composition"] = {"status": "BLOCKED", "stage": stage,
                "prompt_id": prompt["id"], "error_code": code, "updated_at": now(), "background_limit": 20}
            task.update(status="WAITING_REVIEW", current_stage=stage, updated_at=now())
        self.repository.mutate(change)

    def _first_review_approved(self, task_id, person=None):
        state = self.repository.state()
        production = find(state, "tasks", task_id)["context"].get("production", {})
        checkpoint = production.get("first_composition", {})
        rejection = production.get("rejected_review", {})
        if checkpoint.get("status") not in {"PASS", "VERIFIED"} or rejection.get("asset_id") in {
                checkpoint.get("composition_asset_id"), production.get("current_person_asset_id")} and rejection:
            return False
        person_id = person["id"] if person else production.get("current_person_asset_id")
        if person_id and checkpoint.get("person_asset_id") != person_id:
            return False
        asset = next((row for row in state["assets"] if row["id"] == checkpoint.get("composition_asset_id")), None)
        if (not asset or not self.business_asset(asset) or asset.get("sha256") != checkpoint.get("composition_sha256")
                or asset.get("legacy_reference", {}).get("superseded_by_person_asset_id")):
            return False
        try:
            source = next((row for row in state["assets"] if row["task_id"] == task_id and row["type"] == "source" and self.business_asset(row)
                and row["registry_status"] == "registered" and row.get("sha256") == checkpoint.get("source_sha256")), None)
            if not source or file_hash(self.registry.content(source["id"])[0]) != checkpoint.get("source_sha256"):
                return False
            for kind, identifier, expected in (("compose", checkpoint.get("composition_asset_id"), checkpoint.get("composition_sha256")),
                    ("person", checkpoint.get("person_asset_id"), checkpoint.get("rgb_sha256")),
                    ("background", checkpoint.get("background_asset_id"), checkpoint.get("background_sha256"))):
                item = next((row for row in state["assets"] if row["id"] == identifier), None)
                if (not item or item["task_id"] != task_id or item["type"] != kind or item["registry_status"] != "registered"
                        or not self.business_asset(item) or item["status"] in {"rejected", "failed"}
                        or item["sha256"] != expected or file_hash(self.registry.content(identifier)[0]) != expected):
                    return False
                if kind == "person" and (item["legacy_reference"].get("alpha_sha256") != checkpoint.get("alpha_sha256") or not self._alpha_available(item)):
                    return False
        except (HTTPException, OSError, ValueError, KeyError):
            return False
        reviews = [row for row in state["reviews"] if row["task_id"] == task_id and row["asset_id"] == asset["id"]]
        def review_order(pair):
            parts = pair[1].get("id", "").split("-")
            timestamp = parts[1] if len(parts) > 1 and parts[1].isdigit() else ""
            created = "".join(character for character in str(pair[1].get("created_at") or "") if character.isdigit())[:20]
            return created or timestamp, pair[0]
        return bool(reviews and max(enumerate(reviews), key=review_order)[1]["status"] == "approved")

    def _first_composition_checkpoint(self, task_id, source, person, background, composition, quality):
        for asset in (source, person, background, composition):
            if asset.get("task_id") != task_id or asset.get("registry_status") != "registered" or not self.business_asset(asset):
                raise ValueError("FIRST_COMPOSITION_ASSET_NOT_REAL")
            path, _ = self.registry.content(asset["id"])
            if file_hash(path) != asset["sha256"]:
                raise ValueError("FIRST_COMPOSITION_ASSET_HASH_MISMATCH")
        reference = person["legacy_reference"]
        alpha_path = self.registry.allowed_path(reference["alpha_path"])
        person_manifest = self.registry.allowed_path(reference["manifest_path"])
        if file_hash(alpha_path) != reference["alpha_sha256"] or file_hash(person_manifest) != reference["manifest_sha256"]:
            raise ValueError("FIRST_COMPOSITION_PERSON_EVIDENCE_CHANGED")
        manifest_path = self.registry.allowed_path(composition["legacy_reference"]["manifest_path"])
        if not manifest_path.is_relative_to(self.settings.storage_root.resolve()):
            raise ValueError("FIRST_COMPOSITION_MANIFEST_OUTSIDE_FACTORY")
        manifest = json.loads(manifest_path.read_text())
        frames = person["probe_metadata"]["frame_count"]
        if (manifest.get("schema") != "factory.composition.v1" or manifest.get("status") != "READY"
                or manifest.get("full_decode_verified") is not True or manifest.get("cutout_inference_executions") != 0
                or manifest.get("task_id") != task_id or manifest.get("person_asset_id") != person["id"]
                or manifest.get("background_asset_id") != background["id"] or manifest.get("rgb_sha256") != person["sha256"]
                or manifest.get("alpha_sha256") != person["legacy_reference"]["alpha_sha256"]
                or person["legacy_reference"]["source_sha256"] != source["sha256"] or manifest.get("source_sha256") != source["sha256"]
                or manifest.get("background_sha256") != background["sha256"] or manifest.get("output_sha256") != composition["sha256"]
                or not frames or manifest.get("output_media", {}).get("frame_count") != frames or manifest.get("source_media", {}).get("frame_count") != frames):
            raise ValueError("FIRST_COMPOSITION_MEDIA_EVIDENCE_MISMATCH")
        state = self.repository.state()
        task = find(state, "tasks", task_id)
        if manifest.get("context_id") != task["context"]["task_context"]["context_id"]:
            raise ValueError("FIRST_COMPOSITION_CONTEXT_MISMATCH")
        persisted_quality = next((row for row in state["quality_assessments"] if row["id"] == quality["id"]), None)
        run = next((row for row in state["runs"] if row["id"] == quality["processing_run_id"]), None)
        result = task["context"]["production"].get("results", {}).get("quality", {})
        if (not persisted_quality or persisted_quality["task_id"] != task_id or persisted_quality["asset_id"] != person["id"]
                or persisted_quality["status"] not in {"PASS", "FAIL"} or not run or run["task_id"] != task_id or run["stage"] != "quality"
                or run["status"] != ("success" if persisted_quality["status"] == "PASS" else "failed")
                or persisted_quality["status"] == "FAIL" and not self._person_review_approved(task_id, person)
                or result.get("status") != persisted_quality["status"] or result.get("run_id") != run["id"] or result.get("input_sha256") != reference["alpha_sha256"]
                or not persisted_quality["fingerprint"] or result.get("fingerprint") != persisted_quality["fingerprint"]):
            raise ValueError("FIRST_COMPOSITION_QUALITY_EVIDENCE_MISMATCH")
        report_path = self.registry.allowed_path(result["report_path"])
        report = json.loads(report_path.read_text())
        if (not report_path.is_relative_to(self.settings.storage_root.resolve()) or report.get("status") != persisted_quality["status"]
                or report.get("fingerprint") != persisted_quality["fingerprint"] or report.get("input_sha256") != reference["alpha_sha256"]):
            raise ValueError("FIRST_COMPOSITION_QUALITY_REPORT_MISMATCH")
        basis = {"source_sha256": source["sha256"], "person_asset_id": person["id"], "rgb_sha256": person["sha256"],
            "alpha_sha256": person["legacy_reference"]["alpha_sha256"], "background_asset_id": background["id"], "background_sha256": background["sha256"],
            "composition_asset_id": composition["id"], "composition_sha256": composition["sha256"], "manifest_sha256": file_hash(manifest_path),
            "quality_fingerprint": persisted_quality["fingerprint"], "quality_report_sha256": file_hash(report_path)}
        fingerprint = hashlib.sha256(json.dumps(basis, sort_keys=True).encode()).hexdigest()
        checkpoint = {**basis, "fingerprint": fingerprint, "status": "PASS" if persisted_quality["status"] == "PASS" else "VERIFIED",
            "geometry_status": persisted_quality["status"], "semantic_status": "WAITING_HUMAN_REVIEW", "quality_id": quality["id"], "quality_processing_run_id": run["id"],
            "composition_processing_run_id": composition["legacy_reference"]["processing_run_id"], "manifest_path": str(manifest_path),
            "full_decode_verified": True, "background_limit": 20, "human_review": "PENDING", "quality_report_path": str(report_path), "verified_at": now()}
        def change(current):
            production = find(current, "tasks", task_id)["context"]["production"]
            checkpoints = production.setdefault("first_composition_checkpoints", [])
            saved = next((row for row in checkpoints if row["fingerprint"] == fingerprint), None)
            if saved is None:
                checkpoints.append(checkpoint)
            selected = production.get("first_composition", {})
            production["first_composition"] = selected if selected.get("fingerprint") == fingerprint and selected.get("human_review") == "APPROVED" else saved or checkpoint
        self.repository.mutate(change)

    async def _recompose_saved(self, batch_id, task):
        """A review repair cannot restart search, download, prompt or background requests."""
        task_id = task["id"]
        state = self.repository.state()
        production = task["context"].get("production", {})
        current_person = production.get("requested_person_asset_id") or production.get("current_person_asset_id")
        person = next((asset for asset in reversed(state["assets"]) if asset["task_id"] == task_id and asset["type"] == "person"
                       and (not current_person or asset["id"] == current_person) and asset["registry_status"] == "registered" and self.business_asset(asset)), None)
        source = next((asset for asset in state["assets"] if asset["task_id"] == task_id and asset["type"] == "source" and asset["registry_status"] == "registered" and self.business_asset(asset)), None)
        backgrounds = [asset for asset in state["assets"] if asset["task_id"] == task_id and asset["type"] == "background" and asset["registry_status"] == "registered" and self.business_asset(asset)]
        if not person or not source or not backgrounds or not self._alpha_available(person):
            self._task_failure(task_id, "SAVED_COMPOSE_INPUT_REQUIRED", "需要已验证原片、RGB/Alpha 母资产和已有背景；不会自动生成或重新采集")
            return
        self.registry.content(person["id"])
        self.registry.content(source["id"])
        try:
            source, context = await self._saved_context(task)
        except (HTTPException, OSError, ValueError, KeyError):
            self._task_failure(task_id, "SAVED_SOURCE_NOT_VERIFIED", "原片证据不可用；未重新采集或下载")
            return
        latest = {asset["legacy_reference"].get("prompt_id"): asset for asset in backgrounds}
        if production.get("requested_prompt_id"):
            latest = {key: value for key, value in latest.items() if key == production["requested_prompt_id"]}
        completed = 0
        for prompt_id, background in sorted(latest.items(), key=lambda pair: next((row.get("ordinal", 0) for row in state["prompts"] if row["id"] == pair[0]), 0)):
            await self._task_checkpoint(batch_id, task_id)
            prompt = next((row for row in state["prompts"] if row["id"] == prompt_id), None)
            if not prompt or prompt["status"] == "MOCK":
                self._task_failure(task_id, "BACKGROUND_PROMPT_ASSOCIATION_MISSING", "已有背景缺少真实 Prompt 关联；没有发送新的外部请求")
                continue
            if prompt.get("ordinal", 1) > 1 and not self._first_review_approved(task_id, person):
                self._task_status(task_id, "WAITING_REVIEW", "review")
                return
            job = {"id": f"BG{prompt['ordinal']:02d}", "prompt": prompt["prompt"], "negative_prompt": prompt["negative_prompt"], "scene": prompt["scene_type"]}
            outcome = await self._video_stage(batch_id, task_id, "compose", {**context, "job": job, "person_asset": person, "background_asset": background}, prompt=prompt)
            if outcome and outcome["status"] == "READY":
                composition = self._asset(task_id, "compose", prompt_id, person=person, background=background)
                if composition:
                    quality = await self._quality(batch_id, task_id, person, composition)
                    if prompt.get("ordinal") == 1:
                        if not quality or quality["status"] != "PASS":
                            self._hold_first_composition(task_id, "quality", prompt, {"error_code": "FIRST_COMPOSITION_QUALITY_NOT_PASS"})
                            return
                        try:
                            self._first_composition_checkpoint(task_id, source, person, background, composition, quality)
                        except (OSError, ValueError, KeyError):
                            self._hold_first_composition(task_id, "compose", prompt, {"error_code": "FIRST_COMPOSITION_NOT_VERIFIED"})
                            return
                        if not self._first_review_approved(task_id, person):
                            self._task_status(task_id, "WAITING_REVIEW", "review")
                            return
                    completed += 1
        if completed:
            self._task_status(task_id, "WAITING_REVIEW", "review")
        else:
            self._task_failure(task_id, "SAVED_COMPOSITIONS_NOT_COMPLETED", "未生成可核验的新成片；原母资产、背景与失败执行记录保留")

    async def _video_stage(self, batch_id, task_id, stage, context, prompt=None):
        context = {**context, "external_budget_required": True, "external_budget_root": str(self.settings.storage_root)}
        if stage == "background" and self.policy()["background_strategy"] == "manual":
            self._wait(task_id, "MANUAL_BACKGROUND_REQUIRED", stage, "请为当前提示词上传背景；保存后自动继续合成，不重新生成提示词")
            return {"status": "WAITING_MANUAL", "error_code": "MANUAL_BACKGROUND_REQUIRED", "retryable": False}
        names = STAGE_REQUIREMENTS[stage]
        checked = await self.readiness.check(refresh=True, stage=stage, task_id=task_id)
        missing = [row for row in checked["checks"] if row.get("name") in names and row["status"] != "READY"]
        if missing or not names <= {row["name"] for row in checked["checks"]}:
            self._wait(task_id, "DEPENDENCY_NOT_READY", stage, "；".join(row.get("message", row["name"]) for row in missing) or "缺少当前阶段依赖")
            await self.event(task_id, stage, "等待当前阶段依赖；已完成步骤和资产保留", status="WAITING_MANUAL", data={"checks": missing})
            return {"status": "WAITING_MANUAL", "error_code": "DEPENDENCY_NOT_READY", "retryable": False}
        provider = self.providers[stage]
        if stage in {"cutout", "person"}:
            production = find(self.repository.state(), "tasks", task_id)["context"]["production"]
            context = {**context, "cutout_generation": production.get("cutout_generation", 1),
                       "object_strategy": production.get("object_strategy", {})}
            checkpoint = production.get("cutout_checkpoint_resume", {})
            if stage == "cutout" and checkpoint.get("status") == "AUTHORIZED":
                context["cutout_checkpoint_resume"] = checkpoint["pinned"]
        attempts = self.policy()["retry_limit"] if stage in {"cutout", "person", "compose"} else 0
        for attempt in range(attempts + 1):
            result = await self._stage(batch_id, task_id, stage, type(provider).__name__,
                lambda run_id, directory, callback: provider.execute(stage, {**context, "run_id": run_id}, directory,
                    callback, self.cancels[batch_id]), prompt=prompt, context=context)
            if (result.get("status") != "FAILED" or not result.get("retryable")
                    or result.get("error_code") == "UNKNOWN_EXTERNAL_REQUEST" or attempt == attempts):
                return result
            await self.event(task_id, stage, f"本地可重试错误，安全恢复 {attempt + 1}/{attempts}；沿用 journal 与输入指纹")
            await asyncio.sleep(min(2 ** attempt, 5))

    async def _stage(self, batch_id, task_id, stage, provider, invoke, *, prompt=None, context=None):
        await self._task_checkpoint(batch_id, task_id)
        run_id, started = new_id("run"), time.monotonic()
        task = find(self.repository.state(), "tasks", task_id)
        paths = StoragePaths(self.settings.storage_root).task_paths(task["project_id"], task_id)
        generation_key = "cutout_generation" if stage in {"cutout", "person"} else "compose_generation"
        generation = task["context"]["production"].get(generation_key, 1)
        cache_scope = "shared" if stage in {"prompt", "background"} else f"v{generation}"
        directory = Path(paths["metadata"]) / "production" / cache_scope / stage
        if prompt:
            directory = directory / prompt["fingerprint"]
        def begin(state):
            task = find(state, "tasks", task_id)
            if task["status"] == "QUARANTINED":
                raise TaskStopped()
            task["context"]["production"].update(active_stage=stage, active_run_id=run_id,
                active_prompt_id=prompt["id"] if prompt else None)
            stage_row = task_stage(state, task_id, stage)
            retry = sum(run["task_id"] == task_id and run["stage"] == stage for run in state["runs"])
            stage_row.update(status="running", start_time=now(), end_time=None, error=None, retry_count=retry)
            task.update(status="RUNNING", current_stage=stage, updated_at=now(), progress=PRODUCTION_STAGES.index(stage) / 11 * 100)
            state["runs"].append({"id": run_id, "task_id": task_id, "stage": stage, "provider": provider, "status": "running",
                "progress": 0, "logs": [], "created_at": now(), "started_at": now(), "retry_count": retry,
                "idempotency_key": f"{task_id}:{stage}:" + hashlib.sha256(json.dumps({"context": context,
                    "generation": generation, "prompt": prompt.get("fingerprint") if prompt else None}, sort_keys=True, default=str).encode()).hexdigest(),
                "last_heartbeat": now()})
        self.repository.mutate(begin)
        self._item_update(batch_id, task_id, "RUNNING", stage)
        loop = asyncio.get_running_loop()
        event_lock, event_futures, process = asyncio.Lock(), [], {}
        async def persist_event(value):
            async with event_lock:
                await self._process_event(task_id, run_id, stage, value)
        def callback(value):
            if value.get("type") in {"PROCESS_STARTED", "process_finished"}:
                process.update({key: value[key] for key in ("command", "cwd", "pid", "stdout_log_path", "stderr_log_path",
                    "exit_code", "termination_signal", "timed_out", "resource_info") if key in value})
            event_futures.append(asyncio.run_coroutine_threadsafe(persist_event(dict(value)), loop))
        async def drain_events():
            completed = await asyncio.gather(*(asyncio.wrap_future(future) for future in event_futures), return_exceptions=True)
            return [value for value in completed if isinstance(value, BaseException)]
        result = None
        event_errors = []
        try:
            result = await invoke(run_id, directory, callback)
            process.update(result.pop("_process", {}))
            event_errors = await drain_events()
            if event_errors:
                raise event_errors[0]
            await asyncio.to_thread(self._record_result, task_id, run_id, stage, result, process, started, prompt, context)
            if self.cancels[batch_id].is_set():
                raise ProductionStopped()
            return result
        except ProductionStopped:
            raise
        except Exception as exc:
            if not event_errors:
                event_errors = await drain_events()
            external_unknown = (result is None and stage in {"prompt", "background"} and provider != "ManualUpload") or (
                isinstance(result, dict) and result.get("error_code") == "UNKNOWN_EXTERNAL_REQUEST")
            result = {"status": "WAITING_REVIEW" if external_unknown else "FAILED",
                      "error_code": "UNKNOWN_EXTERNAL_REQUEST" if external_unknown else "PROCESS_EVENT_PERSISTENCE_FAILED" if event_errors else "STAGE_RESULT_INVALID",
                      "error_message": "外部请求结果未确认，禁止自动重新提交" if external_unknown else type(exc).__name__,
                      "retryable": False}
            await asyncio.to_thread(self._record_result, task_id, run_id, stage, result, process, started, prompt, context)
            return result

    async def _process_event(self, task_id, run_id, stage, value):
        message = value.get("text") or value.get("message") or value.get("stage") or value.get("type", "")
        fields = {"last_heartbeat": now()}
        if value.get("progress") is not None:
            fields["progress"] = value["progress"]
        if value.get("type") == "PROCESS_STARTED":
            fields.update({key: value[key] for key in ("command", "cwd", "pid", "stdout_log_path", "stderr_log_path")
                           if value.get(key)})
        if value.get("type") == "process_finished":
            fields.update(termination_signal=value.get("termination_signal"), timed_out=value.get("timed_out", False),
                          resource_info=value.get("resource_info", {}))
        await asyncio.to_thread(self.repository.update_processing_run_event, task_id, run_id, fields,
            log_message=None if value.get("type") == "PROCESS_HEARTBEAT" else str(message))
        if value.get("type") != "PROCESS_HEARTBEAT":
            await self.event(task_id, stage, str(message), run_id=run_id, progress=value.get("progress"))

    def save_snapshot(self, task_id, run_id, snapshot):
        if not snapshot:
            return None
        root = self.settings.storage_root.resolve()
        path = Path(snapshot["path"]).resolve()
        if (not path.is_relative_to(root) or not path.is_file() or file_hash(path) != snapshot["sha256"]
                or snapshot.get("task_id") != task_id or snapshot.get("processing_run_id") != run_id):
            raise ValueError("WORKFLOW_SNAPSHOT_NOT_VERIFIED")
        graph = json.loads(path.read_text())
        if not isinstance(graph, dict) or not graph:
            raise ValueError("WORKFLOW_SNAPSHOT_GRAPH_INVALID")
        for node in graph.values():
            if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
                raise ValueError("WORKFLOW_SNAPSHOT_GRAPH_INVALID")
            if any(credential_field(name, value) for name, value in node["inputs"].items()):
                raise ValueError("WORKFLOW_SNAPSHOT_CONTAINS_SECRET")
        keys = ("submitted", "cache_hit", "origin_processing_run_id", "origin_task_id", "origin_history_path", "origin_history_sha256")
        execution = {key: snapshot[key] for key in keys if key in snapshot}
        execution.update(path=str(path), sha256=snapshot["sha256"])
        if any(key in execution and type(execution[key]) is not bool for key in ("submitted", "cache_hit")):
            raise ValueError("WORKFLOW_SNAPSHOT_EXECUTION_FLAGS_INVALID")
        if execution.get("cache_hit") is True and execution.get("submitted") is not False:
            raise ValueError("WORKFLOW_CACHE_CANNOT_CLAIM_SUBMISSION")
        if snapshot.get("prompt_id") or any(key in execution for key in keys[2:]):
            if not all(execution.get(key) for key in keys[2:]) or not snapshot.get("prompt_id"):
                raise ValueError("WORKFLOW_ORIGIN_EVIDENCE_REQUIRED")
            history_path = Path(execution["origin_history_path"]).resolve()
            if not history_path.is_relative_to(root) or not history_path.is_file() or file_hash(history_path) != execution["origin_history_sha256"]:
                raise ValueError("WORKFLOW_ORIGIN_HISTORY_NOT_VERIFIED")
            origin = json.loads(history_path.read_text())
            history = origin.get("history", {})
            history_prompt = history.get("prompt", [])
            origin_graph = Path(origin.get("workflow_path", "")).resolve()
            if not origin_graph.is_relative_to(root):
                raise ValueError("WORKFLOW_ORIGIN_IDENTITY_OR_GRAPH_MISMATCH")
            if not origin_graph.is_file():
                origin_graph = history_path.parent / origin_graph.name
            if (origin.get("processing_run_id") != execution["origin_processing_run_id"] or origin.get("task_id") != execution["origin_task_id"]
                    or origin.get("prompt_id") != snapshot["prompt_id"] or origin.get("submitted_graph_sha256") != snapshot["sha256"]
                    or len(history_prompt) < 3 or history_prompt[1] != snapshot["prompt_id"] or history_prompt[2] != graph
                    or history.get("status", {}).get("completed") is not True or history["status"].get("status_str") == "error"
                    or not origin_graph.is_relative_to(root) or not origin_graph.is_file() or file_hash(origin_graph) != snapshot["sha256"]):
                raise ValueError("WORKFLOW_ORIGIN_IDENTITY_OR_GRAPH_MISMATCH")
            execution["origin_history_path"] = str(history_path)
            execution.update(origin_workflow_path=origin["workflow_path"], resolved_origin_workflow_path=str(origin_graph))
        elif execution.get("submitted") is True or execution.get("cache_hit") is True:
            raise ValueError("WORKFLOW_ORIGIN_EVIDENCE_REQUIRED")
        value = {"id": snapshot["workflow_snapshot_id"], "task_id": task_id,
            "version": snapshot["sha256"][:16], "workflow": graph, "created_at": now(),
            "processing_run_id": run_id, "prompt_id": snapshot.get("prompt_id"), "sha256": snapshot["sha256"], "execution": execution}
        existing = next((row for row in self.repository.workflows(task_id) if row["id"] == value["id"]), None)
        if existing:
            if any(existing.get(key) != value[key] for key in value if key != "created_at"):
                raise ValueError("WORKFLOW_SNAPSHOT_ID_CONFLICT")
            return existing
        self.repository.save_workflow(value)
        return value

    def _record_result(self, task_id, run_id, stage, result, process, started, prompt, context):
        task = find(self.repository.state(), "tasks", task_id)
        self.save_snapshot(task_id, run_id, result.get("metadata", {}).get("workflow_snapshot"))
        for record in result.get("prompts", []):
            self._register_prompt(task, record, run_id)
        for evidence in result.get("assets", []):
            kind = {"BACKGROUND": "background", "BACKGROUND_IMAGE": "background", "PERSON_VIDEO": "person", "COMPOSITION_VIDEO": "compose"}.get(evidence["asset_type"])
            if kind is None:
                raise ValueError("UNSUPPORTED_ASSET_TYPE")
            evidence = {**evidence, "legacy_reference": {**evidence.get("legacy_reference", {}),
                "factory_fingerprint": self._fingerprint(task, kind, prompt["id"] if prompt else None,
                    (context or {}).get("person_asset"), (context or {}).get("background_asset"))}}
            asset = self.registry.register(task, evidence, kind, run_id, prompt_id=prompt["id"] if prompt else None,
                                           person=(context or {}).get("person_asset"), background=(context or {}).get("background_asset"))
            if kind == "person":
                self.repository.mutate(lambda state: find(state, "tasks", task_id)["context"]["production"].update(current_person_asset_id=asset["id"], review_approved=False))
        success = result["status"] in {"READY", "PASS"}
        message = result.get("error_message") or result.get("message") or result.get("error_code") or result["status"]
        error_code, error_message = result.get("error_code"), result.get("error_message")
        execution = None
        if stage == "quality" and find(self.repository.state(), "runs", run_id)["provider"] == "OpenCVCutoutQualityAnalyzer":
            cached = result.get("cache_hit") is True
            execution = {"mode": "CACHE_REUSE" if cached else "PROCESS" if process.get("pid") else "PRECHECK_NO_EXECUTION",
                "cache_hit": cached, **{key: result.get(key) for key in ("asset_id", "input_sha256", "fingerprint", "report_path")}}
            if result.get("report_path") and result["status"] in {"PASS", "FAIL"}:
                report_path = Path(result["report_path"]).resolve()
                if not report_path.is_relative_to(self.settings.storage_root.resolve()) or not report_path.is_file():
                    raise ValueError("QUALITY_REPORT_NOT_VERIFIED")
                report = json.loads(report_path.read_text())
                if any(report.get(key) != result.get(key) for key in ("status", "fingerprint", "input_sha256")):
                    raise ValueError("QUALITY_REPORT_IDENTITY_MISMATCH")
                execution["report_sha256"] = file_hash(report_path)
            issues = sorted({item.get("issue", item.get("type", "UNKNOWN")) for item in result.get("issue_segments", [])})
            prefix = "缓存复用，未启动检测进程" if cached else "真实检测进程" if process.get("pid") else "质检前置检查，未启动检测进程"
            message = f"{prefix}：几何质检 {result['status']}；问题=" + (", ".join(issues) or "无/UNKNOWN")
            if result.get("error_code"):
                message += "；原因=" + result["error_code"]
            for key in ("asset_id", "input_sha256", "fingerprint", "report_sha256"):
                if execution.get(key):
                    message += f"；{key}={execution[key]}"
            if result["status"] == "FAIL":
                error_code, error_message = error_code or "QUALITY_GEOMETRY_FAIL", message
        def change(state):
            task, run = find(state, "tasks", task_id), find(state, "runs", run_id)
            run.update(status="success" if success else "failed", progress=100 if success or execution is not None and result["status"] == "FAIL" else run["progress"],
                       finished_at=now(), duration_ms=int((time.monotonic() - started) * 1000),
                       error_code=error_code, error_message=error_message)
            for key in ("pid", "exit_code", "stdout_log_path", "stderr_log_path", "command", "cwd"):
                if process.get(key) is not None:
                    run[key] = process[key] or None if key == "pid" else process[key]
            run.update(last_heartbeat=now(), termination_signal=process.get("termination_signal"),
                       timed_out=bool(process.get("timed_out")), resource_info=process.get("resource_info") or {})
            if execution is not None:
                run["resource_info"]["execution"] = execution
            run["logs"].append(message)
            task["context"]["production"].setdefault("results", {})[stage + (":" + prompt["id"] if prompt else "")] = {**result, "run_id": run_id}
            status = "success" if success else "not_connected" if result["status"] == "NOT_CONNECTED" else "waiting_review" if result["status"] in {"WAITING_REVIEW", "WAITING_MANUAL", "UNKNOWN"} else "failed"
            task_stage(state, task_id, stage).update(status=status, end_time=now(), duration=(time.monotonic() - started), error=None if success else message)
            if not success:
                task.update(status="WAITING_REVIEW" if status in {"not_connected", "waiting_review"} else "FAILED", updated_at=now())
                state["errors"].append({"id": new_id("error"), "task_id": task_id, "stage": stage, "code": error_code or "STAGE_FAILED",
                    "message": message, "created_at": now(), "provider": run["provider"], "details": {"run_id": run_id, "prompt_id": prompt["id"] if prompt else None,
                        **({"execution": execution} if execution is not None else {})},
                    "retryable": bool(result.get("retryable"))})
            audit(state, "production.stage_finished", run_id, f"{stage}: {result['status']}", task_id)
        self.repository.mutate(change)

    def _register_prompt(self, task, record, run_id):
        fingerprint = hashlib.sha256(json.dumps({key: record.get(key) for key in ("cache_identity", "id", "prompt", "negative_prompt")}, sort_keys=True).encode()).hexdigest()
        def change(state):
            if any(row["task_id"] == task["id"] and row.get("fingerprint") == fingerprint for row in state["prompts"]):
                return
            state["prompts"].append({"id": new_id("prompt"), "task_id": task["id"], "prompt": record["prompt"],
                "negative_prompt": record["negative_prompt"], "provider": "chatgpt_rpa", "scene_type": record.get("scene", record.get("scene_type", "")),
                "style": "", "lighting": "", "color": "", "aspect_ratio": "9:16", "created_at": now(), "ordinal": int(record["id"][2:]),
                "status": record.get("status", "READY"), "processing_run_id": run_id, "fingerprint": fingerprint})
        self.repository.mutate(change)

    async def _quality(self, batch_id, task_id, person, composition=None):
        from app.integrations.quality_adapter import CutoutQualityAdapter
        adapter = CutoutQualityAdapter(self.settings, thresholds=self.policy()["quality_thresholds"])
        result = await self._stage(batch_id, task_id, "quality", "OpenCVCutoutQualityAnalyzer",
            lambda run_id, directory, callback: adapter.analyze(person, directory / run_id, callback, self.cancels[batch_id]))
        if result:
            assessment = None
            def change(state):
                nonlocal assessment
                latest_run = next(run for run in reversed(state["runs"]) if run["task_id"] == task_id and run["stage"] == "quality")
                assessment = {"id": new_id("quality"), "task_id": task_id, "asset_id": person["id"],
                    "processing_run_id": latest_run["id"], "status": result["status"], "quality_score": result.get("quality_score"),
                    "metrics": {**result.get("metrics", {}), **{key: result.get(key) for key in
                        ("geometry_status", "semantic_status", "report_path", "cache_hit", "input_sha256")}}, "issue_segments": result.get("issue_segments", []),
                    "fingerprint": result.get("fingerprint", ""), "created_at": now()}
                state["quality_assessments"].append(assessment)
                reviewed_asset = composition or person
                needs_review = composition is not None or result["status"] != "PASS" or self.policy()["person_review_required"]
                if needs_review and not any(review["asset_id"] == reviewed_asset["id"] for review in state["reviews"]):
                    state["reviews"].append({"id": new_id("review"), "task_id": task_id, "asset_id": reviewed_asset["id"],
                        "status": "pending", "issues": [item["issue"] for item in result.get("issue_segments", [])],
                        "note": "人物 Alpha 结构质检 " + result["status"] + "；成片仍需确认产品与背景"})
                if needs_review:
                    task_stage(state, task_id, "review").update(status="waiting_review")
            self.repository.mutate(change)
            return assessment

    def _fingerprint(self, task, kind, prompt_id=None, person=None, background=None, *, implementation=None):
        config = self.settings.legacy_video_auto_root / "config.yaml"
        configuration = file_hash(config) if config.is_file() else "MISSING"
        workflow = None
        if kind in {"person", "compose"}:
            path = self.settings.factory_comfy_cutout_workflow if kind == "person" else self.settings.factory_comfy_compose_workflow
            workflow = file_hash(path) if path.is_file() else "MISSING"
        source = task.get("collector_result", {}).get("source_asset_metadata") or {}
        values = {"source": source.get("sha256"), "context": task["context"].get("task_context"), "config": configuration,
                  "workflow": workflow, "kind": kind, "prompt_id": prompt_id,
                  "person": person.get("sha256") if person else None, "background": background.get("sha256") if background else None}
        if kind in {"person", "compose"}:
            stage = "cutout" if kind == "person" else "compose"
            values["implementations"] = implementation if implementation is not None else self.providers[stage].assets.implementation_fingerprint(stage)
        if person:
            values["person_asset_id"] = person.get("id")
            values["alpha_sha256"] = person.get("legacy_reference", {}).get("alpha_sha256")
        if kind in {"person", "compose"}:
            values["generation"] = task["context"].get("production", {}).get("cutout_generation" if kind == "person" else "compose_generation", 1)
        return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()

    def _asset(self, task_id, kind, prompt_id=None, *, person=None, background=None):
        state = self.repository.state()
        task = find(state, "tasks", task_id)
        stage = "cutout" if kind == "person" else "compose"
        implementation = self.providers[stage].assets.implementation_fingerprint(stage) if kind in {"person", "compose"} else None
        fingerprint = self._fingerprint(task, kind, prompt_id, person, background, implementation=implementation)
        if kind == "person":
            selected = task["context"].get("production", {}).get("current_person_asset_id")
            asset = next((row for row in state["assets"] if row["id"] == selected and row["task_id"] == task_id and row["type"] == "person"), None)
            adopted = bool(asset and asset["status"] == "approved" and any(row["asset_id"] == selected and row["status"] == "approved" for row in state["reviews"]))
            repaired = bool(asset and (asset["legacy_reference"].get("repair_id") or asset["legacy_reference"].get("parent_person_asset_id")
                                      or asset["legacy_reference"].get("parent_factory_fingerprint")))
            identity_matches = bool(asset and asset["legacy_reference"].get("factory_fingerprint") == fingerprint)
            selected_identity = self._applied_repair_lineage(state, task, asset, fingerprint, implementation) if repaired else identity_matches or adopted
            if asset and self.business_asset(asset) and asset["registry_status"] == "registered" and asset["status"] not in {"rejected", "failed"} and selected_identity and self._mother_available(asset, implementation):
                self.registry.content(asset["id"])
                if file_hash(asset["storage_path"]) == asset["sha256"]:
                    return asset
            if selected:
                return None
        return next((asset for asset in reversed(state["assets"]) if asset["task_id"] == task_id and asset["type"] == kind
                     and self.business_asset(asset) and asset["registry_status"] == "registered" and asset["status"] not in {"rejected", "failed"}
                     and (prompt_id is None or asset.get("legacy_reference", {}).get("prompt_id") == prompt_id)
                     and asset.get("legacy_reference", {}).get("factory_fingerprint") == fingerprint
                     and (kind != "person" or self._mother_available(asset, implementation))
                     and Path(asset["storage_path"]).is_file() and file_hash(asset["storage_path"]) == asset["sha256"]), None)

    def _applied_repair_lineage(self, state, task, asset, fingerprint, implementation):
        if task["execution_mode"] != "integration":
            return False
        assets = {row["id"]: row for row in state["assets"]}
        chain, visited = [], set()
        current = asset
        while current:
            if (current["id"] in visited or current["task_id"] != task["id"] or current["type"] != "person"
                    or current["legacy_reference"].get("execution_scope", "PRODUCTION") != "PRODUCTION"):
                return False
            visited.add(current["id"])
            chain.append(current)
            parent_id = current["legacy_reference"].get("parent_person_asset_id")
            if not parent_id:
                break
            current = assets.get(parent_id)
            if current is None:
                return False
        def timestamp(value):
            return "".join(character for character in str(value or "") if character.isdigit())[:20]
        def latest_review(person_id):
            rows = [row for row in state["reviews"] if row["task_id"] == task["id"] and row["asset_id"] == person_id]
            def order(pair):
                parts = pair[1].get("id", "").split("-")
                id_timestamp = parts[1] if len(parts) > 1 and parts[1].isdigit() else ""
                created = timestamp(pair[1].get("created_at"))
                return created or id_timestamp, pair[0]
            return max(enumerate(rows), key=order)[1] if rows else None
        try:
            for index, current in enumerate(chain):
                reference = current["legacy_reference"]
                review = latest_review(current["id"])
                if (current["registry_status"] != "registered" or current["status"] in {"rejected", "failed"}
                        or review and review["status"] == "rejected" or not self._mother_available(current, implementation)):
                    return False
                rgb, _ = self.registry.content(current["id"])
                if file_hash(rgb) != current["sha256"]:
                    return False
                manifest = json.loads(self.registry.allowed_path(reference["manifest_path"]).read_text())
                if (manifest.get("status") != "READY" or manifest.get("execution_scope", "PRODUCTION") != "PRODUCTION"
                        or manifest.get("alpha_mode") != "straight" or reference.get("alpha_mode") != "straight"
                        or manifest.get("context_id") != task["context"]["task_context"]["context_id"]):
                    return False
                human_approved = bool(current["status"] == "approved" and review and review["status"] == "approved"
                    and any(row.get("action") == "review.approve" and row.get("entity_id") == review["id"]
                            and row.get("task_id") == task["id"] for row in state["audits"]))
                if human_approved or index and reference.get("factory_fingerprint") == fingerprint:
                    return True
                if index + 1 == len(chain):
                    return False
                parent = chain[index + 1]
                repair = next((row for row in task["context"].get("matte_repairs", []) if row["id"] == reference.get("repair_id")), None)
                if not repair:
                    return False
                restored = repair["status"] == "UNDONE" and repair.get("applied_at") and any(
                    row.get("action") == "person.version.restore" and row.get("entity_id") == current["id"] and row.get("task_id") == task["id"]
                    and timestamp(row.get("created_at")) > timestamp(repair.get("undone_at")) for row in state["audits"])
                run = next((row for row in state["runs"] if row["id"] == repair["run_id"]), None)
                if (repair["status"] != "APPLIED" and not restored or repair.get("execution_scope", "PRODUCTION") != "PRODUCTION"
                        or repair.get("task_id") != task["id"] or repair.get("asset_id") != current["id"] or repair.get("person_asset_id") != parent["id"]
                        or not run or run["task_id"] != task["id"] or run["stage"] != "person" or run["provider"] != "FactoryMatteRepair" or run["status"] != "success"
                        or not isinstance(run.get("pid"), int) or run["pid"] <= 0 or run.get("exit_code") != 0
                        or reference.get("processing_run_id") != run["id"] or manifest.get("processing_run_id") != run["id"]
                        or manifest.get("schema") != "factory.foreground.repair.v1" or manifest.get("repair_id") != repair["id"]
                        or repair.get("parent_rgb_sha256") != parent["sha256"] or current["sha256"] != parent["sha256"]
                        or repair.get("parent_alpha_sha256") != parent["legacy_reference"].get("alpha_sha256")
                        or manifest.get("parent_alpha_sha256") != repair.get("parent_alpha_sha256")
                        or self.registry.allowed_path(manifest["parent_rgb_path"]) != self.registry.allowed_path(parent["storage_path"])
                        or self.registry.allowed_path(manifest["parent_alpha_path"]) != self.registry.allowed_path(parent["legacy_reference"]["alpha_path"])
                        or not any(row.get("action") == "repair.applied" and row.get("entity_id") == repair["id"]
                                   and row.get("task_id") == task["id"] for row in state["audits"])):
                    return False
        except (OSError, ValueError, KeyError, HTTPException):
            return False
        return False

    def _mother_available(self, asset, implementation):
        if not self._alpha_available(asset):
            return False
        reference = asset["legacy_reference"]
        try:
            path = self.registry.allowed_path(reference["manifest_path"])
            if file_hash(path) != reference["manifest_sha256"]:
                return False
            manifest = json.loads(path.read_text())
            source = self.registry.allowed_path(manifest["source_path"])
            return (manifest.get("task_id") == asset["task_id"] and manifest.get("full_decode_verified") is True
                and manifest.get("implementation_hashes") == implementation and manifest.get("rgb_sha256") == asset["sha256"]
                and manifest.get("alpha_sha256") == reference["alpha_sha256"]
                and manifest.get("workflow_sha256") == reference["workflow_sha256"] == file_hash(self.settings.factory_comfy_cutout_workflow)
                and manifest.get("source_sha256") == reference["source_sha256"] == file_hash(source))
        except (OSError, ValueError, KeyError, HTTPException):
            return False

    def _alpha_available(self, asset):
        reference = asset.get("legacy_reference", {})
        if not reference.get("alpha_path") or not reference.get("alpha_sha256"):
            return False
        try:
            path = self.registry.allowed_path(reference["alpha_path"])
            return path.is_file() and file_hash(path) == reference["alpha_sha256"]
        except (OSError, ValueError):
            return False

    def _task_status(self, task_id, status, stage=None):
        def change(state):
            task = find(state, "tasks", task_id)
            if task["status"] == "QUARANTINED" and status != "QUARANTINED":
                return
            task.update(status=status, updated_at=now())
            if stage:
                task["current_stage"] = stage
        self.repository.mutate(change)

    def _task_failure(self, task_id, code, message):
        def change(state):
            task = find(state, "tasks", task_id)
            if task["status"] == "QUARANTINED":
                return
            task.update(status="FAILED", updated_at=now())
            state["errors"].append({"id": new_id("error"), "task_id": task_id, "stage": task["current_stage"],
                "code": code, "message": message, "created_at": now(), "provider": "FactoryProductionQueue", "retryable": False})
        self.repository.mutate(change)

    def _stage_status(self, task_id, stage, status):
        self.repository.mutate(lambda state: task_stage(state, task_id, stage).update(status=status, end_time=now()))

    def _batch_update(self, batch_id, **values):
        def change(state):
            batch = find(state, "batches", batch_id)
            batch.update(**values, updated_at=now())
            if "summary" not in values:
                batch["summary"] = self._summary(state, batch["task_ids"])
        self.repository.mutate(change)

    def _item_update(self, batch_id, task_id, status, stage=None):
        def change(state):
            batch = find(state, "batches", batch_id)
            row = next(row for row in batch["items"] if row["task_id"] == task_id)
            task = find(state, "tasks", task_id)
            row["status"] = "QUARANTINED" if task["status"] == "QUARANTINED" else status
            if stage:
                row["stage"] = stage
            batch["summary"] = self._summary(state, batch["task_ids"])
            batch["updated_at"] = now()
        self.repository.mutate(change)

    def _claim_next(self, batch_id):
        def change(state):
            batch = find(state, "batches", batch_id)
            if self.closing or self.cancels[batch_id].is_set() or batch["status"] not in {"QUEUED", "RUNNING"}:
                return None
            tasks = {task["id"]: task for task in state["tasks"]}
            for row in batch["items"]:
                if row["status"] == "QUEUED" and tasks[row["task_id"]]["status"] == "QUARANTINED":
                    row.update(status="QUARANTINED", stage=tasks[row["task_id"]]["current_stage"])
            item = next((row for row in batch["items"] if row["status"] == "QUEUED"), None)
            if item is None:
                return None
            batch.update(status="RUNNING", active_task_id=item["task_id"], updated_at=now())
            item["status"] = "RUNNING"
            return item["task_id"]
        return self.repository.mutate(change)

    def _completed_shutdown_boundary(self, state, batch, task):
        production = task["context"].get("production", {})
        if (batch["status"] != "PAUSED" or task["status"] != "CANCELLED" or task["execution_mode"] != "integration"
                or production.get("batch_id") != batch["id"] or not production.get("automatic")
                or not any(row["action"] == "production.pause" and row["entity_id"] == batch["id"] for row in state["audits"])
                or any(row["action"] == "production.cancel" and row["entity_id"] == batch["id"] for row in state["audits"])):
            return False
        runs = [row for row in state["runs"] if row["task_id"] == task["id"]]
        if not runs or any(row["status"] in {"queued", "running"} or row.get("error_code") == "UNKNOWN_EXTERNAL_REQUEST" for row in runs):
            return False
        last = runs[-1]
        if (last["stage"] != "compose" or last["provider"] != "LegacyComposeAdapter" or last["status"] != "success"
                or last.get("exit_code") != 0 or not last.get("finished_at") or not isinstance(last.get("pid"), int) or last["pid"] <= 0
                or not any(key.startswith("compose:") and value.get("run_id") == last["id"] and value.get("status") == "READY"
                           for key, value in production.get("results", {}).items())):
            return False
        asset = next((row for row in state["assets"] if row["task_id"] == task["id"] and row["type"] == "compose"
                      and row["legacy_reference"].get("processing_run_id") == last["id"]), None)
        if not asset or not self.business_asset(asset):
            return False
        reference = asset["legacy_reference"]
        background = next((row for row in state["assets"] if row["id"] == reference.get("background_asset_id")), None)
        try:
            person = self._asset(task["id"], "person")
            if (not person or not self._person_review_approved(task["id"], person) or not self._first_review_approved(task["id"], person)
                    or production.get("current_person_asset_id") != person["id"] or reference.get("person_asset_id") != person["id"] or not background
                    or self._asset(task["id"], "background", reference.get("prompt_id")) != background):
                return False
            return self._asset(task["id"], "compose", reference.get("prompt_id"), person=person, background=background) == asset
        except (OSError, ValueError, KeyError, HTTPException):
            return False

    def _cancelled_restore_plan(self, batch_id, state):
        current = next((row for row in reversed(state["batches"]) if row["status"] in ACTIVE_BATCHES),
                       state["batches"][-1] if state["batches"] else None)
        if current is None or current["id"] != batch_id:
            raise HTTPException(409, "BATCH_CHANGED：只能恢复当前已取消批次，请刷新队列")
        if current["status"] != "CANCELLED":
            raise HTTPException(409, "BATCH_NOT_CANCELLED：请使用原暂停/恢复操作")
        job = self.jobs.get(batch_id)
        if job is not None and not job.done() or current.get("active_task_id"):
            raise HTTPException(409, "BATCH_WORKER_ACTIVE：原批次尚未到安全结束边界")
        active = [row for row in state["runs"] if row["provider"] != "mock" and row["status"] in {"running", "queued"}]
        if active or self.collector.active:
            raise HTTPException(409, "COLLECTOR_ACTIVE：先等待活动 Run 与原 Worker 完成，不并行恢复")
        tasks = {row["id"]: row for row in state["tasks"]}
        started = {row["task_id"] for row in state["runs"]}
        outputs = {row["task_id"] for name in ("assets", "prompts", "reviews", "quality_assessments") for row in state[name]}
        rules_used = {row.get("last_task_id") for row in state["collector"] if row.get("last_run_id") or row.get("result")}
        eligible, excluded, evidence = [], [], []
        for item in current["items"]:
            task = tasks[item["task_id"]]
            production = task["context"].get("production", {})
            reason = None
            if item["status"] != "CANCELLED" or task["status"] != "CANCELLED":
                reason = "HISTORICAL_RESULT_PRESERVED"
            elif task["execution_mode"] != "integration" or item.get("stage") != "collector" or task["current_stage"] != "collector" or production.get("requested_stage") not in {None, "collector"}:
                reason = "NOT_UNSTARTED_COLLECTOR"
            elif task["id"] in started or task["id"] in rules_used:
                reason = "PRIOR_RUN_PRESERVED"
            elif task.get("collector_result") or task.get("source_video_id") or task["id"] in outputs or production.get("results"):
                reason = "PRIOR_OUTPUT_PRESERVED"
            elif production.get("batch_id") != batch_id or production.get("rejected_review"):
                reason = "CONTEXT_NOT_ELIGIBLE"
            elif any(row["task_id"] == task["id"] and (row.get("start_time") or row.get("end_time") or row["status"] not in {"pending", "not_connected"}) for row in state["stages"]):
                reason = "PRIOR_STAGE_PRESERVED"
            files = []
            if reason is None:
                paths = StoragePaths(self.settings.storage_root).task_paths(task["project_id"], task["id"])
                directory = Path(paths["metadata"]).parent
                try:
                    files = sorted(str(path.relative_to(directory)) for path in directory.rglob("*") if path.is_file())
                except OSError:
                    reason = "OUTPUT_STATE_UNVERIFIED"
                if files:
                    reason = "PRIOR_FILES_PRESERVED"
            evidence.append({"task_id": task["id"], "files": files, "reason": reason})
            if reason:
                excluded.append({"task_id": task["id"], "status": item["status"], "reason": reason})
            else:
                eligible.append(task["id"])
        fingerprint = hashlib.sha256(json.dumps({"batch": current, "tasks": [tasks[identifier] for identifier in current["task_ids"]],
            "runs": [row for row in state["runs"] if row["task_id"] in current["task_ids"]], "evidence": evidence},
            ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        return {"batch_id": batch_id, "status": "CANCELLED", "fingerprint": fingerprint, "eligible_task_ids": eligible,
            "eligible_count": len(eligible), "excluded": excluded, "preserved_count": len(excluded),
            "status_counts": dict(Counter(item["status"] for item in current["items"])),
            "can_restore": bool(eligible), "message": "仅恢复从未执行、无输出的已取消采集项；已有拒绝、审核、Run、文件和外部请求不重放"}

    def cancelled_restore_plan(self, batch_id):
        return self._cancelled_restore_plan(batch_id, self.repository.state())

    def _environment_restore_plan(self, batch_id, state):
        batch = find(state, "batches", batch_id)
        tasks = {row["id"]: row for row in state["tasks"]}
        started = {row["task_id"] for row in state["runs"]}
        outputs = {row["task_id"] for name in ("assets", "prompts", "reviews", "quality_assessments") for row in state[name]}
        errors = {row["task_id"]: row for row in state["errors"]}
        eligible, excluded, evidence = [], [], []
        for item in batch["items"]:
            task, error = tasks[item["task_id"]], errors.get(item["task_id"], {})
            production = task["context"].get("production", {})
            environment = task["context"].get("collector_environment", {})
            missing = [row for row in environment.get("checks", []) if row.get("required", True)
                and row.get("status", "READY" if row.get("ok") else "MISSING") != "READY"]
            try:
                checked_at = datetime.fromisoformat(environment.get("checked_at", ""))
                failed_at = datetime.fromisoformat(error.get("created_at", ""))
                elapsed = ((failed_at.replace(tzinfo=timezone.utc) if failed_at.tzinfo is None else failed_at)
                    - (checked_at.replace(tzinfo=timezone.utc) if checked_at.tzinfo is None else checked_at)).total_seconds()
                same_preflight = 0 <= elapsed <= 10
            except (TypeError, ValueError):
                same_preflight = False
            reason = None
            if item["status"] != "FAILED" or task["status"] != "FAILED" or item.get("stage") != "collector" or task["current_stage"] != "collector":
                reason = "HISTORICAL_RESULT_PRESERVED"
            elif task["execution_mode"] != "integration" or production.get("previous_status") not in {"CREATED", "QUEUED"}:
                reason = "NOT_UNSTARTED_COLLECTOR"
            elif task["id"] in started or task["id"] in outputs or task.get("collector_result") or task.get("source_video_id") or production.get("results") or production.get("active_run_id"):
                reason = "EXECUTION_OR_ASSET_PRESERVED"
            elif (error.get("code") != "PRODUCTION_ERROR" or error.get("message") != "生产阶段异常：HTTPException"
                    or error.get("stage") != "collector" or error.get("provider") != "FactoryProductionQueue" or not same_preflight):
                reason = "ERROR_NOT_ENVIRONMENT_REGRESSION"
            elif (environment.get("schema") != "collector-environment-v1" or environment.get("status") != "MISSING"
                    or environment.get("business_executed") is not False or environment.get("paid_requests") != 0
                    or len(missing) != 1 or missing[0].get("name") != "existing_cdp_get"
                    or missing[0].get("error_code") != "CHROME_CDP_UNAVAILABLE"):
                reason = "ENVIRONMENT_EVIDENCE_NOT_VERIFIED"
            if reason:
                excluded.append({"task_id": task["id"], "status": item["status"], "reason": reason})
            else:
                eligible.append(task["id"])
            evidence.append({"task_id": task["id"], "task_status": task["status"], "item_status": item["status"],
                "previous_status": production.get("previous_status"), "environment": environment, "error": error,
                "has_run": task["id"] in started, "has_output": task["id"] in outputs, "reason": reason})
        fingerprint = hashlib.sha256(json.dumps({"batch_id": batch_id, "status": batch["status"], "updated_at": batch.get("updated_at"),
            "active_task_id": batch.get("active_task_id"), "evidence": evidence}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        return {"batch_id": batch_id, "fingerprint": fingerprint, "eligible_task_ids": eligible,
            "eligible_count": len(eligible), "excluded": excluded, "preserved_count": len(excluded),
            "can_restore": batch["status"] == "PAUSED" and bool(eligible),
            "message": "仅恢复未创建任何Run、无资产且明确因CDP预检失败的采集项；所有原错误、审核和执行历史保留"}

    def environment_restore_plan(self, batch_id):
        return self._environment_restore_plan(batch_id, self.repository.state())

    async def restore_environment(self, batch_id, *, confirmed, fingerprint):
        if not confirmed:
            raise HTTPException(422, "RESTORE_CONFIRMATION_REQUIRED")
        async with self.start_lock:
            state = await asyncio.to_thread(self.repository.state)
            plan = self._environment_restore_plan(batch_id, state)
            if plan["fingerprint"] != fingerprint or not plan["can_restore"]:
                raise HTTPException(409, "ENVIRONMENT_RESTORE_PLAN_CHANGED：重新检查原批次和失败凭据")
            if any(row["status"] in {"queued", "running"} for row in state["runs"]) or any(
                    item["status"] == "RUNNING" for row in state["batches"] for item in row["items"]):
                raise HTTPException(409, "ALREADY_RUNNING：等待当前真实执行到安全边界")
            checked = await self.readiness.check(refresh=True, stage="collector", task_id=plan["eligible_task_ids"][0])
            names = STAGE_REQUIREMENTS["collector"]
            required = [row for row in checked["checks"] if row.get("name") in names]
            if not names <= {row["name"] for row in required} or any(row["status"] != "READY" for row in required):
                raise HTTPException(409, {"code": "PRODUCTION_NOT_READY", "checks": required, "message": "先准备真实采集环境；原失败历史与暂停状态保留"})
            restoration_id, restored_at = new_id("restore"), now()
            def restore(current):
                verified = self._environment_restore_plan(batch_id, current)
                if verified["fingerprint"] != fingerprint or any(row["status"] in {"queued", "running"} for row in current["runs"]) or any(
                        item["status"] == "RUNNING" for row in current["batches"] for item in row["items"]):
                    raise HTTPException(409, "ENVIRONMENT_RESTORE_PLAN_CHANGED：预检期间状态变化，未恢复")
                batch = find(current, "batches", batch_id)
                tasks = {row["id"]: row for row in current["tasks"]}
                for item in batch["items"]:
                    if item["task_id"] not in verified["eligible_task_ids"]:
                        continue
                    task = tasks[item["task_id"]]
                    record = {"id": restoration_id, "previous_task_status": task["status"], "previous_item_status": item["status"],
                        "error_id": next(row["id"] for row in reversed(current["errors"]) if row["task_id"] == task["id"]),
                        "fingerprint": fingerprint, "restored_at": restored_at}
                    task["context"]["production"].setdefault("environment_restorations", []).append(record)
                    item.setdefault("environment_restorations", []).append(record)
                    task.update(status="QUEUED", updated_at=restored_at)
                    item["status"] = "QUEUED"
                batch.update(status="PAUSED", active_task_id=None, updated_at=restored_at, summary=self._summary(current, batch["task_ids"]))
                audit(current, "production.restore_environment", batch_id,
                    f"正式恢复{len(verified['eligible_task_ids'])}个零Run环境预检失败项；旧错误记录保留，批次仍暂停，凭据{fingerprint}")
            await asyncio.to_thread(self.repository.mutate, restore)
            return {"accepted": True, "batch_id": batch_id, "status": "PAUSED", "restoration_id": restoration_id,
                "restored_count": plan["eligible_count"], "task_ids": plan["eligible_task_ids"], "preserved_count": plan["preserved_count"],
                "message": f"已恢复{plan['eligible_count']}个未执行环境失败项；所有历史保留，原队列仍暂停"}

    async def restore_cancelled(self, batch_id, *, confirmed, fingerprint):
        if not confirmed:
            raise HTTPException(422, "RESTORE_CONFIRMATION_REQUIRED：恢复已取消任务必须明确确认")
        if self.settings.app_mode != "integration":
            raise HTTPException(409, "INTEGRATION_MODE_REQUIRED")
        async with self.start_lock:
            plan = self.cancelled_restore_plan(batch_id)
            if plan["fingerprint"] != fingerprint:
                raise HTTPException(409, "RESTORE_PLAN_CHANGED：取消状态或执行证据已变化，请重新检查")
            if not plan["can_restore"]:
                raise HTTPException(409, "NO_UNSTARTED_CANCELLED_TASKS：没有可安全恢复的未开始采集项")
            checked = await self.readiness.check(refresh=True, stage="collector", task_id=plan["eligible_task_ids"][0])
            names = STAGE_REQUIREMENTS["collector"]
            required = [row for row in checked["checks"] if row.get("name") in names]
            if not names <= {row["name"] for row in required} or any(row["status"] != "READY" for row in required):
                raise HTTPException(409, {"code": "PRODUCTION_NOT_READY", "message": "采集依赖未就绪，原取消状态保留", "checks": required})
            restored_at, restoration_id = now(), new_id("restore")
            def restore(state):
                verified = self._cancelled_restore_plan(batch_id, state)
                if verified["fingerprint"] != fingerprint:
                    raise HTTPException(409, "RESTORE_PLAN_CHANGED：就绪检查期间状态变化，未恢复任何任务")
                batch = find(state, "batches", batch_id)
                record = {"id": restoration_id, "action": "restore_cancelled", "previous_batch_status": "CANCELLED",
                    "cancelled_updated_at": batch["updated_at"], "restored_at": restored_at,
                    "task_ids": verified["eligible_task_ids"], "preserved": verified["excluded"], "fingerprint": fingerprint}
                for item in batch["items"]:
                    if item["task_id"] not in verified["eligible_task_ids"]:
                        continue
                    task = find(state, "tasks", item["task_id"])
                    history = {"restoration_id": restoration_id, "previous_task_status": task["status"],
                        "previous_item_status": item["status"], "cancelled_updated_at": task["updated_at"], "restored_at": restored_at}
                    item.setdefault("cancellation_restorations", []).append(history)
                    task["context"]["production"].setdefault("cancellation_restorations", []).append(history)
                    item["status"] = "QUEUED"
                    task.update(status="QUEUED", updated_at=restored_at)
                batch.update(status="QUEUED", active_task_id=None, updated_at=restored_at)
                audit(state, "production.restore_cancelled", batch_id, json.dumps(record, ensure_ascii=False, sort_keys=True))
            self.repository.mutate(restore)
            return {"accepted": True, "action": "restore_cancelled", "batch_id": batch_id, "status": "queued",
                "restoration_id": restoration_id, "restored_count": plan["eligible_count"], "task_ids": plan["eligible_task_ids"],
                "preserved_count": plan["preserved_count"], "message": f"已恢复原批次 {plan['eligible_count']} 个未开始采集项；保留 {plan['preserved_count']} 项历史结果"}

    async def control(self, action, task_id=None, *, batch_id=None):
        queue = await asyncio.to_thread(self.queue)
        if batch_id is not None and batch_id != queue.get("batch_id"):
            raise HTTPException(409, "BATCH_CHANGED：队列已更新，请刷新后再操作")
        if task_id is not None and task_id not in queue.get("task_ids", []):
            raise HTTPException(409, "TASK_NOT_IN_CURRENT_BATCH：不会操作其它任务的队列")
        batch_id = queue.get("batch_id")
        state = await asyncio.to_thread(self.repository.state)
        def review_approved(identifier):
            task = find(state, "tasks", identifier)
            production = task["context"].get("production", {})
            if production.get("rejected_review") or task["status"] in {"REJECTED", "QUARANTINED"}:
                return False
            return bool(production.get("review_approved") or self.source_review.approval_record(identifier))
        resumable = action == "resume" and queue["status"] in {"COMPLETED_WITH_ERRORS", "COMPLETED"} and await asyncio.to_thread(lambda: any(
            find(state, "tasks", item["task_id"])["status"] != "QUARANTINED" and (
                self._download_resume(item["task_id"]) or self._local_resume(item["task_id"]) or review_approved(item["task_id"])) for item in queue["items"]))
        if not batch_id or queue["status"] not in ACTIVE_BATCHES and not resumable:
            raise HTTPException(409, "NO_ACTIVE_BATCH")
        if action == "pause" and queue["status"] == "PAUSED":
            raise HTTPException(409, "QUEUE_ALREADY_PAUSED")
        if action == "resume" and queue["status"] == "RUNNING":
            raise HTTPException(409, "QUEUE_ALREADY_RUNNING")
        if action == "skip":
            def skip_pending(current):
                batch = find(current, "batches", batch_id)
                item = next((row for row in batch["items"] if row["task_id"] == task_id), None)
                if batch["status"] not in ACTIVE_BATCHES or not item or item["status"] != "QUEUED":
                    raise HTTPException(409, "只能跳过尚未执行的任务；当前任务可取消")
                if find(current, "tasks", task_id)["status"] == "QUARANTINED":
                    raise HTTPException(409, "COLLECTOR_QUARANTINED：已隔离，无需再次跳过")
                item["status"] = "SKIPPED"
                find(current, "tasks", task_id).update(status="SKIPPED", updated_at=now())
            await asyncio.to_thread(self.repository.mutate, skip_pending)
        elif action == "cancel":
            if batch_id in self.cancels:
                self.cancels[batch_id].set()
            if queue.get("active_task_id"):
                for run in (await asyncio.to_thread(self.repository.state))["runs"]:
                    if run["task_id"] == queue["active_task_id"] and run["id"] in self.collector.active:
                        self.collector.active[run["id"]].set()
            def cancel_pending(current):
                batch = find(current, "batches", batch_id)
                batch.update(status="CANCELLED", updated_at=now())
                for item in batch["items"]:
                    if item["status"] == "QUEUED":
                        if find(current, "tasks", item["task_id"])["status"] == "QUARANTINED":
                            item["status"] = "QUARANTINED"
                            continue
                        item["status"] = "CANCELLED"
                        find(current, "tasks", item["task_id"]).update(status="CANCELLED", updated_at=now())
            await asyncio.to_thread(self.repository.mutate, cancel_pending)
        elif action == "pause":
            await asyncio.to_thread(self._batch_update, batch_id, status="PAUSED")
        elif action == "resume":
            environment = await asyncio.to_thread(self._environment_restore_plan, batch_id, state)
            if environment["can_restore"]:
                await self.restore_environment(batch_id, confirmed=True, fingerprint=environment["fingerprint"])
                state, queue = await asyncio.to_thread(self.repository.state), await asyncio.to_thread(self.queue)
            ready_items = []
            for item in queue["items"]:
                identifier = item["task_id"]
                task = find(state, "tasks", identifier)
                if task["status"] == "QUARANTINED" or item["status"] == "QUARANTINED":
                    continue
                approved = await asyncio.to_thread(review_approved, identifier) if item["status"] in {"WAITING_REVIEW", "COLLECTED"} else False
                live_running = queue["status"] == "PAUSED" and item["status"] == "RUNNING" and batch_id in self.jobs
                pending = item["status"] == "QUEUED" or live_running
                recoverable = item["status"] in {"WAITING_REVIEW", "CANCELLED"} and await asyncio.to_thread(
                    lambda: self._download_resume(identifier) or self._local_resume(identifier))
                if item["status"] == "CANCELLED" and await asyncio.to_thread(self._completed_shutdown_boundary, state, queue, task):
                    recoverable = True
                if not (pending or recoverable or approved):
                    continue
                selected = task["context"].get("production", {}).get("requested_stage")
                bounded_saved = selected == "saved_background_compose" and self.manual_compose is not None
                if approved and not bounded_saved:
                    selected = None
                if selected != "compose" and any(run["task_id"] == identifier and run.get("error_code") == "UNKNOWN_EXTERNAL_REQUEST" for run in state["runs"]):
                    raise HTTPException(409, "UNKNOWN_EXTERNAL_REQUEST：外部状态未确认，禁止重复提交")
                if not ready_items:
                    if bounded_saved:
                        await asyncio.to_thread(self.manual_compose.validate_saved_backgrounds, task)
                        await self._saved_context(task)
                        ready_items.append((identifier, selected, pending))
                        continue
                    next_stage = selected or await asyncio.to_thread(self.next_stage, task)
                    checked = await self.readiness.check(refresh=True, stage=next_stage, task_id=identifier)
                    names = STAGE_REQUIREMENTS[next_stage]
                    required = [row for row in checked["checks"] if row.get("name") in names]
                    if not names <= {row["name"] for row in required} or any(row["status"] != "READY" for row in required):
                        raise HTTPException(409, {"code": "PRODUCTION_NOT_READY", "checks": required})
                ready_items.append((identifier, selected, pending))
            if not ready_items:
                raise HTTPException(409, "NO_RESUMABLE_TASK：请先处理当前审核或缺失依赖")
            def resume(current):
                batch = find(current, "batches", batch_id)
                if batch["status"] != queue["status"]:
                    raise HTTPException(409, "BATCH_CHANGED：核验期间队列状态已变化，未恢复")
                items = {row["task_id"]: row for row in batch["items"]}
                previous_items = {row["task_id"]: row for row in queue["items"]}
                tasks = {row["id"]: row for row in current["tasks"]}
                for identifier, selected, pending in ready_items:
                    task = tasks[identifier]
                    if task["status"] != find(state, "tasks", identifier)["status"] or items[identifier]["status"] != previous_items[identifier]["status"]:
                        raise HTTPException(409, "TASK_CHANGED：核验期间任务状态已变化，未恢复")
                    if not pending:
                        items[identifier]["status"] = "QUEUED"
                        task["context"]["production"].update(requested_stage=selected, previous_status=task["status"], retry=False)
                        task.update(status="QUEUED", updated_at=now())
                batch.update(status="RUNNING", updated_at=now(), summary=self._summary(current, batch["task_ids"]))
                audit(current, "production.resume", batch_id, "显式恢复原批次；续接已核实下载或中断的本地 journal，不重新提交付费请求", task_id)
            await asyncio.to_thread(self.repository.mutate, resume)
            if batch_id not in self.jobs:
                await self.launch(batch_id)
        else:
            raise HTTPException(422, "UNKNOWN_QUEUE_ACTION")
        message = {"pause": "当前阶段结束后暂停整个队列", "resume": "继续原队列", "cancel": "已请求取消整个队列；历史和外部任务保留", "skip": "已跳过该任务"}[action]
        if action != "resume":
            await asyncio.to_thread(self.repository.mutate, lambda current: audit(current, "production." + action, batch_id, message, task_id))
        return {"message": message, "batch_id": batch_id, "scope": "TASK" if action == "skip" else "BATCH",
            "affected_task_ids": [task_id] if action == "skip" else queue["task_ids"]}

    def summary(self, task_ids):
        return self._summary(self.repository.state(), task_ids)

    def _summary(self, state, task_ids):
        tasks = [row for row in state["tasks"] if row["id"] in task_ids]
        assets = [row for row in state["assets"] if row["task_id"] in task_ids and row["registry_status"] == "registered" and self.business_asset(row)]
        business_reviews = {asset["id"] for asset in assets}
        decisions = Counter(row["collector_result"].get("decision") for row in tasks)
        quality = [row for row in state["quality_assessments"] if row["task_id"] in task_ids and row.get("asset_id") in business_reviews]
        return {"project_ids": sorted({task["project_id"] for task in tasks}), "task_count": len(tasks),
            "approved_source_count": sum(any(record.get("status") == "approved" and record.get("confirmed_full_video_watermark_free") is True
                and any(review["id"] == record["review_id"] and review["status"] == "approved" and review["issues"] == ["WATERMARK_UNVERIFIED"] for review in state["reviews"])
                for record in task["context"].get("source_watermark_reviews", [])) for task in tasks),
            "accepted_count": decisions["accepted"], "rejected_count": decisions["rejected"], "manual_review_count": sum(task["status"] == "WAITING_REVIEW" for task in tasks),
            "quarantined_count": sum(task["status"] == "QUARANTINED" for task in tasks),
            "downloaded_count": sum(asset["type"] == "source" for asset in assets), "person_asset_count": sum(asset["type"] == "person" for asset in assets),
            "background_count": sum(asset["type"] == "background" for asset in assets), "compose_count": sum(asset["type"] == "compose" for asset in assets),
            "quality_pass": sum(row["status"] == "PASS" for row in quality), "quality_fail": sum(row["status"] == "FAIL" for row in quality),
            "review_count": sum(row["task_id"] in task_ids and row["asset_id"] in business_reviews for row in state["reviews"]),
            "failure_reasons": dict(Counter(row["code"] for row in state["errors"] if row["task_id"] in task_ids))}

    async def recover(self, *, active_run_ids=None):
        snapshot = self.repository.state()
        self.recovered_batches = []
        self.recovered_run_ids = (set(active_run_ids) if active_run_ids is not None else
                                  {row["id"] for row in snapshot["runs"] if row["status"] in {"queued", "running"}})
        self.recovered_item_ids = {item["task_id"] for batch in snapshot["batches"]
                                  if batch["status"] in {"QUEUED", "RUNNING"} for item in batch["items"] if item["status"] == "RUNNING"}
        for batch in snapshot["batches"]:
            if batch["status"] in ACTIVE_BATCHES:
                if batch["status"] != "PAUSED":
                    self.recovered_batches.append(batch["id"])
                for item in batch["items"]:
                    task = find(snapshot, "tasks", item["task_id"])
                    if task["status"] == "QUARANTINED" or item["status"] == "QUARANTINED":
                        if item["status"] != "QUARANTINED":
                            self._item_update(batch["id"], item["task_id"], "QUARANTINED", task["current_stage"])
                        continue
                    if item["status"] == "RUNNING":
                        production = task["context"].get("production", {})
                        if (item.get("stage") == "collector" and task["status"] in {"CREATED", "QUEUED", "RUNNING"}
                                and not task.get("collector_result") and not production.get("active_run_id")
                                and not any(run["task_id"] == task["id"] for run in snapshot["runs"])):
                            def release_unstarted(state):
                                current = find(state, "tasks", task["id"])
                                if (current["status"] not in {"CREATED", "QUEUED", "RUNNING"} or current.get("collector_result")
                                        or current["context"].get("production", {}).get("active_run_id") or any(
                                        run["task_id"] == current["id"] for run in state["runs"])):
                                    return
                                target = next(row for row in find(state, "batches", batch["id"])["items"] if row["task_id"] == current["id"])
                                target["status"] = "QUEUED"
                                current.update(status="QUEUED", updated_at=now())
                                audit(state, "production.unstarted_claim_recovered", batch["id"],
                                    "仅释放未创建任何 Run 的采集队列占位；批次保持暂停，不改变历史执行结果", current["id"])
                            self.repository.mutate(release_unstarted)
                            continue
                        self._item_update(batch["id"], item["task_id"], "WAITING_REVIEW")
                        self._task_status(item["task_id"], "WAITING_REVIEW")
                self._batch_update(batch["id"], status="PAUSED", active_task_id=None)
        def change(state):
            for run in state["runs"]:
                if run["provider"] not in {"mock", "LegacyCollectorAdapter"} and run["status"] in {"queued", "running"}:
                    paid = run["provider"] in {"LegacyPromptAdapter", "BackgroundRPAAdapter"}
                    run.update(status="failed", error_code="UNKNOWN_EXTERNAL_REQUEST" if paid else "LOCAL_EXECUTION_INTERRUPTED",
                               error_message="外部请求需确认，禁止重复计费" if paid else "本地步骤中断；保留 Comfy journal 与所有资产，通过正式恢复继续", finished_at=now())
        if any(run["provider"] not in {"mock", "LegacyCollectorAdapter"} and run["status"] in {"queued", "running"}
               for run in snapshot["runs"]):
            self.repository.mutate(change)

    async def resume_recovered(self):
        """Resume only requests that a user already started, never create new work at boot."""
        if self.settings.app_mode != "integration":
            return
        snapshot = await asyncio.to_thread(self.repository.state)
        policy = {**DEFAULT_POLICY, **snapshot.get("settings", {}).get("production_policy", {})}
        if not policy["auto_resume"]:
            return
        tasks = {task["id"]: task for task in snapshot["tasks"]}
        batches = {batch["id"]: batch for batch in snapshot["batches"]}
        runs_by_task = {}
        for run in snapshot["runs"]:
            runs_by_task.setdefault(run["task_id"], []).append(run)
        for batch_id in self.recovered_batches:
            batch = batches[batch_id]
            eligible = []
            for item in batch["items"]:
                task_id = item["task_id"]
                task = tasks[task_id]
                if task["status"] == "QUARANTINED" or item["status"] == "QUARANTINED":
                    continue
                runs = runs_by_task.get(task_id, [])
                if any(row.get("error_code") == "UNKNOWN_EXTERNAL_REQUEST" for row in runs):
                    self._wait(task_id, "UNKNOWN_EXTERNAL_REQUEST", task["current_stage"],
                               "重启前外部请求状态未知；请核实已保存请求和回执，不会自动再次付费")
                    continue
                latest = runs[-1] if runs else {}
                pid = latest.get("pid") if latest.get("id") in self.recovered_run_ids else None
                if pid:
                    try:
                        probe_process(pid)
                    except ProcessLookupError:
                        pass
                    except PermissionError:
                        self._wait(task_id, "WORKER_STATUS_UNKNOWN", latest.get("stage", "collector"), f"无法确认原进程 PID {pid}；不会并行重交")
                        continue
                    else:
                        self._wait(task_id, "WORKER_STILL_RUNNING", latest.get("stage", "collector"), f"原进程 PID {pid} 仍存在；先核实执行结果，不重复启动")
                        continue
                collector_safe = False
                code = latest.get("error_code")
                saved_mode = task["context"].get("production", {}).get("requested_stage") == "saved_background_compose"
                if saved_mode and self.manual_compose:
                    try:
                        await asyncio.to_thread(self.manual_compose.validate_saved_backgrounds, task)
                    except (HTTPException, OSError, ValueError, KeyError):
                        self._wait(task_id, "SAVED_BACKGROUND_RECOVERY_BLOCKED", "compose", "当前人物、首条审核或已有背景证据变化；保存所有历史，不转交旧合成器")
                        continue
                    if item["status"] == "QUEUED" or task_id in self.recovered_item_ids:
                        eligible.append(task_id)
                    continue
                if (task_id in self.recovered_item_ids and latest.get("provider") == "LegacyCollectorAdapter"
                        and code in {"PROCESS_INTERRUPTED", "DOWNLOAD_PENDING"}):
                    try:
                        phase = self.collector.adapter.checkpoint(self.collector.context(task_id))["phase"]
                        collector_safe = (phase in {"NOT_SUBMITTED", "SEMANTIC_RETRY_READY"} if code == "PROCESS_INTERRUPTED"
                                          else phase in {"DOWNLOAD_PENDING", "RESULT_READY", "SOURCE_VERIFICATION_READY"}
                                          and self._download_resume(task_id))
                    except (OSError, ValueError, KeyError, HTTPException):
                        phase = "UNAVAILABLE"
                    if not collector_safe:
                        self._wait(task_id, "COLLECTOR_RECOVERY_BLOCKED", "collector",
                                   f"采集恢复凭据 {phase} 不允许自动重交；保留已有原片、VidBee 任务和外部请求记录")
                if item["status"] == "QUEUED" or task_id in self.recovered_item_ids and (collector_safe or code in {"LOCAL_EXECUTION_INTERRUPTED", "COLLECTOR_INTERRUPTED", "SEARCH_INTERRUPTED"}):
                    eligible.append(task_id)
            if eligible:
                first = tasks[eligible[0]]
                selected = first["context"].get("production", {}).get("requested_stage")
                if selected != "saved_background_compose":
                    next_stage = selected or self.next_stage(first)
                    checked = await self.readiness.check(refresh=True, stage=next_stage, task_id=first["id"])
                    names = STAGE_REQUIREMENTS[next_stage]
                    required = [row for row in checked["checks"] if row.get("name") in names]
                    if not names <= {row["name"] for row in required} or any(row["status"] != "READY" for row in required):
                        await asyncio.to_thread(self._pause_dependency, batch_id, first["id"], next_stage,
                            {"code": "PRODUCTION_NOT_READY", "checks": required,
                             "message": "恢复前检查发现当前阶段依赖未就绪；原批次暂停，未执行项不消耗"})
                        continue
                def requeue(state):
                    batch = find(state, "batches", batch_id)
                    current_policy = {**DEFAULT_POLICY, **state.get("settings", {}).get("production_policy", {})}
                    if batch["status"] == "CANCELLED" or batch_id not in self.recovered_batches or not current_policy["auto_resume"]:
                        return False
                    current_tasks = {task["id"]: task for task in state["tasks"]}
                    unknown = {run["task_id"] for run in state["runs"] if run.get("error_code") == "UNKNOWN_EXTERNAL_REQUEST"}
                    changed = False
                    for item in batch["items"]:
                        task = current_tasks[item["task_id"]]
                        if (item["task_id"] not in eligible or item["task_id"] in unknown
                                or item["status"] in {"CANCELLED", "REJECTED", "SKIPPED", "PAUSED", "QUARANTINED"}
                                or task["status"] in {"CANCELLED", "REJECTED", "SKIPPED", "PAUSED", "QUARANTINED"}):
                            continue
                        item["status"] = "QUEUED"
                        task.update(status="QUEUED", updated_at=now())
                        changed = True
                    if changed:
                        batch.update(status="QUEUED", updated_at=now(), summary=self._summary(state, batch["task_ids"]))
                        audit(state, "production.restart_resume", batch_id,
                            "仅恢复重启前已授权批次；沿用源、资产与 journal，不重复外部未知请求")
                    return changed
                if await asyncio.to_thread(self.repository.mutate, requeue):
                    await self.launch(batch_id)
        snapshot = await asyncio.to_thread(self.repository.state)
        if not {**DEFAULT_POLICY, **snapshot.get("settings", {}).get("production_policy", {})}["auto_advance"]:
            return
        batches = {batch["id"]: batch for batch in snapshot["batches"]}
        runs_by_task = {}
        for run in snapshot["runs"]:
            runs_by_task.setdefault(run["task_id"], []).append(run)
        for task in snapshot["tasks"]:
            production = task["context"].get("production", {})
            first = production.get("first_composition", {})
            pending = production.get("deferred_continue", {}).get("status") == "PENDING"
            saved = task["context"].get("manual_compose", {}).get("saved_backgrounds") or {}
            saved_authorized = (self.manual_compose is not None and saved.get("status") == "AUTHORIZED"
                and saved.get("approved_review_id") == saved.get("first_review_id"))
            approved_first = (production.get("waiting", {}).get("reason") == "FIRST_COMPOSITION_REVIEW_REQUIRED"
                              and first.get("human_review") == "APPROVED")
            if (task["execution_mode"] != "integration" or not (pending or approved_first or saved_authorized)
                    or task["status"] in {"CANCELLED", "REJECTED", "SKIPPED", "PAUSED", "QUARANTINED"}
                    or task["status"] == "COMPLETED" and not saved_authorized):
                continue
            origin = batches.get(production.get("batch_id"))
            if (not origin or task["id"] not in origin["task_ids"] or origin["status"] == "CANCELLED"
                    or origin["status"] == "PAUSED" and origin["id"] not in self.recovered_batches):
                continue
            runs = runs_by_task.get(task["id"], [])
            if any(row["status"] in {"queued", "running"} or row.get("error_code") == "UNKNOWN_EXTERNAL_REQUEST" for row in runs):
                continue
            if not pending and not saved_authorized:
                if not await asyncio.to_thread(self._first_review_approved, task["id"]):
                    continue
                saved = {"id": new_id("continue"), "status": "PENDING", "stage": None,
                    "person_asset_id": first["person_asset_id"], "rgb_sha256": first["rgb_sha256"], "alpha_sha256": first["alpha_sha256"],
                    "first_human_review": first["human_review"], "first_fingerprint": first.get("fingerprint"),
                    "first_composition_asset_id": first["composition_asset_id"], "repair_run_ids": [], "created_at": now()}
                def recovered_approval(state):
                    find(state, "tasks", task["id"])["context"]["production"]["deferred_continue"] = saved
                    audit(state, "production.approval_continue_recovered", saved["id"], "重启核实首条真实人工批准及原始媒体 hash，恢复此前未交接的生产续跑", task["id"])
                self.repository.mutate(recovered_approval)
            blocked = False
            for run in runs:
                if run["id"] not in self.recovered_run_ids or not run.get("pid"):
                    continue
                try:
                    probe_process(run["pid"])
                except ProcessLookupError:
                    continue
                except PermissionError:
                    reason = "WORKER_STATUS_UNKNOWN"
                else:
                    reason = "WORKER_STILL_RUNNING"
                self._wait(task["id"], reason, run["stage"], f"原进程 PID {run['pid']} 未确认结束；保留审核批准与待续跑，不并行启动")
                blocked = True
                break
            if not blocked:
                recovered_batch_id = origin["id"] if origin["id"] in self.recovered_batches else None
                if saved_authorized:
                    try:
                        await asyncio.to_thread(self.manual_compose.validate_saved_backgrounds, task)
                    except (HTTPException, OSError, ValueError, KeyError):
                        self._wait(task["id"], "SAVED_BACKGROUND_RECOVERY_BLOCKED", "compose", "续跑授权已保存，但人物、首条审批或质检证据变化；保留资产，不重新提交")
                        continue
                    response = await self.continue_task(task["id"], stage="saved_background_compose", recovered_batch_id=recovered_batch_id)
                    if response.get("accepted"):
                        await self.launch(response["batch_id"])
                else:
                    await self.consume_deferred_continue(task["id"], recovered_batch_id=recovered_batch_id)

    async def shutdown(self):
        self.closing = True
        for cancel in self.cancels.values():
            cancel.set()
        if self.jobs:
            try:
                await asyncio.wait_for(asyncio.gather(*list(self.jobs.values()), return_exceptions=True), timeout=8)
            except TimeoutError:
                pass
