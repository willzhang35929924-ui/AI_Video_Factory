import asyncio
from contextlib import contextmanager
import json
import threading
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select

from app import models as m
from app.domain import new_id
from app.repository import record
from app.integrations.input_service import source_url, write_json
from app.integrations.publishing.description_selector import description_pool_hash
from app.integrations.process_runner import ProcessRunner
from app.integrations.publishing.schemas import ProductInput, AccountInput
from app.integrations.publishing.service import PublishingService


class SourceRequest(BaseModel):
    url: str | None = None


class ImportRequest(BaseModel):
    row_indexes: list[int] = Field(min_length=1, max_length=100)
    import_accounts: bool = True
    product_mappings: dict[int, str] = Field(default_factory=dict)


class _SourceImportCatalog(PublishingService):
    def __init__(self, publishing, session):
        super().__init__(publishing.repository, publishing.config, publishing.adapter)
        self.transaction = session

    @contextmanager
    def session(self):
        yield self.transaction


class ProductSourceService:
    def __init__(self, settings, publishing_config):
        self.settings, self.config = settings, publishing_config
        self.root = publishing_config.output_root / "product_sources"
        self.jobs, self.cancellations = {}, {}

    def directory(self, job_id):
        if not job_id.startswith("source-") or len(job_id) != 39 or any(c not in "0123456789abcdef" for c in job_id[7:]):
            raise HTTPException(404, "SOURCE_JOB_NOT_FOUND")
        return self.root / job_id

    async def create(self, url=None):
        url = source_url(url or self.config.product_source_url)
        job_id = "source-" + uuid4().hex
        directory = self.directory(job_id)
        directory.mkdir(parents=True)
        write_json(directory / "request.json", {"source_document": url})
        write_json(directory / "status.json", {"id": job_id, "status": "QUEUED", "source_document": url})
        self.cancellations[job_id] = threading.Event()
        self.jobs[job_id] = asyncio.create_task(self.read(job_id))
        return self.status(job_id)

    def status(self, job_id):
        directory = self.directory(job_id)
        path = directory / "status.json"
        if not path.is_file():
            raise HTTPException(404, "SOURCE_JOB_NOT_FOUND")
        value = json.loads(path.read_text())
        if (directory / "result.json").is_file():
            result = json.loads((directory / "result.json").read_text())
            if value.get("finished_at") and value.get("exit_code") == 0 and result.get("status") == "READY":
                value["status"] = "READY"
            value = {**result, **value}
        return value

    async def read(self, job_id):
        directory = self.directory(job_id)
        def event(item):
            if item.get("type") == "PROCESS_STARTED":
                write_json(directory / "status.json", {"id": job_id, "status": "READING", "pid": item["pid"]})
        result = await asyncio.to_thread(ProcessRunner().run, [str(self.settings.legacy_tiktok_auto_python),
            str(Path(__file__).with_name("product_source_worker.py")), "--legacy-root", str(self.settings.legacy_tiktok_auto_root),
            "--job-dir", str(directory), "--product-selection-mode", self.config.product_selection_mode], cwd=directory, log_dir=directory / "logs", timeout_s=180,
            on_event=event, cancel_event=self.cancellations[job_id])
        state = {"id": job_id, "status": "FAILED", "pid": result.pid, "exit_code": result.exit_code,
                 "started_at": result.started_at, "finished_at": result.finished_at, "log_path": result.log_path}
        if (directory / "result.json").is_file():
            exported = json.loads((directory / "result.json").read_text())
            if exported.get("status") == "READY" and result.exit_code == 0:
                state["status"] = "READY"
            elif exported.get("status") == "WAITING_MANUAL":
                state["status"] = "WAITING_MANUAL"
            elif exported.get("status") == "READY":
                state.update(error_code="SOURCE_PROCESS_FAILED", message="导出结果缺少成功进程证据；未允许导入")
        else:
            state.update(error_code="SOURCE_PROCESS_FAILED", message="导出进程未产生结果；请查看日志")
        write_json(directory / "status.json", state)

    async def shutdown(self):
        for event in self.cancellations.values():
            event.set()
        await asyncio.gather(*self.jobs.values(), return_exceptions=True)


def create_product_source_router(service, publishing):
    router = APIRouter(prefix="/api/publishing/product-source", tags=["publishing-source"])

    @router.post("", status_code=202)
    async def read(request: SourceRequest):
        return await service.create(request.url)

    @router.get("/latest")
    def latest():
        paths = sorted((path for path in service.root.glob("source-*/request.json") if (path.parent / "status.json").is_file()),
                       key=lambda path: path.stat().st_mtime_ns, reverse=True)
        return service.status(paths[0].parent.name) if paths else {"id": "", "status": "EMPTY"}

    @router.get("/{job_id}")
    def status(job_id: str):
        return service.status(job_id)

    @router.post("/{job_id}/import")
    def import_rows(job_id: str, request: ImportRequest):
        source = service.status(job_id)
        if source["status"] != "READY":
            raise HTTPException(409, "PRODUCT_SOURCE_NOT_READY")
        rows = source["rows"]
        if set(request.product_mappings) - set(request.row_indexes):
            raise HTTPException(422, "PRODUCT_MAPPING_ROW_NOT_SELECTED")
        required = ("country", "keyword")
        if publishing.config.product_selection_mode == "exact_id":
            required += ("product_link_id",)
        if any(index < 0 or index >= len(rows) or not all(rows[index].get(key) for key in
               required) for index in request.row_indexes):
            raise HTTPException(422, "SELECT_IDENTIFIED_SOURCE_ROWS")
        imported = []
        with publishing.session() as session:
            catalog = _SourceImportCatalog(publishing, session)
            try:
                for index in dict.fromkeys(request.row_indexes):
                    row = rows[index]
                    target_id = request.product_mappings.get(index)
                    if target_id:
                        target = publishing.get(session, m.Product, target_id)
                        normalize = lambda value: " ".join(value.casefold().split())
                        keywords = [*target.keywords, *session.scalars(select(m.Task.keyword).where(m.Task.product_id == target.id, m.Task.execution_mode == "integration"))]
                        if target.country != row["country"] or normalize(row["keyword"]) not in {normalize(value) for value in keywords}:
                            raise HTTPException(409, "SOURCE_PRODUCT_COUNTRY_KEYWORD_MISMATCH")
                        previous_source = target.publishing_context or {}
                        if previous_source.get("source_document") and any(previous_source.get(key, "") != row.get(key, "")
                                for key in ("source_document", "source_row", "worksheet", "account_name", "telephone_number")):
                            raise HTTPException(409, "SOURCE_PRODUCT_CONTEXT_CONFLICT")
                        old = record(target)
                    else:
                        candidates = [product for product in catalog.products()
                                      if product.get("publishing_context", {}).get("context_id") == row["context_id"]]
                        if len(candidates) > 1:
                            raise HTTPException(409, "PRODUCT_MAPPING_AMBIGUOUS")
                        old = candidates[0] if candidates else {}
                    context = {key: row[key] for key in ("telephone_number", "account_name", "keyword", "source_document", "source_row", "context_id")}
                    context["data_status"] = row["status"]
                    context.update({key: row[key] for key in ("worksheet", "header_row", "column_map", "description_columns", "protected_description_ordinals") if key in row})
                    product = catalog.save_product(ProductInput(name=old.get("name") or row["keyword"], country=row["country"],
                        image=old.get("image", ""), url=old.get("url", ""), description=old.get("description", ""),
                        status=old.get("status", "active"), description_selection=old.get("description_selection", {}),
                        binding_reference=row.get("product_link_id") or "", platform_product_id=row.get("product_link_id") or None,
                        keywords=list(dict.fromkeys([*old.get("keywords", []), row["keyword"]])), descriptions=list({item["id"]: item for item in [*old.get("descriptions", []), *row["descriptions"]]}.values()),
                        publishing_context={**old.get("publishing_context", {}), **context}), old.get("id"))
                    saved = publishing.get(session, m.Product, product["id"])
                    if saved.descriptions:
                        digest = description_pool_hash(saved.descriptions)
                        if not saved.publishing_context.get("description_review_required") or digest != description_pool_hash(old.get("descriptions", [])):
                            session.add(m.AuditLog(id=new_id("audit"), action="publishing.description_source_imported", entity_id=saved.id,
                                message=json.dumps({"source_context": row["context_id"], "sha256": digest, "content_review": "REQUIRED"}), created_at=m.utc_now()))
                        saved.publishing_context = {**saved.publishing_context, "description_review_required": True}
                    if not row["account_name"] or not request.import_accounts:
                        imported.append({"product_id": product["id"], "account_id": None, "context_id": row["context_id"]})
                        continue
                    accounts = [account for account in catalog.accounts() if account["country"] == row["country"] and account["name"] == row["account_name"]]
                    if len(accounts) > 1:
                        raise HTTPException(409, "ACCOUNT_MAPPING_AMBIGUOUS")
                    old_account = accounts[0] if accounts else {}
                    old_status = old_account.get("legacy_status", "NOT_CONNECTED")
                    account = catalog.save_account(AccountInput(name=row["account_name"], country=row["country"],
                        product_ids=list(dict.fromkeys([*old_account.get("product_ids", []), product["id"]])),
                        enabled=old_account.get("enabled", True), status=old_status if old_status in {"NOT_CONNECTED", "WAITING_MANUAL", "EXPIRED", "DISABLED"} else "NOT_CONNECTED",
                        cooldown_seconds=old_account.get("cooldown_seconds", 120),
                        expected_username=old_account.get("expected_username"), cdp_endpoint=old_account.get("cdp_endpoint"),
                        hubstudio_env_id=old_account.get("hubstudio_env_id"), publishing_context={**old_account.get("publishing_context", {}), **context}), old_account.get("id"))
                    imported.append({"product_id": product["id"], "account_id": account["id"], "context_id": row["context_id"]})
            except ValidationError:
                raise HTTPException(422, "INVALID_PRODUCT_SOURCE_ROW") from None
        return {"status": "IMPORTED", "rows": imported, "message": "产品文案与账号映射已保存；未授权、上传或发布"}

    return router
