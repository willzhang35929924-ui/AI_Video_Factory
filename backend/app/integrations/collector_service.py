import asyncio
import copy
import hashlib
import json
import mimetypes
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import HTTPException

from app.domain import audit, find, new_id, now, task_stage
from app.integrations.contracts import CollectorInput, CollectorResult, TaskContext
from app.integrations.legacy.collector_adapter import LegacyCollectorAdapter
from app.integrations.legacy.vidbee_adapter import VidbeeDownloaderAdapter
from app.integrations.asset_registry import file_hash
from app.integrations.production_rules import read_production_rules, read_factory_rules, verified_factory_rules
from app.integrations.source_review import SourceWatermarkReview
from app.integrations.collector_triage import load_policy, record_machine_decision, SAFE_RETRY_ERRORS
from app.integrations.collection_identity import claim_identity, identity_for, lookup_identity
from app.schemas import ProductionEvent

REAL_STAGES = ("collector", "analysis", "download", "cutout", "person", "background", "compose", "review", "final", "publish")
REVIEW_ERRORS = {"UNKNOWN_EXTERNAL_REQUEST", "LOGIN_REQUIRED", "CAPTCHA_REQUIRED", "DOWNLOAD_PENDING", "RECOVERY_CONFLICT", "EXTERNAL_AUTHORIZATION_REQUIRED", "EXTERNAL_BUDGET_EXHAUSTED"}
FROZEN_ERRORS = {"UNKNOWN_EXTERNAL_REQUEST", "RECOVERY_CONFLICT", "LEGACY_OUTPUT_PROTECTED"}


def record_collector_environment(repository, task_id, result):
    checks = copy.deepcopy(result.get("checks", []))
    missing = [row for row in checks if row.get("required", True) and row.get("status", "READY" if row.get("ok") else "MISSING") != "READY"]
    status = "READY" if result.get("ok", result.get("ready", False)) else "WAITING_MANUAL" if any(row.get("status") == "WAITING_MANUAL" for row in missing) else result.get("status", "MISSING")
    evidence = {"schema": "collector-environment-v1", "task_id": task_id, "checked_at": result.get("checked_at") or now(),
                "status": status, "checks": checks, "reasons": [{"name": row["name"], "status": row.get("status"),
                    "error_code": row.get("error_code"), "message": row.get("message") or row.get("error") or row["name"]} for row in missing],
                "frontend_node_required": False, "business_executed": False, "paid_requests": 0}
    def change(state):
        find(state, "tasks", task_id)["context"]["collector_environment"] = evidence
        audit(state, "collector.environment_checked", task_id, status + ("：" + "；".join(row["message"] for row in evidence["reasons"]) if missing else "；未执行搜索或付费分析"), task_id)
    repository.mutate(change)
    return evidence


class CollectorService:
    def __init__(self, repository, hub, settings, adapter=None):
        self.repository, self.hub, self.settings = repository, hub, settings
        self.adapter = adapter or LegacyCollectorAdapter(settings.legacy_tiktok_auto_root, settings.storage_root,
                                                        python_path=settings.legacy_tiktok_auto_python,
                                                        timeout_s=settings.legacy_collector_timeout_seconds)
        self.active: dict[str, threading.Event] = {}
        self.jobs: dict[str, asyncio.Task] = {}
        self.source_reconcile_lock = asyncio.Lock()
        self.source_reviews = SourceWatermarkReview(repository, settings, self)

    def rules(self) -> dict:
        return read_production_rules(self.settings)

    def context(self, task_id: str) -> dict:
        task = find(self.repository.state(), "tasks", task_id)
        if task["execution_mode"] != "integration":
            raise HTTPException(409, "NOT_REAL_TASK：请在真实采集表单创建独立 Task")
        return TaskContext.model_validate(task["context"]["task_context"]).model_dump(exclude_none=True)

    @staticmethod
    def _duplicate_seen(state, task, identity, *, video_url=None):
        source_key = identity.get("source_identity_key") or identity["id"]
        reference = source_key + ":" + task["id"]
        encounter = {"source_identity_key": source_key,
            "canonical_task_id": identity["canonical_task_id"], "video_url": video_url,
            "seen_at": now(), "paid_requests": 0}
        history = task["context"].setdefault("collection_duplicate_encounters", [])
        if not any(row["source_identity_key"] == source_key for row in history):
            history.append(encounter)
        if not any(row["action"] == "collector.duplicate_seen" and row["entity_id"] == reference for row in state["audits"]):
            audit(state, "collector.duplicate_seen", reference,
                "命中全历史 " + source_key + " → " + identity["canonical_task_id"] + "；未新建有效采集、Review 或外部请求", task["id"])
        return encounter

    @classmethod
    def _skip_duplicate(cls, state, task, identity, *, video_url=None):
        encounter = cls._duplicate_seen(state, task, identity, video_url=video_url)
        task["context"]["collection_identity"] = {"status": "DUPLICATE_SKIPPED",
            "source_identity_key": identity["source_identity_key"], "canonical_task_id": identity["canonical_task_id"]}
        task["context"]["collector_duplicate"] = encounter
        if (not task.get("collector_result") and not task.get("source_video_id")
                and task["status"] in {"CREATED", "QUEUED", "COLLECTING", "RUNNING", "FAILED"}):
            task.update(status="SKIPPED", progress=100, updated_at=now())
            task_stage(state, task["id"], "collector").update(status="skipped", end_time=now(), error="DUPLICATE_SOURCE")
            for row in state["collector"]:
                if row.get("last_task_id") == task["id"]:
                    row["status"] = "SKIPPED"
        return {"accepted": False, "task_id": task["id"], "run_id": None, "status": "duplicate",
            "canonical_task_id": identity["canonical_task_id"], "source_identity_key": identity["source_identity_key"],
            "message": "该视频已存在全历史身份；未创建新的有效采集、Review 或付费分析"}

    def _claim_source(self, state, task, video_id=None, video_url=None):
        context = task["context"].get("task_context", {})
        identifier, url = video_id or context.get("video_id"), video_url or context.get("video_url")
        try:
            resolved = identity_for(identifier, url)
            bound = identity_for(context.get("video_id"), context.get("video_url"))
            if resolved and bound and resolved["id"] != bound["id"]:
                raise ValueError("COLLECTION_IDENTITY_CONFLICT")
            identity = claim_identity(state, task["id"], identifier, url) if resolved else None
        except ValueError as exc:
            raise HTTPException(409, {"code": "COLLECTION_IDENTITY_CONFLICT",
                "message": "视频 ID、URL 或历史身份不一致；未发送外部请求", "reason": str(exc)}) from None
        if resolved is None:
            if isinstance(self.adapter, LegacyCollectorAdapter):
                raise HTTPException(409, "POSSIBLE_DUPLICATE：尚无稳定视频身份；先完成搜索和链接解析，不发送分析请求")
            return None
        return self._skip_duplicate(state, task, identity, video_url=url) if identity["duplicate"] else None

    def dry_run(self, task_id: str | None = None) -> dict:
        context = self.context(task_id) if task_id else {"task_id": None}
        result = self.adapter.dry_run(context)
        result.update(integration_dry_run=self.settings.integration_dry_run)
        result.setdefault("status", "READY" if result["ok"] else "MISSING")
        return result

    async def create(self, payload: dict) -> dict:
        data = CollectorInput.model_validate(payload)
        known_source = identity_for(data.video_id, data.video_url) if data.video_id or data.video_url else None
        if (data.video_id or data.video_url) and known_source is None:
            raise HTTPException(409, "POSSIBLE_DUPLICATE：短链接尚未解析为稳定视频 ID；未创建任务或发送付费请求")
        snapshot = self.rules()
        factory_snapshot = data.factory_rules.snapshot() if data.factory_rules else read_factory_rules(self.settings, snapshot)["snapshot"]
        factory_values = verified_factory_rules(factory_snapshot)
        task_id, product_id, rule_id = new_id("tsk"), data.product_id or new_id("prd"), new_id("col")
        images = [str(Path(value).expanduser().resolve()) for value in data.product_images]
        identity = {"country": data.country, "keyword": data.keyword, "product_images": images,
                    "source_document": data.source_document, "source_row": data.source_row, "product_id": product_id}
        context_id = data.context_id or hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:24]

        def change(state):
            project_id = data.project_id or state["projects"][0]["id"]
            find(state, "projects", project_id)
            if known_source:
                previous = lookup_identity(state, known_source["native_video_id"], data.video_url)
                if previous:
                    owner = find(state, "tasks", previous["canonical_task_id"])
                    reference = previous["id"] + ":" + task_id
                    audit(state, "collector.duplicate_seen", reference, json.dumps({
                        "source_identity_key": previous["id"], "canonical_task_id": owner["id"],
                        "country": data.country, "keyword": data.keyword, "project_id": project_id,
                        "product_id": data.product_id, "product_images": images, "source_document": data.source_document,
                        "source_row": data.source_row, "video_url": data.video_url, "paid_requests": 0,
                        "new_task_created": False, "new_review_created": False}, ensure_ascii=False), owner["id"])
                    existing_rule = next((row["id"] for row in state["collector"] if row.get("last_task_id") == owner["id"]), None)
                    return {"message": "命中既有视频历史身份；未创建 Task、产品、Review 或付费请求", "task_id": owner["id"],
                        "canonical_task_id": owner["id"], "rule_id": existing_rule, "status": "duplicate", "created": False,
                        "source_identity_key": previous["id"]}
            if data.product_id:
                product = find(state, "products", product_id)
                if product["country"] != data.country:
                    raise HTTPException(409, "PRODUCT_COUNTRY_MISMATCH")
            else:
                state["products"].append({"id": product_id, "name": data.product_name or data.keyword,
                    "country": data.country, "image": images[0], "url": "", "binding_reference": "",
                    "description": "单 Task 明确输入的产品上下文", "status": "active", "keywords": [data.keyword]})
            context = TaskContext(task_id=task_id, project_id=project_id, country=data.country, keyword=data.keyword,
                                  product_id=product_id, product_images=images, source_document=data.source_document,
                                  source_row=data.source_row, context_id=context_id, factory_rules=factory_snapshot,
                                  video_id=known_source["native_video_id"] if known_source else None,
                                  video_url=data.video_url or known_source["canonical_url"] if known_source else None).model_dump(exclude_none=True)
            state["tasks"].append({"id": task_id, "project_id": project_id, "country": data.country, "keyword": data.keyword,
                "product_id": product_id, "source_video_id": None, "current_stage": "collector", "status": "CREATED",
                "progress": 0, "thumbnail": "", "created_at": now(), "updated_at": now(), "execution_mode": "integration",
                "context": {"task_context": context, "production_rules": snapshot}, "collector_result": {}})
            for name in REAL_STAGES:
                state["stages"].append({"id": task_id + "-" + name, "task_id": task_id, "name": name,
                    "status": "pending" if name == "collector" else "not_connected", "start_time": None,
                    "end_time": None, "duration": 0, "retry_count": 0, "error": None})
            state["collector"].append({"id": rule_id, "country": data.country, "keyword": data.keyword,
                "product_id": product_id, "product_image": images[0], "product_images": images,
                "min_views": factory_values["min_views"], "min_likes": factory_values["min_likes"], "min_ratio": factory_values["min_like_view_ratio"] * 100,
                "match_threshold": factory_values["product_match_min_confidence"], "filter_overlay": factory_values["overlay_requirement"] == "absent",
                "filter_watermark": factory_values["watermark_requirement"] == "absent", "filter_subtitle": factory_values["subtitle_requirement"] == "absent", "views": 0, "likes": 0, "status": "CREATED",
                "context_id": context_id, "source_document": data.source_document, "source_row": data.source_row or None,
                "last_task_id": task_id, "last_run_id": None, "result": {}})
            state["legacy_bindings"].append({"id": new_id("bind"), "task_id": task_id, "project_id": project_id,
                "product_id": product_id, "source_system": "TikTok_Auto", "context_id": context_id,
                "source_document": data.source_document, "source_row": data.source_row, "created_at": now()})
            if known_source:
                claim_identity(state, task_id, known_source["native_video_id"], data.video_url)
            audit(state, "collector.created", task_id, "创建真实单 Task；尚未启动搜索", task_id)
        duplicate = self.repository.mutate(change)
        if duplicate:
            await self.hub.broadcast(ProductionEvent(type="STAGE_COMPLETED", task_id=duplicate["task_id"], stage="collector",
                status="DUPLICATE_SKIPPED", message=duplicate["message"], data=duplicate))
            return duplicate
        await self.hub.broadcast(ProductionEvent(type="TASK_CREATED", task_id=task_id, stage="collector", status="CREATED", message="真实采集任务已创建，等待手动 Start Collection"))
        return {"message": "独立采集 Task 已创建；未开始搜索", "task_id": task_id, "rule_id": rule_id}

    async def expand_search(self, task_id: str, cancel_event: threading.Event | None = None) -> dict:
        """Discover once, then bind every real hit to its own persisted normal Task."""
        task = find((await asyncio.to_thread(self.repository.state)), "tasks", task_id)
        saved = task["context"].get("collection_search", {})
        if saved.get("status") == "READY":
            return {**saved, "reused": True}
        context = await asyncio.to_thread(self.context, task_id)
        if context.get("video_url") or task.get("collector_result", {}).get("video_id"):
            return {"status": "READY", "task_ids": [task_id], "discovered_count": 1, "run_id": None, "reused": True}
        if not hasattr(self.adapter, "discover"):
            # Isolated contract-test adapters still exercise collect_one; no production fallback exists.
            if isinstance(self.adapter, LegacyCollectorAdapter):
                raise HTTPException(409, "COLLECTOR_DISCOVERY_UNAVAILABLE")
            return {"status": "READY", "task_ids": [task_id], "discovered_count": 1, "run_id": None}
        if any(run["task_id"] == task_id and run.get("error_code") == "UNKNOWN_EXTERNAL_REQUEST" for run in (await asyncio.to_thread(self.repository.state))["runs"]):
            raise HTTPException(409, "UNKNOWN_EXTERNAL_REQUEST：未知付费请求不能通过重新搜索绕过")
        checked = await asyncio.to_thread(self.adapter.dry_run, context) if isinstance(self.adapter, LegacyCollectorAdapter) else {"ok": True, "status": "READY", "checks": []}
        environment = await asyncio.to_thread(record_collector_environment, self.repository, task_id, checked)
        if not checked["ok"]:
            raise HTTPException(409, {"code": "INTEGRATION_NOT_READY", "message": "采集环境检查未通过；没有启动搜索", "checks": checked.get("checks", [])})
        run_id, started = new_id("run"), time.monotonic()
        stop = cancel_event or threading.Event()
        def begin(state):
            if any(run["provider"] in {"LegacyCollectorAdapter", "LegacyCollectorDiscoveryAdapter"} and run["status"] in {"queued", "running"} for run in state["runs"]):
                raise HTTPException(409, "COLLECTOR_ALREADY_RUNNING")
            current = find(state, "tasks", task_id)
            self._guard_collection_start(state, current, stop)
            state["runs"].append({"id": run_id, "run_id": run_id, "task_id": task_id, "stage": "collector",
                "provider": "LegacyCollectorDiscoveryAdapter", "status": "running", "progress": 0, "started_at": now(), "created_at": now(),
                "logs": ["[ENV] " + environment["status"] + " · " + environment["checked_at"], "[REAL] 使用旧搜索分页器发现全部结果；不读取付费分析、不提交下载"],
                "resource_info": {"collector_environment": environment},
                "idempotency_key": task_id + ":collector-discovery:" + context["context_id"]})
            current.update(status="COLLECTING", current_stage="collector", updated_at=now())
            task_stage(state, task_id, "collector").update(status="running", start_time=now(), error=None)
        await asyncio.to_thread(self.repository.mutate, begin)
        self.active[run_id] = stop
        await self.hub.broadcast(ProductionEvent(type="STAGE_STARTED", task_id=task_id, run_id=run_id, stage="collector", status="COLLECTING", message="真实搜索全部结果，逐视频建立正常任务"))
        loop = asyncio.get_running_loop()
        def callback(event):
            asyncio.run_coroutine_threadsafe(self._event(task_id, run_id, event), loop).result(timeout=30)
        process = {}
        try:
            result = await self.adapter.discover(context, on_event=callback, cancel_event=stop, run_id=run_id)
            process = result.pop("_process", {})
            if result.get("status") != "READY":
                code = result.get("error_code") or "SEARCH_ERROR"
                await asyncio.to_thread(self._fail, task_id, run_id, code, result.get("error_message") or "搜索未完成；没有创建候选任务", None, process, started)
                await self.hub.broadcast(ProductionEvent(type="STAGE_FAILED", task_id=task_id, run_id=run_id, stage="collector", status=result.get("status"), message=code))
                return {"status": result.get("status", "FAILED"), "error_code": code, "error_message": result.get("error_message", ""), "task_ids": [], "discovered_count": 0, "run_id": run_id}
            hits, seen = [], set()
            for value in result.get("hits", []):
                identifier, url = str(value.get("video_id", "")), str(value.get("url", ""))
                parsed = urlsplit(url)
                if (not identifier.isdigit() or parsed.scheme not in {"http", "https"} or parsed.hostname not in {"tiktok.com", "www.tiktok.com"}
                        or parsed.username or parsed.password or not parsed.path.rstrip("/").endswith(("/video/" + identifier, "/photo/" + identifier))
                        or identity_for(identifier, url) is None):
                    raise ValueError("SEARCH_RESULT_IDENTITY_INVALID")
                if identifier not in seen:
                    hits.append({"video_id": identifier, "video_url": url})
                    seen.add(identifier)
            if not hits:
                raise ValueError("SEARCH_RESULT_EMPTY")
            created = []
            def bind(state):
                root = find(state, "tasks", task_id)
                original_row = next(row for row in state["collector"] if row.get("last_task_id") == task_id)
                identifiers, duplicate_hits = [], []
                root_bound = False
                for ordinal, hit in enumerate(hits):
                    previous = lookup_identity(state, hit["video_id"], hit["video_url"])
                    if previous and previous["canonical_task_id"] != task_id:
                        duplicate_hits.append({"video_id": hit["video_id"], "video_url": hit["video_url"],
                            "source_identity_key": previous["id"], "canonical_task_id": previous["canonical_task_id"]})
                        self._duplicate_seen(state, root, previous, video_url=hit["video_url"])
                        continue
                    target = root if not root_bound else next((row for row in state["tasks"] if row["execution_mode"] == "integration"
                        and row["project_id"] == root["project_id"] and row["context"].get("task_context", {}).get("context_id") == context["context_id"]
                        and row["context"].get("task_context", {}).get("video_id") == hit["video_id"]
                        and row["context"].get("task_context", {}).get("factory_rules") == context.get("factory_rules")), None)
                    if target is None:
                        child_id = new_id("tsk")
                        target = {**copy.deepcopy(root), "id": child_id, "status": "CREATED", "progress": 0, "source_video_id": None,
                            "thumbnail": "", "created_at": now(), "updated_at": now(), "collector_result": {},
                            "context": {"task_context": {**context, "task_id": child_id, **hit}, "production_rules": copy.deepcopy(root["context"]["production_rules"])}}
                        state["tasks"].append(target)
                        state["collector"].append({**copy.deepcopy(original_row), "id": new_id("col"), "last_task_id": child_id,
                            "last_run_id": None, "status": "CREATED", "views": 0, "likes": 0, "result": {}})
                        state["legacy_bindings"].append({"id": new_id("bind"), "task_id": child_id, "project_id": context["project_id"],
                            "product_id": context["product_id"], "source_system": "TikTok_Auto", "context_id": context["context_id"],
                            "video_id": hit["video_id"], "source_document": context["source_document"], "source_row": context["source_row"], "created_at": now()})
                        for name in REAL_STAGES:
                            state["stages"].append({"id": child_id + "-" + name, "task_id": child_id, "name": name,
                                "status": "pending" if name == "collector" else "not_connected", "start_time": None, "end_time": None,
                                "duration": 0, "retry_count": 0, "error": None})
                        created.append(child_id)
                        audit(state, "collector.video_task_created", child_id, "真实搜索结果 " + hit["video_id"] + "；继承明确国家/产品图/来源行/规则快照", child_id)
                    target["context"]["task_context"].update(hit)
                    claimed = claim_identity(state, target["id"], hit["video_id"], hit["video_url"])
                    if claimed["duplicate"]:
                        raise ValueError("COLLECTION_IDENTITY_CHANGED_DURING_BIND")
                    root_bound = True
                    target["context"].setdefault("collection_source", {"source_id": "tiktok:" + hit["video_id"], "root_task_id": task_id,
                        "discovery_run_id": run_id, "ordinal": ordinal + 1, "context_id": context["context_id"], **hit})
                    identifiers.append(target["id"])
                summary = {"status": "READY", "task_ids": identifiers, "discovered_count": len(hits), "run_id": run_id,
                    "new_source_count": len(identifiers), "duplicate_count": len(duplicate_hits), "duplicate_hits": duplicate_hits,
                    "result_path": result.get("result_path"), "search_config": result.get("search_config", {}), "completed_at": now()}
                root["context"]["collection_search"] = summary
                root.update(status=task["status"], updated_at=now())
                stage = task_stage(state, task_id, "collector")
                stage.update(status="pending", end_time=None, error=None)
                if not identifiers and duplicate_hits:
                    self._skip_duplicate(state, root, duplicate_hits[0], video_url=duplicate_hits[0]["video_url"])
                run = find(state, "runs", run_id)
                run.update(status="success", progress=100)
                run["logs"].append(f"[REAL] 搜索发现 {len(hits)} 个不同 video_id；{len(duplicate_hits)} 个全历史重复；{len(identifiers)} 个有效来源；新增 {len(created)} 个逐视频任务")
                self._run_end(run, process, started)
                audit(state, "collector.search_expanded", task_id, f"真实搜索返回 {len(hits)} 条，保留完整分页结果", task_id)
            await asyncio.to_thread(self.repository.mutate, bind)
            for child_id in created:
                await self.hub.broadcast(ProductionEvent(type="TASK_CREATED", task_id=child_id, stage="collector", status="CREATED", message="真实搜索结果已建立独立视频任务"))
            saved = find((await asyncio.to_thread(self.repository.state)), "tasks", task_id)["context"]["collection_search"]
            await self.hub.broadcast(ProductionEvent(type="STAGE_COMPLETED", task_id=task_id, run_id=run_id, stage="collector", message=f"搜索发现 {len(hits)} 条，等待逐视频指标与分析", data=saved))
            return saved
        except asyncio.CancelledError:
            stop.set()
            await asyncio.to_thread(self._fail, task_id, run_id, "SEARCH_INTERRUPTED", "搜索中断；没有付费提交，可恢复搜索快照", None, process, started)
            raise
        except Exception as exc:
            await asyncio.to_thread(self._fail, task_id, run_id, "SEARCH_ERROR", "搜索结果不能可靠绑定：" + type(exc).__name__, None, process, started)
            return {"status": "FAILED", "error_code": "SEARCH_ERROR", "task_ids": [], "discovered_count": 0, "run_id": run_id}
        finally:
            self.active.pop(run_id, None)

    def can_resume_download(self, task_id: str) -> bool:
        if not isinstance(self.adapter, LegacyCollectorAdapter):
            return False
        result = find(self.repository.state(), "tasks", task_id)["collector_result"]
        if result.get("decision") in {"accepted", "rejected"} and not result.get("error_code"):
            return False
        return self.adapter.checkpoint(self.context(task_id))["phase"] in {"DOWNLOAD_PENDING", "RESULT_READY", "SOURCE_VERIFICATION_READY"}

    def can_retry_semantic(self, task_id: str) -> bool:
        if not isinstance(self.adapter, LegacyCollectorAdapter):
            return False
        state = self.repository.state()
        if any(run["task_id"] == task_id and run.get("error_code") == "UNKNOWN_EXTERNAL_REQUEST" for run in state["runs"]):
            return False
        return self.adapter.checkpoint(self.context(task_id))["phase"] == "SEMANTIC_RETRY_READY"

    def _verified_source_origin(self, task_id, *, allow_active=False):
        state = self.repository.state()
        task = find(state, "tasks", task_id)
        context = self.context(task_id)
        if not allow_active and (any(run["task_id"] == task_id and run["status"] in {"queued", "running"} for run in state["runs"]) or any(
                item["task_id"] == task_id and item["status"] in {"QUEUED", "RUNNING"} for batch in state.get("batches", [])
                if batch["status"] in {"QUEUED", "RUNNING", "PAUSED"} for item in batch["items"])):
            raise HTTPException(409, "TASK_BUSY：不能在执行过程中对账")
        source = next((asset for asset in reversed(state["assets"]) if asset["task_id"] == task_id and asset["type"] == "source"
            and asset["registry_status"] == "registered" and asset.get("legacy_reference", {}).get("execution_scope") != "TECHNICAL_VALIDATION"), None)
        if not source or not task.get("source_video_id"):
            raise HTTPException(409, "SOURCE_EVIDENCE_MISSING：没有已登记原片")
        metadata = source.get("probe_metadata", {})
        path = Path(source.get("external_path") or source["storage_path"]).resolve()
        if (metadata.get("full_decode_verified") is not True or not path.is_file() or not path.is_relative_to(self.settings.legacy_tiktok_auto_root / "downloads")
                or path.stat().st_size != source.get("size") or path.stat().st_mtime_ns != metadata.get("mtime_ns") or file_hash(path) != source.get("sha256")):
            raise HTTPException(409, "SOURCE_EVIDENCE_CHANGED：原片完整核验或当前文件已变化")
        rules = self.rules()
        if not rules.get("sha256") or task["context"].get("production_rules", {}).get("sha256") != rules["sha256"]:
            raise HTTPException(409, "SOURCE_RULES_CHANGED：生产规则快照已变化")
        checkpoint_path = self.settings.storage_root / "projects" / task["project_id"] / "tasks" / task_id / "metadata/collector-state.json"
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        payload = {key: context[key] for key in TaskContext.model_fields if key in context}
        identity = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        evidence, row = checkpoint.get("recovery_evidence", {}), checkpoint.get("row", {})
        if checkpoint.get("identity") != identity or evidence.get("config_sha256") != rules["sha256"] or checkpoint.get("status") != "finished":
            raise HTTPException(409, "SOURCE_CONTEXT_CHANGED：原采集上下文未完整绑定")
        image_hashes = sorted({file_hash(value) for value in context["product_images"]})
        image_fingerprint = hashlib.sha256("\n".join(image_hashes).encode()).hexdigest()[:16]
        expected = {**context, "video_id": source.get("legacy_video_id"), "video_url": source.get("source_url")}
        if (image_fingerprint != evidence.get("reference_image_fingerprint") or image_fingerprint != row.get("reference_image_fingerprint")
                or any(str(evidence.get(key, "")) != str(expected.get(key, "")) for key in ("country", "keyword", "context_id", "source_document", "source_row", "video_id", "video_url"))
                or source.get("legacy_context_id") != context["context_id"]):
            raise HTTPException(409, "SOURCE_CONTEXT_CHANGED：图片或源身份已变化")
        matching_run = None
        for run in reversed(state["runs"]):
            if run["task_id"] != task_id or run["provider"] != "LegacyCollectorAdapter" or run["status"] != "success" or run.get("exit_code") != 0 or not run.get("pid"):
                continue
            result_path = checkpoint_path.parent / run["id"] / "collector-result.json"
            if not result_path.is_file():
                continue
            result = json.loads(result_path.read_text(encoding="utf-8"))
            actual = result.get("source_asset_metadata", {}) or {}
            if (any(result.get(key) != context[key] for key in context) or result.get("error_code")
                    or result.get("video_id") != source.get("legacy_video_id") or result.get("legacy_vidbee_task_id") != evidence.get("vidbee_task_id")
                    or result.get("download_status") != "completed" or actual.get("full_decode_verified") is not True
                    or actual.get("sha256") != source["sha256"] or actual.get("mtime_ns") != metadata["mtime_ns"]
                    or Path(actual.get("path", "")).resolve() != path):
                continue
            matching_run = {"run": run, "result_path": str(result_path), "result_sha256": file_hash(result_path)}
            break
        if not matching_run:
            raise HTTPException(409, "SOURCE_RUN_EVIDENCE_MISSING：缺少成功原Run及真实结果绑定")
        return task, context, source, matching_run

    async def reconcile_verified_source(self, task_id):
        async with self.source_reconcile_lock:
            try:
                task, context, source, origin = await asyncio.to_thread(self._verified_source_origin, task_id)
                directory = self.settings.storage_root / "projects" / task["project_id"] / "tasks" / task_id / "metadata/source-reconcile" / new_id("check")
                row = {"vidbee_task_id": task["collector_result"]["legacy_vidbee_task_id"], "video_id": source["legacy_video_id"],
                    "url": source["source_url"], "context_id": context["context_id"], "download_path": source["external_path"] or source["storage_path"],
                    "download_source_sha256": source["sha256"], "download_source_type": "vidbee_automation"}
                verified = await VidbeeDownloaderAdapter(self.settings).verify_completed_once(context, row, directory)
                process = verified.pop("_process", {})
                verification_run_id = new_id("run")
                self.repository.mutate(lambda state: state["runs"].append({"id": verification_run_id, "task_id": task_id,
                    "stage": "download", "provider": "VidbeeDownloaderAdapter", "status": "success" if verified.get("status") == "READY" else "failed",
                    "progress": 100 if verified.get("status") == "READY" else 0, "created_at": now(),
                    "started_at": datetime.fromtimestamp(process["started_at"], timezone.utc).isoformat() if process.get("started_at") else None,
                    "finished_at": datetime.fromtimestamp(process["finished_at"], timezone.utc).isoformat() if process.get("finished_at") else None,
                    **{key: process.get(key) for key in ("pid", "exit_code", "duration_ms", "cwd", "stdout_log_path", "stderr_log_path")},
                    "command": process.get("command") or [],
                    "logs": ["[REAL] verify_existing_source：只查询原 VidBee 任务并核验同一文件，0 新下载/搜索/付费",
                        "原 Collector Run=" + origin["run"]["id"] + "; source_asset=" + source["id"] + "; evidence=" + str(directory / "reconcile-evidence.json")],
                    "error_code": verified.get("error_code"), "error_message": verified.get("message") if verified.get("status") != "READY" else None}))
                fresh = verified.get("asset", {})
                if (verified.get("status") != "READY" or fresh.get("sha256") != source["sha256"]
                        or Path(fresh.get("path", "")).resolve() != Path(row["download_path"]).resolve()
                        or fresh.get("download_verification", {}).get("association") != "same_file"
                        or fresh.get("download_verification", {}).get("task_id") != row["vidbee_task_id"]):
                    raise HTTPException(409, "SOURCE_NOT_VERIFIED：外部completed同文件状态未确认")
                current_task, _, current_source, current_origin = await asyncio.to_thread(self._verified_source_origin, task_id)
                if current_source != source or current_origin != origin or current_task["collector_result"] != task["collector_result"]:
                    raise HTTPException(409, "SOURCE_EVIDENCE_CHANGED：核验过程中任务发生变化")
                proof = {"origin_result_path": origin["result_path"], "origin_result_sha256": origin["result_sha256"],
                    "verification_process": process, "verification_run_id": verification_run_id, "operation": "verify_existing_source", "current_vidbee_verification": verified,
                    "business_run_created": False, "search_count": 0, "download_submission_count": 0, "paid_requests": 0}
                proof_path = directory / "reconcile-evidence.json"
                proof_path.write_text(json.dumps(proof, ensure_ascii=False, indent=2), encoding="utf-8")
                def change(state):
                    # Recheck inside the same transaction that changes the mutable stage.
                    latest_task, _, latest_source, latest_origin = self._verified_source_origin(task_id)
                    if latest_source != source or latest_origin != origin or latest_task["collector_result"] != task["collector_result"]:
                        raise HTTPException(409, "SOURCE_EVIDENCE_CHANGED：无法应用过期核验")
                    current = find(state, "tasks", task_id)
                    run = find(state, "runs", origin["run"]["id"])
                    self._record_download_checkpoint(state, current, run, source["probe_metadata"], source["id"],
                        reconciliation={"path": str(proof_path), "sha256": file_hash(proof_path), "recorded_at": now(), "verification_run_id": verification_run_id})
                    audit(state, "collector.source_reconciled", source["id"], "正式源对账；沿用成功CollectorRun，0搜索/0新下载/0付费", task_id)
                self.repository.mutate(change)
            except HTTPException:
                raise
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise HTTPException(409, "SOURCE_EVIDENCE_INVALID：" + type(exc).__name__) from None
            checkpoint = find(self.repository.state(), "tasks", task_id)["context"]["download_checkpoint"]
            try:
                review_id = await asyncio.to_thread(self.source_reviews.register_pending, task_id)
            except HTTPException:
                review_id = None
            await self.hub.broadcast(ProductionEvent(type="STAGE_COMPLETED", task_id=task_id, run_id=origin["run"]["id"], stage="download", status="READY",
                message="已依据原成功Run对账下载状态；没有重放采集、下载或付费请求", data=checkpoint))
            return {"status": "READY", "task_id": task_id, "checkpoint": checkpoint, "source_review_id": review_id, "business_run_created": False, "paid_requests": 0}

    @staticmethod
    def _guard_collection_start(state, task, cancel_event=None):
        if task["status"] == "QUARANTINED":
            raise HTTPException(409, "COLLECTOR_QUARANTINED：证据不足或请求未知；禁止自动重复采集/计费")
        batch_id = task["context"].get("production", {}).get("batch_id")
        batch = next((row for row in state.get("batches", []) if row["id"] == batch_id), None)
        if task["status"] in {"CANCELLED", "SKIPPED"} or batch and batch["status"] == "CANCELLED" or cancel_event and cancel_event.is_set():
            raise HTTPException(409, "COLLECTION_STOPPED：任务或原批次已取消/跳过，未创建采集 Run")

    @staticmethod
    def _collection_entry_status(state, task):
        production = task["context"].get("production", {})
        active = next((batch for batch in state.get("batches", []) if batch["id"] == production.get("batch_id")
            and batch["status"] in {"QUEUED", "RUNNING", "PAUSED"} and batch.get("active_task_id") == task["id"]), None)
        return production.get("previous_status", task["status"]) if task["status"] == "QUEUED" and active else task["status"]

    async def queue(self, task_id: str, retry: bool = False, *, resume_download=False, semantic_retry=False, cancel_event=None) -> dict:
        context = await asyncio.to_thread(self.context, task_id)
        duplicate = await asyncio.to_thread(self.repository.mutate, lambda current:
            self._claim_source(current, find(current, "tasks", task_id)))
        if duplicate:
            await self.hub.broadcast(ProductionEvent(type="STAGE_COMPLETED", task_id=task_id, stage="collector",
                status="SKIPPED", message=duplicate["message"], data=duplicate))
            return duplicate
        state = (await asyncio.to_thread(self.repository.state))
        task = find(state, "tasks", task_id)
        self._guard_collection_start(state, task, cancel_event)
        entry_status = self._collection_entry_status(state, task)
        if semantic_retry and resume_download:
            raise HTTPException(409, "RECOVERY_CONFLICT：下载恢复与付费语义重试必须分开执行")
        if any(run["task_id"] == task_id and run.get("error_code") == "UNKNOWN_EXTERNAL_REQUEST" for run in state["runs"]):
            raise HTTPException(409, "UNKNOWN_EXTERNAL_REQUEST：历史外部请求未确认，不自动重发")
        if entry_status in {"COLLECTED", "REJECTED"}:
            raise HTTPException(409, "ALREADY_COMPLETED")
        if entry_status == "COLLECTING":
            raise HTTPException(409, "ALREADY_RUNNING")
        if entry_status == "WAITING_REVIEW" and not resume_download and not semantic_retry:
            raise HTTPException(409, "WAITING_REVIEW：禁止盲目重复外部付费请求")
        if resume_download and not self.can_resume_download(task_id):
            raise HTTPException(409, "RECOVERY_CONFLICT：原下载等待证据缺失或不匹配，未搜索或提交下载")
        if semantic_retry and not self.can_retry_semantic(task_id):
            raise HTTPException(409, "RECOVERY_CONFLICT：无效响应协议重试证据缺失或唯一尝试已使用")
        if not resume_download and not semantic_retry and retry != (entry_status == "FAILED"):
            raise HTTPException(409, "请对 FAILED Task 使用 Retry，其它 Task 使用 Start Collection")
        if self.settings.integration_dry_run:
            raise HTTPException(409, "DRY_RUN_ONLY：先完成 Dry Run，再将 Factory INTEGRATION_DRY_RUN=false")
        checked = {"ok": True} if resume_download or semantic_retry else await asyncio.to_thread(self.adapter.dry_run, context)
        environment = await asyncio.to_thread(record_collector_environment, self.repository, task_id, checked) if not resume_download and not semantic_retry else task["context"].get("collector_environment", {})
        if not checked["ok"]:
            raise HTTPException(409, {"code": "INTEGRATION_NOT_READY", "message": "采集环境检查未通过；没有启动搜索", "checks": checked.get("checks", [])})
        run_id = new_id("run")
        snapshot = await asyncio.to_thread(self.rules)

        def change(state):
            current = find(state, "tasks", task_id)
            duplicate = self._claim_source(state, current)
            if duplicate:
                return duplicate
            self._guard_collection_start(state, current, cancel_event)
            current_entry = self._collection_entry_status(state, current)
            if any(run["task_id"] == task_id and run.get("error_code") == "UNKNOWN_EXTERNAL_REQUEST" for run in state["runs"]):
                raise HTTPException(409, "UNKNOWN_EXTERNAL_REQUEST：历史外部请求未确认，不自动重发")
            if any(batch["status"] in {"QUEUED", "RUNNING", "PAUSED"} and batch["active_task_id"] != task_id for batch in state.get("batches", [])):
                raise HTTPException(409, "PRODUCTION_QUEUE_ACTIVE：请通过当前串行队列执行")
            if current_entry in {"COLLECTED", "REJECTED"}:
                raise HTTPException(409, "ALREADY_COMPLETED")
            if current_entry == "COLLECTING":
                raise HTTPException(409, "ALREADY_RUNNING")
            if current_entry == "WAITING_REVIEW" and not resume_download and not semantic_retry:
                raise HTTPException(409, "WAITING_REVIEW：禁止盲目重复外部付费请求")
            if resume_download and (current["status"] != task["status"] or not self.can_resume_download(task_id)):
                raise HTTPException(409, "RECOVERY_CONFLICT：下载恢复状态已变化")
            if semantic_retry and (current["status"] != task["status"] or not self.can_retry_semantic(task_id)):
                raise HTTPException(409, "RECOVERY_CONFLICT：语义协议重试状态已变化")
            if not resume_download and not semantic_retry and retry != (current_entry == "FAILED"):
                raise HTTPException(409, "任务状态已变化，请刷新后使用对应操作")
            if any(run["provider"] == "LegacyCollectorAdapter" and run["status"] in ("queued", "running") for run in state["runs"]):
                raise HTTPException(409, "SINGLE_TASK_LIMIT：当前阶段一次只运行一个真实 Task")
            stage = task_stage(state, task_id, "collector")
            stage.update(status="pending", error=None, start_time=None, end_time=None, duration=0)
            if retry or resume_download or semantic_retry:
                stage["retry_count"] += 1
            current.update(status="COLLECTING", progress=0, updated_at=now())
            if not resume_download and not semantic_retry:
                current["context"]["production_rules"] = snapshot
            message = "[REAL] 显式单次语义协议重试；原无效响应已收到且可能已计费；不搜索、不下载、不重算参考图匹配" if semantic_retry else "[REAL] 显式恢复原 VidBee 下载；不搜索、不提交新下载" if resume_download else "[REAL] 单 Task 已排队；尚未执行搜索"
            state["runs"].append({"id": run_id, "run_id": run_id, "task_id": task_id, "stage": "collector",
                "provider": "LegacyCollectorAdapter", "status": "queued", "progress": 0,
                "logs": (["[ENV] " + environment.get("status", "UNKNOWN") + " · " + environment.get("checked_at", "")] if environment else []) + [message], "created_at": now(),
                "resource_info": {"collector_environment": environment} if environment else {},
                "retry_count": stage["retry_count"], "idempotency_key": task_id + (":semantic-protocol-retry:" if semantic_retry else ":download-resume:" if resume_download else ":collector:") + str(stage["retry_count"])})
            row = next(row for row in state["collector"] if row.get("last_task_id") == task_id)
            row.update(status="COLLECTING", last_run_id=run_id)
            audit(state, "collector.semantic_protocol_retry" if semantic_retry else "collector.download_resume" if resume_download else "collector.queued", run_id,
                  "显式唯一语义协议重试；保留原无效响应和生产规则" if semantic_retry else "恢复原下载，保留外部 ID，不重复搜索或入队" if resume_download else "单任务真实采集排队", task_id)
        duplicate = await asyncio.to_thread(self.repository.mutate, change)
        if duplicate:
            return duplicate
        self.active[run_id] = threading.Event()
        if cancel_event is not None and cancel_event.is_set():
            self.active[run_id].set()
        return {"accepted": True, "task_id": task_id, "run_id": run_id, "status": "queued", "message": "单 Task 真实采集已排队"}

    async def _event(self, task_id: str, run_id: str, event: dict) -> None:
        event_type = event.get("type", "")
        phase = event.get("stage", "")
        message = event.get("text") or phase or event_type
        progress = {"CONNECT_CHROME": 5, "SEARCH": 15, "METRICS": 30, "METRICS_PRODUCT_DOWNLOAD": 30,
                    "WAITING_DOWNLOAD": 60, "SEMANTIC_ANALYSIS": 80, "DONE": 95}.get(phase)
        fields = {"progress": progress} if progress is not None else {}
        if event_type == "PROCESS_STARTED":
            fields.update(pid=event["pid"], command=event["command"], cwd=event["cwd"], last_heartbeat=now())
            fields.update({key: event[key] for key in ("stdout_log_path", "stderr_log_path") if event.get(key)})
        if event_type == "PROCESS_HEARTBEAT":
            fields["last_heartbeat"] = datetime.fromtimestamp(event["last_heartbeat"], timezone.utc).isoformat()
        if event_type == "process_finished":
            fields.update(termination_signal=event.get("termination_signal"), timed_out=bool(event.get("timed_out")),
                          resource_info=event.get("resource_info") or {})
        metrics = None
        if event_type == "METRICS_UPDATED":
            metrics = {key: event.get(key) for key in ("views", "likes", "metric_source", "metric_evidence", "metrics_provenance")}
            views, likes = event.get("views"), event.get("likes")
            metrics["like_view_ratio"] = (likes / views * 100 if views else 0) if views is not None and likes is not None else None
        await asyncio.to_thread(self.repository.update_processing_run_event, task_id, run_id, fields,
            log_message=None if event_type == "PROCESS_HEARTBEAT" else "[REAL] " + str(message),
            task_progress=progress, live_metrics=metrics)
        if event_type != "PROCESS_HEARTBEAT":
            await self.hub.broadcast(ProductionEvent(type=event_type if event_type in {"METRICS_UPDATED", "AI_ANALYSIS_UPDATED"} else "STAGE_PROGRESS",
                task_id=task_id, run_id=run_id, stage="collector", progress=progress, message=str(message), data={**event, "task_id": task_id}))

    async def launch(self, task_id: str, run_id: str) -> None:
        job = asyncio.create_task(self.run(task_id, run_id))
        self.jobs[run_id] = job
        job.add_done_callback(lambda _job: self.jobs.pop(run_id, None))

    async def run(self, task_id: str, run_id: str) -> None:
        started = time.monotonic()
        stop = self.active[run_id]
        def begin(state):
            task = find(state, "tasks", task_id)
            duplicate = self._claim_source(state, task)
            if duplicate:
                run = find(state, "runs", run_id)
                run.update(status="success", progress=100)
                run["logs"].append("[DUPLICATE] " + duplicate["source_identity_key"] + "；未启动子进程或外部请求")
                self._run_end(run, {}, started)
                return duplicate
            find(state, "runs", run_id).update(status="running", started_at=now())
            task_stage(state, task_id, "collector").update(status="running", start_time=now())
        try:
            duplicate = await asyncio.to_thread(self.repository.mutate, begin)
        except (HTTPException, ValueError):
            await asyncio.to_thread(self._fail, task_id, run_id, "COLLECTION_IDENTITY_CONFLICT",
                "提交前视频身份核验失败；没有启动子进程或付费请求", None, {}, started)
            self.active.pop(run_id, None)
            await self.hub.broadcast(ProductionEvent(type="STAGE_FAILED", task_id=task_id, run_id=run_id,
                stage="collector", status="FAILED", message="视频身份冲突，已停止提交"))
            return
        if duplicate:
            self.active.pop(run_id, None)
            await self.hub.broadcast(ProductionEvent(type="STAGE_COMPLETED", task_id=task_id, run_id=run_id,
                stage="collector", status="SKIPPED", message=duplicate["message"], data=duplicate))
            return
        await self.hub.broadcast(ProductionEvent(type="STAGE_STARTED", task_id=task_id, run_id=run_id, stage="collector", status="COLLECTING", message="开始真实 Collector"))
        loop = asyncio.get_running_loop()
        def callback(event):
            future = asyncio.run_coroutine_threadsafe(self._event(task_id, run_id, event), loop)
            future.result(timeout=30)
        process = {}
        try:
            options = {"on_event": callback, "cancel_event": self.active[run_id], "run_id": run_id}
            if ":download-resume:" in find((await asyncio.to_thread(self.repository.state)), "runs", run_id).get("idempotency_key", ""):
                options["resume_download"] = True
            if ":semantic-protocol-retry:" in find((await asyncio.to_thread(self.repository.state)), "runs", run_id).get("idempotency_key", ""):
                options["semantic_retry"] = True
            raw = await self.adapter.collect_one(await asyncio.to_thread(self.context, task_id), **options)
            process = raw.pop("_process", {})
            result = CollectorResult.model_validate(raw).model_dump(mode="json")
            context = await asyncio.to_thread(self.context, task_id)
            if any(result[key] != context[key] for key in context):
                raise ValueError("RESULT_CONTEXT_MISMATCH")
            if result["video_id"]:
                for event_type in ("METRICS_UPDATED", "AI_ANALYSIS_UPDATED", "TASK_DECISION"):
                    await self.hub.broadcast(ProductionEvent(type=event_type, task_id=task_id, run_id=run_id, stage="collector", data=result, message=result["decision_reason"]))
            if result["error_code"]:
                await asyncio.to_thread(self._fail, task_id, run_id, result["error_code"], result["error_message"] or result["decision_reason"], result, process, started)
                event_type = "STAGE_FAILED"
            else:
                if not result["video_id"] or not result["video_url"] or not result["decision_reason"]:
                    raise ValueError("RESULT_INCOMPLETE")
                completed = await asyncio.to_thread(self._finish, task_id, run_id, result, process, started)
                event_type = "STAGE_COMPLETED" if completed else "STAGE_FAILED"
        except asyncio.CancelledError:
            self.active[run_id].set()
            phase = (await asyncio.to_thread(self.adapter.checkpoint, await asyncio.to_thread(self.context, task_id)))["phase"] if isinstance(self.adapter, LegacyCollectorAdapter) else "UNKNOWN_EXTERNAL_REQUEST"
            code = "DOWNLOAD_PENDING" if phase in {"DOWNLOAD_PENDING", "RESULT_READY", "SOURCE_VERIFICATION_READY"} else "PROCESS_INTERRUPTED" if phase in {"NOT_SUBMITTED", "SEMANTIC_RETRY_READY"} else "UNKNOWN_EXTERNAL_REQUEST"
            await asyncio.to_thread(self._fail, task_id, run_id, code, "服务停止；保留原任务和外部 ID，等待显式恢复", None, process, started)
            raise
        except Exception as exc:
            # Never persist arbitrary exception text from third-party API responses.
            await asyncio.to_thread(self._fail, task_id, run_id, "INTEGRATION_ERROR", "Collector 集成异常：" + type(exc).__name__, None, process, started)
            event_type = "STAGE_FAILED"
        finally:
            self.active.pop(run_id, None)
        task = find((await asyncio.to_thread(self.repository.state)), "tasks", task_id)
        policy = load_policy(self.settings)
        previous_run = await asyncio.to_thread(self.repository.read_run, run_id)
        if (policy["enabled"] and previous_run.get("error_code") in SAFE_RETRY_ERRORS
                and task["status"] == "FAILED" and previous_run.get("retry_count", 0) < policy["safe_pre_submit_retry_limit"]
                and isinstance(self.adapter, LegacyCollectorAdapter)):
            checkpoint = await asyncio.to_thread(self.adapter.checkpoint, await asyncio.to_thread(self.context, task_id))
            if checkpoint["phase"] == "NOT_SUBMITTED":
                batch_id = task["context"].get("production", {}).get("batch_id")
                current_state = await asyncio.to_thread(self.repository.state)
                batch = next((value for value in current_state["batches"] if value["id"] == batch_id), None)
                if stop.is_set() or batch and batch["status"] != "RUNNING":
                    return
                delay = min(30, policy["retry_backoff_seconds"] * 2 ** previous_run.get("retry_count", 0))
                await self.hub.broadcast(ProductionEvent(type="STAGE_PROGRESS", task_id=task_id, run_id=run_id,
                    stage="collector", status="RETRYING", message="已证实外部请求未提交；有限退避重试，不重提 UNKNOWN 请求"))
                await asyncio.sleep(delay)
                if not self.settings.integration_dry_run:
                    queued = await self.queue(task_id, retry=True, cancel_event=stop)
                    if queued.get("status") == "duplicate":
                        return
                    await self.run(task_id, queued["run_id"])
                    return
        if task["status"] == "WAITING_REVIEW" and task["collector_result"].get("decision_reason") == "watermark_unverified" and not task["collector_result"].get("error_code"):
            try:
                await asyncio.to_thread(self.source_reviews.register_pending, task_id, allow_active=True)
            except HTTPException:
                await asyncio.to_thread(self.repository.mutate, lambda state: audit(state, "source.watermark_review_not_ready", task_id, "原片审核证据未完整匹配；保留 WAITING_REVIEW，不放行", task_id))
        await self.hub.broadcast(ProductionEvent(type=event_type, task_id=task_id, run_id=run_id, stage="collector", status=task["status"], message=task["collector_result"].get("decision_reason") or "Collector 运行结束"))
        verified_download = task["context"].get("download_checkpoint", {})
        if verified_download.get("verification_run_id") == run_id:
            await self.hub.broadcast(ProductionEvent(type="STAGE_COMPLETED", task_id=task_id, run_id=run_id, stage="download",
                status="READY", message="原 VidBee 视频已核验；核验记录关联 Collector Run，不伪造下载时间", data=verified_download))

    def _run_end(self, run, process, started):
        finished = now()
        if started is not None:
            duration_ms = int((time.monotonic() - started) * 1000)
        elif run.get("started_at"):
            duration_ms = max(0, int((datetime.fromisoformat(finished) - datetime.fromisoformat(run["started_at"].replace("Z", "+00:00"))).total_seconds() * 1000))
        else:
            duration_ms = None
        run.update(finished_at=finished, duration_ms=duration_ms)
        for key in ("pid", "exit_code", "stdout_log_path", "stderr_log_path", "command", "cwd", "termination_signal", "timed_out", "resource_info"):
            if key in process:
                run[key] = process[key] or None if key == "pid" else {**run.get(key, {}), **(process[key] or {})} if key == "resource_info" else process[key]
        if process.get("last_heartbeat"):
            run["last_heartbeat"] = datetime.fromtimestamp(process["last_heartbeat"], timezone.utc).isoformat()
        if process.get("log_path") and not run.get("stdout_log_path"):
            run["stdout_log_path"] = process["log_path"]

    @staticmethod
    def _record_download_checkpoint(state, task, run, source, asset_id, reconciliation=None):
        download = task_stage(state, task["id"], "download")
        if download["status"] != "success":
            download.update(status="success", start_time=None, end_time=run["finished_at"], duration=0, error=None)
        recovered = reconciliation is not None or ":download-resume:" in run.get("idempotency_key", "") or ":semantic-protocol-retry:" in run.get("idempotency_key", "")
        result = task["collector_result"]
        checkpoint = {"status": "READY", "verification_run_id": run["id"], "provider": "LegacyCollectorAdapter",
            "source_asset_id": asset_id, "source_video_id": task["source_video_id"],
            "legacy_vidbee_task_id": result["legacy_vidbee_task_id"], "video_id": result["video_id"],
            "context_id": result["context_id"], "path": source["path"], "sha256": source["sha256"],
            "file_size": source["file_size"], "frame_count": source.get("frame_count"), "full_decode_verified": True,
            "recorded_at": reconciliation["recorded_at"] if reconciliation else run["finished_at"],
            "origin_verification_recorded_at": run["finished_at"], "timing_scope": "verification_recorded_in_collector_run",
            "download_started_at": None, "download_duration_ms": None, "recovered_original_download": recovered,
            "new_download_submission_count": 0 if recovered else None}
        if reconciliation:
            checkpoint["reconciliation"] = reconciliation
        else:
            run["logs"].append("[REAL] 下载阶段校验完成；" + ("复用原 VidBee 文件，0 个新下载；" if recovered else "") + "核验关联此 Collector Run；原下载开始和耗时未知")
            audit(state, "collector.download_verified", run["id"], "原文件存在、VidBee completed、ffprobe + full decode；不伪造下载提交", task["id"])
        task["context"].setdefault("download_checkpoints", []).append(checkpoint)
        task["context"]["download_checkpoint"] = checkpoint

    def _finish(self, task_id, run_id, result, process, started):
        policy = load_policy(self.settings)
        source = result["source_asset_metadata"]
        if source and (not source.get("verified") or not Path(source["path"]).is_file() or result["download_status"] != "completed"):
            self._fail(task_id, run_id, "OUTPUT_NOT_FOUND", "未满足文件存在 + VidBee 完成 + ffprobe 校验，不登记下载成功", result, process, started)
            return False
        asset_id = new_id("ast")
        links = {}
        def change(state):
            task, run = find(state, "tasks", task_id), find(state, "runs", run_id)
            duplicate = self._claim_source(state, task, result.get("video_id"), result.get("video_url"))
            if duplicate:
                run.update(status="success", progress=100)
                run["logs"].append("[DUPLICATE] 命中历史身份；不新增候选、下载资产或 Review")
                self._run_end(run, process, started)
                return
            review = result["decision"] == "manual_review"
            status = "WAITING_REVIEW" if review else "REJECTED" if result["decision"] == "rejected" else "COLLECTED"
            task.update(status=status, progress=100, updated_at=now(), collector_result=result)
            stage = task_stage(state, task_id, "collector")
            stage.update(status="waiting_review" if review else "success", end_time=now(), duration=time.monotonic() - started, error=None)
            run.update(status="success", progress=100)
            run["logs"].append("[REAL] Decision=" + result["decision"] + "；" + result["decision_reason"])
            self._run_end(run, process, started)
            row = next(row for row in state["collector"] if row.get("last_task_id") == task_id)
            row.update(status=task["status"], views=result["views"] or 0, likes=result["likes"] or 0, result=result)
            binding = next(row for row in state["legacy_bindings"] if row["task_id"] == task_id)
            binding.update(video_id=result["video_id"], download_task_id=result["legacy_vidbee_task_id"], legacy_reference=result["legacy_result_reference"])
            candidate_path = result["legacy_result_reference"].get("raw_result_path")
            asset = {"id": asset_id, "task_id": task_id, "type": "source" if source else "source_candidate",
                "asset_type": "SOURCE_VIDEO" if source else "SOURCE_CANDIDATE", "name": Path(source["path"]).name if source else result["video_id"],
                "thumbnail": "", "duration": source["duration"] or 0 if source else 0,
                "width": source["width"] if source else 0, "height": source["height"] if source else 0, "fps": source["fps"] or 0 if source else 0,
                "codec": source["codec"] or "UNKNOWN" if source else "UNKNOWN", "country": task["country"], "keyword": task["keyword"],
                "status": "downloaded" if source else "candidate", "storage_path": source["path"] if source else candidate_path or "",
                "external_path": source["path"] if source else None, "created_at": now(),
                "sha256": source["sha256"] if source else None, "size": source["file_size"] if source else None,
                "mime_type": (mimetypes.guess_type(source["path"])[0] or "application/octet-stream") if source else "application/json", "source_system": "TikTok_Auto",
                "source_url": result["video_url"], "legacy_video_id": result["video_id"], "legacy_context_id": result["context_id"],
                "legacy_reference": result["legacy_result_reference"], "registry_status": "registered" if source else "unverified",
                "probe_metadata": source or {}}
            existing = next((value for value in state["assets"] if source and ":semantic-protocol-retry:" in run.get("idempotency_key", "")
                and value["task_id"] == task_id and value["type"] == "source" and value["registry_status"] == "registered"
                and value.get("sha256") == source["sha256"] and value.get("legacy_video_id") == result["video_id"]
                and value.get("legacy_context_id") == result["context_id"] and Path(value.get("external_path") or value["storage_path"]).resolve() == Path(source["path"]).resolve()), None)
            if existing:
                result["legacy_result_reference"]["reused_source_asset_id"] = existing["id"]
                run["logs"].append("[REAL] 复用原 SOURCE_VIDEO=" + existing["id"] + "；没有新增下载或覆盖资产")
            else:
                state["assets"].append(asset)
            if source:
                if not existing:
                    task["source_video_id"] = new_id("source")
                binding.update(source_path=source["path"], sha256=source["sha256"])
                links[existing["id"] if existing else asset_id] = {"views": result["views"], "likes": result["likes"],
                    "source": {"download_task_id": result["legacy_vidbee_task_id"], "r_frame_rate": source.get("r_frame_rate"),
                               "avg_frame_rate": source.get("avg_frame_rate"), "probe_metadata": source},
                    "analysis": {"product_match_score": result["product_match_confidence"], "overlay_detected": result["overlay_detected"], "watermark_detected": result["watermark_detected"]}}
                verified = source.get("download_verification", {})
                if source.get("full_decode_verified") is True and verified.get("status") == "completed" and verified.get("task_id") == result["legacy_vidbee_task_id"]:
                    self._record_download_checkpoint(state, task, run, source, existing["id"] if existing else asset_id)
            audit(state, "collector.decision", run_id, result["decision"] + ": " + result["decision_reason"], task_id)
            machine = record_machine_decision(state, task, result, policy, run_id=run_id)
            if machine:
                row["status"] = task["status"]
                if task["status"] == "QUARANTINED":
                    stage.update(status="skipped", error="AUTO_QUARANTINED:" + machine["reason"])
                run["logs"].append("[AUTO TRIAGE] " + machine["decision"] + ": " + machine["reason"] + "；0新增请求")
        self.repository.mutate(change, links)
        return True

    def _fail(self, task_id, run_id, code, message, result, process, started):
        policy = load_policy(self.settings)
        def change(state):
            task, run = find(state, "tasks", task_id), find(state, "runs", run_id)
            review = code in REVIEW_ERRORS
            task.update(status="WAITING_REVIEW" if review else "FAILED", updated_at=now())
            if result:
                task["collector_result"] = result
                binding = next(row for row in state["legacy_bindings"] if row["task_id"] == task_id)
                binding.update(video_id=result.get("video_id"), download_task_id=result.get("legacy_vidbee_task_id"), legacy_reference=result.get("legacy_result_reference") or {})
            run.update(status="failed", error_code=code, error_message=message)
            run["logs"].append("[REAL] " + code + "：" + message)
            self._run_end(run, process, started)
            task_stage(state, task_id, "collector").update(status="waiting_review" if review else "failed", end_time=now(), duration=(run["duration_ms"] or 0) / 1000, error=message)
            row = next(row for row in state["collector"] if row.get("last_task_id") == task_id)
            row.update(status=task["status"], result=result or {})
            if result:
                row.update(views=result["views"] or 0, likes=result["likes"] or 0)
            state["errors"].append({"id": new_id("error"), "task_id": task_id, "stage": "collector", "code": code,
                                   "message": message, "created_at": now(), "provider": run["provider"],
                                   "details": {"run_id": run_id}, "retryable": code not in FROZEN_ERRORS})
            audit(state, "collector.failed", run_id, code + "：" + message, task_id)
            machine = record_machine_decision(state, task, {**(result or {}), "error_code": code}, policy, run_id=run_id)
            if machine:
                row["status"] = task["status"]
                if task["status"] == "QUARANTINED":
                    task_stage(state, task_id, "collector").update(status="skipped", error="AUTO_QUARANTINED:" + code)
                run["logs"].append("[AUTO TRIAGE] " + machine["decision"] + ": " + code + "；0新增请求")
        self.repository.mutate(change)

    async def cancel(self, task_id: str) -> dict:
        run = next((run for run in self.repository.state()["runs"] if run["task_id"] == task_id and run["status"] in ("queued", "running") and run["provider"] == "LegacyCollectorAdapter"), None)
        if not run or run["id"] not in self.active:
            raise HTTPException(409, "NOT_RUNNING")
        self.active[run["id"]].set()
        return {"message": "已请求停止当前子进程；浏览器登录状态保留，未知付费请求不会重复提交", "task_id": task_id}

    async def recover(self):
        for run in self.repository.state()["runs"]:
            if run["provider"] == "LegacyCollectorDiscoveryAdapter" and run["status"] in {"queued", "running"}:
                self._fail(run["task_id"], run["id"], "SEARCH_INTERRUPTED", "搜索进程已退出；保留真实分页快照，恢复时不重复提交分析或下载", None, {}, None)
                continue
            if run["provider"] == "LegacyCollectorAdapter" and run["status"] in ("queued", "running"):
                phase = self.adapter.checkpoint(self.context(run["task_id"]))["phase"] if isinstance(self.adapter, LegacyCollectorAdapter) else "UNKNOWN_EXTERNAL_REQUEST"
                code = "DOWNLOAD_PENDING" if phase in {"DOWNLOAD_PENDING", "RESULT_READY", "SOURCE_VERIFICATION_READY"} else "PROCESS_INTERRUPTED" if run["status"] == "queued" and phase != "UNKNOWN_EXTERNAL_REQUEST" or phase in {"NOT_SUBMITTED", "SEMANTIC_RETRY_READY"} else "UNKNOWN_EXTERNAL_REQUEST"
                self._fail(run["task_id"], run["id"], code, "上次后端已退出；保留历史，不自动重放真实任务", None, {}, None)

    async def shutdown(self):
        for cancel in self.active.values():
            cancel.set()
        if self.jobs:
            try:
                await asyncio.wait_for(asyncio.gather(*list(self.jobs.values()), return_exceptions=True),
                                       timeout=self.settings.legacy_process_terminate_grace_seconds)
            except TimeoutError:
                # wait_for cancelled the jobs; each job persists the uncertain state.
                pass
