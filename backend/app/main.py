from contextlib import asynccontextmanager
import hashlib
from pathlib import Path
from typing import Literal

from fastapi import BackgroundTasks, FastAPI, HTTPException, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.config import Settings
from app.realtime import ProductionHub
from app.repository import StateRepository
from app.schemas import (
    AccountDetail, AccountView, ActionRequest, AssetView, AuditView, CollectorView,
    ErrorView, FactoryState, ProductView, ProjectView, PromptView, PublishingView,
    ReviewView, RunView, StageView, TaskDetail, TaskView, VideoMetadata, WorkflowView,
)
from app.storage import StoragePaths
from app.service import FactoryService
from app.integrations.collector_service import CollectorService
from app.integrations.collector_board import create_collector_board_router
from app.integrations.input_routes import create_input_router
from app.integrations.production_routes import create_production_router
from app.integrations.production_service import ProductionService
from app.integrations.review_service import RealReviewService
from app.integrations.matte_repair import MatteRepairService, create_repair_router
from app.integrations.asset_routes import create_asset_router
from app.local_catalog import create_local_catalog_router
from app.integrations.manual_compose import ManualComposeService
from app.integrations.manual_compose_routes import create_manual_compose_router
from app.integrations.local_cutout import LocalCutoutService, create_local_cutout_router
from app.integrations.publishing.config import load_publishing_config
from app.integrations.publishing.routes import create_publishing_router
from app.integrations.publishing.browser_adapter import ExistingHubBrowserAdapter
from app.integrations.publishing.product_source import ProductSourceService, create_product_source_router
from app.integrations.publishing.description_generation import DescriptionGenerationService, create_description_router


def create_app(config: Settings | None = None, collector_adapter=None) -> FastAPI:
    config = config or Settings()
    repository = StateRepository(config.database_url, Path(__file__).with_name("seed.json"), app_mode=config.app_mode)
    hub = ProductionHub()
    storage = StoragePaths(config.storage_root)
    service = FactoryService(repository, hub, storage)
    collector = CollectorService(repository, hub, config, collector_adapter)
    production = ProductionService(repository, hub, config, collector)
    review = RealReviewService(production)
    repairs = MatteRepairService(production)
    manual_compose = ManualComposeService(production)
    local_cutout = LocalCutoutService(production, manual_compose)
    publishing_router = create_publishing_router(repository, load_publishing_config())
    publishing_router.service.adapter = ExistingHubBrowserAdapter(publishing_router.service.config)
    product_source = ProductSourceService(config, publishing_router.service.config)
    descriptions = DescriptionGenerationService(publishing_router.service, product_source, config)
    input_router = create_input_router(collector, config) if config.app_mode == "integration" else None

    @asynccontextmanager
    async def lifespan(_app):
        if config.external_recovery_enabled:
            await publishing_router.service.recover()
            await descriptions.recover()
        if config.app_mode == "mock":
            await service.runtime.recover()
        active_run_ids = {run["id"] for run in repository.state()["runs"] if run["status"] in {"queued", "running"}}
        if config.production_recovery_enabled:
            await local_cutout.recover()
            await collector.recover()
            await production.recover(active_run_ids=active_run_ids)
            await repairs.recover()
            await review.reconcile_historical_rejections()
            await production.resume_recovered()
        yield
        await descriptions.shutdown()
        await product_source.shutdown()
        await local_cutout.shutdown()
        await manual_compose.shutdown()
        await publishing_router.service.shutdown(recover=config.external_recovery_enabled)
        if input_router:
            await input_router.input_service.close()
        await repairs.shutdown()
        await production.shutdown()
        await collector.shutdown()
        await service.runtime.shutdown(recover=config.app_mode == "mock")
        repository.engine.dispose()

    app = FastAPI(title=config.app_name, version="0.1.0", lifespan=lifespan)
    app.state.repository = repository
    app.state.hub = hub
    app.state.config = config
    app.state.service = service
    app.state.collector = collector
    app.state.production = production
    app.state.repairs = repairs
    app.state.publishing = publishing_router.service
    app.state.manual_compose = manual_compose
    app.state.local_cutout = local_cutout
    app.state.descriptions = descriptions
    app.include_router(publishing_router)
    app.include_router(create_product_source_router(product_source, publishing_router.service))
    app.include_router(create_description_router(descriptions))
    app.include_router(create_manual_compose_router(manual_compose))
    app.include_router(create_local_cutout_router(local_cutout))
    if input_router:
        app.include_router(input_router)
        app.include_router(create_collector_board_router(repository))
    app.include_router(create_production_router(production))
    app.include_router(create_asset_router(production.registry))
    app.include_router(create_repair_router(repairs))
    if config.app_mode == "integration":
        app.include_router(create_local_catalog_router(repository, config, production))
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[config.frontend_origin, "http://127.0.0.1:3000"],
        allow_credentials=False, allow_methods=["*"], allow_headers=["*"],
        allow_private_network=True,
    )

    def require_task(task_id: str) -> dict:
        task = next((item for item in repository.state()["tasks"] if item["id"] == task_id), None)
        if not task:
            raise HTTPException(404, "任务不存在")
        return task

    @app.get("/api/health")
    def health():
        checks = (production.readiness.cached or {}).get("checks", [])
        comfyui = next((row["status"] for row in checks if "comfy" in row["name"].lower()), "UNKNOWN")
        database_identity = hashlib.sha256(str(Path(repository.engine.url.database).resolve()).encode()).hexdigest()
        return {"status": "healthy", "database_identity": database_identity, "app_mode": config.app_mode, "integration_dry_run": config.integration_dry_run,
                "api": "online", "comfyui": comfyui, "worker": "serial_queue" if config.app_mode == "integration" and config.production_recovery_enabled else "not_started",
                "production_execution_enabled": config.production_recovery_enabled, "external_recovery_enabled": config.external_recovery_enabled,
                "publishing_backend": "CONTROLLED_UPLOAD", "publishing_production_readiness": "NOT_PRODUCTION_READY"}

    @app.get("/api/integrations/collector/rules")
    def collector_rules():
        try:
            return collector.rules()
        except (OSError, RuntimeError, ValueError):
            raise HTTPException(503, "LEGACY_CONFIG_UNAVAILABLE：请检查旧项目路径、环境与配置") from None

    @app.get("/api/integrations/collector/dry-run")
    def collector_dry_run(task_id: str):
        return collector.dry_run(task_id)

    @app.get("/api/state", response_model=FactoryState)
    def state(scope: Literal["raw", "production", "history", "technical"] = "raw"):
        return repository.state(scope=scope)

    def collection_endpoint(key: str):
        def listing(task_id: str | None = None, country: str | None = None, status: str | None = None,
                    scope: Literal["raw", "production", "history", "technical"] | None = None):
            selected_scope = scope or ("raw" if key == "publishing" else "production")
            rows = repository.state(scope=selected_scope)[key]
            return [row for row in rows if
                    (task_id is None or row.get("task_id", row.get("id")) == task_id)
                    and (country is None or row.get("country") == country)
                    and (status is None or row.get("status") == status)
                    and (key != "assets" or status == "MEDIA_DELETED" or row.get("status") != "MEDIA_DELETED")]
        return listing

    endpoints = {
        "projects": ("projects", ProjectView), "tasks": ("tasks", TaskView),
        "assets": ("assets", AssetView), "collector": ("collector", CollectorView),
        "accounts": ("accounts", AccountView), "products": ("products", ProductView),
        "review": ("reviews", ReviewView), "publish": ("publishing", PublishingView),
        "prompts": ("prompts", PromptView), "runs": ("runs", RunView),
        "errors": ("errors", ErrorView), "audits": ("audits", AuditView),
    }
    for path, (key, schema) in endpoints.items():
        app.add_api_route("/api/" + path, collection_endpoint(key), methods=["GET"], response_model=list[schema], name="list_" + key)

    @app.get("/api/tasks/{task_id}", response_model=TaskDetail)
    def task_detail(task_id: str):
        task = require_task(task_id)
        state = repository.state(scope="production")
        task = next((row for row in state["tasks"] if row["id"] == task_id), None)
        if task is None:
            raise HTTPException(404, "生产任务不存在；历史或技术记录请从对应入口查看")
        result = {"task": task, **{key: [item for item in state[key] if item["task_id"] == task_id]
            for key in ("stages", "assets", "prompts", "runs", "errors", "audits", "reviews", "quality_assessments")}}
        run_ids = {row["id"] for row in result["runs"]}
        result["workflows"] = [row for row in repository.workflows(task_id) if config.app_mode == "mock" or row.get("processing_run_id") in run_ids]
        result["batch"] = next((row for row in reversed(state["batches"]) if task_id in row["task_ids"]), None)
        result["lineage"] = [{"asset_id": row["id"], "asset_type": row["type"], "parent_asset_id": row.get("parent_asset_id") or row["legacy_reference"].get("parent_asset_id"),
            **{key: row["legacy_reference"].get(key) for key in ("source_asset_id", "person_asset_id", "background_asset_id", "composition_asset_id", "prompt_id", "processing_run_id")}}
            for row in result["assets"]]
        return result

    @app.get("/api/tasks/{task_id}/stages", response_model=list[StageView])
    def task_stages(task_id: str):
        require_task(task_id)
        return [item for item in repository.state()["stages"] if item["task_id"] == task_id]

    @app.get("/api/tasks/{task_id}/metadata", response_model=VideoMetadata)
    def task_metadata(task_id: str, scope: Literal["production", "history"] = "production"):
        require_task(task_id)
        metadata = repository.metadata(task_id, scope=scope)
        if metadata is None:
            raise HTTPException(404, "当前范围没有任务 metadata")
        return metadata

    @app.get("/api/tasks/{task_id}/workflow", response_model=WorkflowView)
    def task_workflow(task_id: str):
        require_task(task_id)
        workflow = repository.workflow(task_id)
        if not workflow:
            raise HTTPException(404, "当前任务尚无工作流快照")
        return workflow

    @app.get("/api/tasks/{task_id}/storage")
    def task_storage(task_id: str):
        task = require_task(task_id)
        return {"task_id": task_id, "mode": "logical_paths_only", "paths": storage.task_paths(task["project_id"], task_id)}

    @app.get("/api/assets/{asset_id}", response_model=AssetView)
    def asset_detail(asset_id: str):
        asset = repository.read_asset(asset_id)
        if not asset:
            raise HTTPException(404, "资产不存在")
        return asset

    @app.get("/api/accounts/{account_id}", response_model=AccountDetail)
    def account_detail(account_id: str):
        state = repository.state()
        account = next((item for item in state["accounts"] if item["id"] == account_id), None)
        if not account:
            raise HTTPException(404, "账号不存在")
        history = [item for item in state["publishing"] if item["account_id"] == account_id]
        return {"account": account, "authorization": {"mode": "mock", "secret_ref": repository.secret_reference(account_id)},
                "history": [item for item in history if item["status"] in ("published", "failed")],
                "queue": [item for item in history if item["status"] not in ("published", "failed")],
                "products": [item for item in state["products"] if item["id"] in account["product_ids"]], "errors": account["errors"]}

    @app.get("/api/cutout")
    def cutout(task_id: str | None = None, scope: Literal["raw", "production", "history", "technical"] = "production"):
        state = repository.state(scope=scope)
        tasks = state["tasks"]
        if task_id:
            require_task(task_id)
            tasks = [task for task in tasks if task["id"] == task_id]
        rows = []
        for task in tasks:
            real = task["execution_mode"] == "integration"
            assets = [asset for asset in state["assets"] if asset["task_id"] == task["id"] and asset["type"] == "person"]
            ready = any(asset["registry_status"] == "registered" and production.business_asset(asset) for asset in assets)
            rows.append({"task_id": task["id"], "assets": assets if real else repository.cutout_details(task["id"]),
                         "provider": "LegacyComfyUIAdapter" if real else "MockComfyUIProvider",
                         "status": ("READY" if ready else "NOT_STARTED") if real else "MOCK"})
        return rows

    @app.get("/api/background")
    def background(task_id: str | None = None, scope: Literal["raw", "production", "history", "technical"] = "production"):
        state = repository.state(scope=scope)
        if task_id:
            require_task(task_id)
        return {"prompts": [item for item in state["prompts"] if not task_id or item["task_id"] == task_id],
                "assets": [item for item in state["assets"] if item["type"] == "background" and (not task_id or item["task_id"] == task_id)],
                "providers": ["chatgpt_rpa", "manual", "local_comfyui", "other"], "mode": config.app_mode}

    @app.get("/api/compose")
    def compose(task_id: str | None = None, scope: Literal["raw", "production", "history", "technical"] = "production"):
        state = repository.state(scope=scope)
        if task_id:
            require_task(task_id)
        return {"assets": [item for item in state["assets"] if item["type"] in ("person", "background", "compose", "final") and (not task_id or item["task_id"] == task_id)],
                "output": {key: state["settings"]["output_" + key] for key in ("width", "height", "fps")},
                "processor": "LegacyComposeAdapter" if config.app_mode == "integration" else "MockVideoProcessor"}

    @app.post("/api/actions")
    async def dispatch_action(request: ActionRequest, background_tasks: BackgroundTasks):
        if request.action.startswith("production."):
            action = request.action.split(".", 1)[1]
            if action == "resume_cutout":
                if not request.entity_id:
                    raise HTTPException(422, "TASK_ID_REQUIRED")
                result = await production.resume_cutout_checkpoint(request.entity_id)
                if result.get("launch"):
                    background_tasks.add_task(production.launch, result["batch_id"])
                return JSONResponse(result, status_code=202)
            if action == "continue":
                if not request.entity_id:
                    raise HTTPException(422, "TASK_ID_REQUIRED")
                result = await production.continue_task(request.entity_id, explicit=True)
                if result.get("accepted"):
                    background_tasks.add_task(production.launch, result["batch_id"])
                    return JSONResponse(result, status_code=202)
                return result
            if action in {"start", "retry"}:
                task_ids = request.payload.get("task_ids") or ([request.entity_id] if request.entity_id else [])
                result = await production.start(task_ids, retry=action == "retry", stage=request.payload.get("stage"),
                    prompt_id=request.payload.get("prompt_id"), person_asset_id=request.payload.get("person_asset_id"))
                background_tasks.add_task(production.launch, result["batch_id"])
                return JSONResponse(result, status_code=202)
            return await production.control(action, request.entity_id, batch_id=request.payload.get("batch_id"))
        if request.action.startswith("review."):
            result = await review.action(request.action.split(".", 1)[1], request.entity_id, request.payload)
            if result.get("accepted"):
                background_tasks.add_task(production.launch, result["batch_id"])
                return JSONResponse(result, status_code=202)
            return result
        if request.action in {"create_collection", "start_collection", "retry_collection", "resume_collection", "cancel_collection"}:
            if config.app_mode != "integration":
                raise HTTPException(409, "INTEGRATION_MODE_REQUIRED")
            try:
                if request.action == "create_collection":
                    return await collector.create(request.payload)
                if not request.entity_id:
                    raise HTTPException(422, "必须提供独立 task_id（entity_id）")
                if request.payload:
                    raise HTTPException(422, "任务上下文创建后不可在 Start/Retry 中替换")
                if request.action == "cancel_collection":
                    return await collector.cancel(request.entity_id)
                if collector_adapter is None:
                    result = await production.start([request.entity_id], retry=request.action == "retry_collection")
                    background_tasks.add_task(production.launch, result["batch_id"])
                    return JSONResponse(result, status_code=202)
                result = await collector.queue(request.entity_id, request.action == "retry_collection",
                                               resume_download=request.action == "resume_collection")
                if result.get("status") == "duplicate":
                    return result
                background_tasks.add_task(collector.launch, result["task_id"], result["run_id"])
                return JSONResponse(result, status_code=202)
            except ValidationError:
                raise HTTPException(422, "Collector 参数或 TaskContext 不符合接口要求") from None
        if config.app_mode == "integration" and request.action in {"create_task", "save_collector", "import_collector", "collect", "reset_demo"}:
            raise HTTPException(409, "REAL_COLLECTOR_ONLY：请从采集数据来源创建任务，再点击开始生产")
        if config.app_mode == "integration" and request.action in {
            "start_cutout", "retry_interval", "regenerate_mask", "manual_fix", "accept_cutout", "generate_prompt",
            "generate_background", "upload_background", "compose", "update_composition", "pause_task", "retry_task",
            "review_approve", "review_reject", "review_retry", "review_manual", "authorize_account", "update_account_status",
            "save_product", "save_settings",
        }:
            raise HTTPException(409, "MOCK_ACTION_ISOLATED：请使用真实生产、审核或本地账号管理接口")
        state = repository.state()
        related_id = request.entity_id
        for collection in ("assets", "reviews", "prompts", "publishing"):
            entity = next((item for item in state[collection] if item["id"] == related_id), None)
            if entity:
                related_id = entity["task_id"]
                break
        task = next((task for task in state["tasks"] if task["id"] == related_id), None)
        if task and task["execution_mode"] == "integration":
            raise HTTPException(409, "NOT_CONNECTED：请使用真实生产操作；Mock 动作不能操作真实任务")
        if request.action == "save_publishing" and any(row.get("task_id") in {task["id"] for task in state["tasks"] if task["execution_mode"] == "integration"} for row in request.payload.get("rows", [])):
            raise HTTPException(409, "NOT_CONNECTED：真实 Task 不能使用 Mock 发布")
        return await service.execute(request)

    @app.websocket("/ws/production")
    async def production_socket(socket: WebSocket):
        await hub.listen(socket)

    @app.websocket("/ws/tasks/{task_id}")
    async def task_socket(socket: WebSocket, task_id: str):
        if not any(task["id"] == task_id for task in repository.state()["tasks"]):
            await socket.close(code=4404, reason="任务不存在")
            return
        await hub.listen(socket, task_id)

    return app


app = create_app()
