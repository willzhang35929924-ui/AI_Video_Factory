"""Read-only, database-paged Collector task board."""
import copy
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session

from app.models import AuditLog, ProcessingRun, Task
from app.repository import record
from app.schemas import RunView, TaskView

CATEGORIES = (
    ("DUPLICATE_SKIPPED", "历史重复，已跳过"),
    ("PENDING", "待处理"), ("PROCESSING", "正在采集 / 处理中"),
    ("AUTO_ACCEPTED", "自动通过"), ("AUTO_REJECTED", "自动拒绝"), ("QUARANTINED", "隔离，不参与生产"),
    ("AUTO_FAILED", "故障终止"), ("RETRYING", "有限重试中"),
    ("WAITING_REVIEW", "独立人工采集审核"), ("DOWNSTREAM_REVIEW", "后续生产审核 / 外部等待"), ("WAITING_RESOURCE", "等待外部预算或资源"),
    ("ACCEPTED_QUEUED", "已同意、等待执行"), ("REJECTED", "已拒绝"),
    ("COMPLETED", "已完成"), ("FAILED", "失败"), ("CANCELLED", "已取消"),
    ("PAUSED", "已暂停"), ("SKIPPED", "已跳过"), ("OTHER", "其他真实状态"),
)
RESOURCE_ERRORS = ("EXTERNAL_BUDGET_EXHAUSTED", "EXTERNAL_AUTHORIZATION_REQUIRED", "DOWNLOAD_PENDING", "RESOURCE_BUSY")


class CollectorBoard:
    def __init__(self, repository):
        self.repository = repository

    def list(self, *, status="all", page=1, page_size=10, search="", sort="updated_desc", country=None, project_id=None, platform=None):
        latest_error = (select(ProcessingRun.error_code).where(ProcessingRun.task_id == Task.id,
            ProcessingRun.provider == "LegacyCollectorAdapter").order_by(ProcessingRun.created_at.desc(), ProcessingRun.id.desc()).limit(1).scalar_subquery())
        error = func.coalesce(Task.collector_result["error_code"].as_string(), latest_error, "")
        intent = Task.context["collector_candidate_decision"]["decision"].as_string()
        triage = Task.context["collector_auto_triage"]["decision"].as_string()
        category = case(
            (Task.context["collection_identity"]["status"].as_string() == "DUPLICATE_SKIPPED", "DUPLICATE_SKIPPED"),
            (Task.status == "QUARANTINED", "QUARANTINED"),
            (and_(Task.status == "REJECTED", triage == "AUTO_REJECTED"), "AUTO_REJECTED"),
            (and_(Task.status == "FAILED", triage == "AUTO_FAILED"), "AUTO_FAILED"),
            (and_(Task.status == "COLLECTING", triage == "AUTO_FAILED"), "RETRYING"),
            (and_(Task.status == "COLLECTED", triage == "AUTO_ACCEPTED"), "AUTO_ACCEPTED"),
            (or_(Task.status == "REJECTED", intent == "REJECTED"), "REJECTED"),
            (Task.status.in_(("COLLECTING", "RUNNING")), "PROCESSING"),
            (Task.status == "FAILED", "FAILED"),
            (Task.status == "CANCELLED", "CANCELLED"),
            (and_(Task.status == "WAITING_REVIEW", Task.current_stage.not_in(("collector", "analysis", "download"))), "DOWNSTREAM_REVIEW"),
            (and_(Task.status == "WAITING_REVIEW", error.in_(RESOURCE_ERRORS)), "WAITING_RESOURCE"),
            (and_(intent == "ACCEPTED", Task.status.in_(("CREATED", "QUEUED", "WAITING_REVIEW"))), "ACCEPTED_QUEUED"),
            (Task.status == "WAITING_REVIEW", "WAITING_REVIEW"),
            (Task.status.in_(("COLLECTED", "COMPLETED")), "COMPLETED"),
            (Task.status.in_(("CREATED", "QUEUED")), "PENDING"),
            (Task.status == "PAUSED", "PAUSED"), (Task.status == "SKIPPED", "SKIPPED"),
            else_="OTHER",
        )
        base = [Task.execution_mode == "integration", Task.context["production_rules"]["sha256"].as_string().is_not(None)]
        filters = list(base)
        if country:
            filters.append(Task.country == country)
        if project_id:
            filters.append(Task.project_id == project_id)
        if platform and platform != "TikTok":
            filters.append(Task.id.is_(None))
        if search.strip():
            value = search.strip()
            filters.append(or_(*(field.contains(value, autoescape=True) for field in (
                Task.id, Task.keyword, Task.collector_result["video_id"].as_string(),
                Task.context["task_context"]["video_id"].as_string(), Task.context["collection_source"]["video_id"].as_string(),
                Task.collector_result["title"].as_string(), Task.collector_result["author"].as_string(),
                Task.collector_result["legacy_result_reference"]["title"].as_string(),
                Task.collector_result["legacy_result_reference"]["author"].as_string(),
            ))))
        with self.repository.lock, Session(self.repository.engine) as session:
            counts = dict(session.execute(select(category, func.count(Task.id)).where(*filters).group_by(category)).all())
            categories = [{"status": key, "label": label, "count": counts.get(key, 0)} for key, label in CATEGORIES]
            if status != "all":
                filters.append(category == status)
            total = session.scalar(select(func.count(Task.id)).where(*filters)) or 0
            total_pages = (total + page_size - 1) // page_size
            page = min(max(1, page), max(1, total_pages))
            timestamp = Task.created_at if sort.startswith("created") else Task.updated_at
            descending = sort.endswith("desc")
            order = (timestamp.desc(), Task.id.desc()) if descending else (timestamp.asc(), Task.id.asc())
            rows = session.scalars(select(Task).where(*filters).order_by(*order).offset((page - 1) * page_size).limit(page_size)).all()
            items = []
            for row in rows:
                values = {key: value for key, value in record(row).items() if key in TaskView.model_fields}
                values["context"] = {key: copy.deepcopy(row.context[key]) for key in
                    ("task_context", "collection_source", "collection_identity", "collector_duplicate", "collection_search", "collector_candidate_decision", "collector_auto_triage", "live_metrics") if key in row.context}
                items.append(TaskView.model_validate(values).model_dump(mode="json"))
            task_ids = [row.id for row in rows]
            runs = []
            if task_ids:
                latest_ids = select(ProcessingRun.id, func.row_number().over(partition_by=ProcessingRun.task_id,
                    order_by=(ProcessingRun.created_at.desc(), ProcessingRun.id.desc())).label("position")).where(
                        ProcessingRun.task_id.in_(task_ids), ProcessingRun.provider == "LegacyCollectorAdapter").subquery()
                for run in session.scalars(select(ProcessingRun).join(latest_ids, ProcessingRun.id == latest_ids.c.id).where(latest_ids.c.position == 1)):
                    values = {key: value for key, value in record(run).items() if key in RunView.model_fields}
                    if len(values["logs"]) > 200:
                        values["logs"] = ["仅显示最近 200 条日志；完整 stdout/stderr 保留在此 Run 的日志文件。", *values["logs"][-200:]]
                    runs.append(RunView.model_validate(values).model_dump(mode="json"))
            countries = list(session.scalars(select(Task.country).where(*base).distinct().order_by(Task.country)))
            projects = list(session.scalars(select(Task.project_id).where(*base).distinct().order_by(Task.project_id)))
            encounter_count = session.scalar(select(func.count(AuditLog.id)).join(Task, AuditLog.task_id == Task.id).where(
                *base, AuditLog.action == "collector.duplicate_seen")) or 0
        return {"items": items, "runs": runs, "total": total, "page": page, "page_size": page_size,
            "effective_total": sum(counts.values()) - counts.get("DUPLICATE_SKIPPED", 0),
            "duplicate_total": counts.get("DUPLICATE_SKIPPED", 0), "duplicate_encounters": encounter_count,
            "total_pages": total_pages, "matching_total": sum(counts.values()), "categories": categories,
            "filters": {"countries": countries, "project_ids": projects, "platforms": ["TikTok"]}}


def create_collector_board_router(repository):
    router = APIRouter(prefix="/api/collector", tags=["collector-board"])
    service = CollectorBoard(repository)

    @router.get("/tasks")
    def tasks(status: Literal["all", "DUPLICATE_SKIPPED", "PENDING", "PROCESSING", "AUTO_ACCEPTED", "AUTO_REJECTED", "QUARANTINED", "AUTO_FAILED", "RETRYING", "DOWNSTREAM_REVIEW", "WAITING_REVIEW", "WAITING_RESOURCE", "ACCEPTED_QUEUED", "REJECTED", "COMPLETED", "FAILED", "CANCELLED", "PAUSED", "SKIPPED", "OTHER"] = "all",
              page: int = Query(1, ge=1), page_size: int = Query(10, ge=10, le=50),
              search: str = Query("", max_length=300), sort: Literal["updated_desc", "updated_asc", "created_desc", "created_asc"] = "updated_desc",
              country: str | None = Query(None, max_length=100), project_id: str | None = Query(None, max_length=100), platform: str | None = Query(None, max_length=30)):
        if page_size not in {10, 30, 50}:
            raise HTTPException(422, "每页数量仅支持 10、30 或 50 条")
        return service.list(status=status, page=page, page_size=page_size, search=search, sort=sort,
            country=country, project_id=project_id, platform=platform)

    return router

