from datetime import datetime, timezone
from uuid import uuid4

from fastapi import HTTPException

from app.schemas import STAGES


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}-{uuid4().hex[:8]}"


def find(state: dict, collection: str, entity_id: str | None) -> dict:
    row = next((item for item in state[collection] if item["id"] == entity_id), None)
    if not row:
        raise HTTPException(404, "指定记录不存在")
    return row


def task_stage(state: dict, task_id: str, name: str) -> dict:
    return next(item for item in state["stages"] if item["task_id"] == task_id and item["name"] == name)


def audit(state: dict, action: str, entity_id: str, message: str, task_id: str | None = None) -> None:
    state["audits"].append({"id": new_id("audit"), "task_id": task_id, "action": action, "entity_id": entity_id, "message": message, "created_at": now()})


def error(state: dict, task_id: str, stage: str, code: str, message: str) -> None:
    state["errors"].append({"id": new_id("error"), "task_id": task_id, "stage": stage, "code": code, "message": message, "created_at": now()})


def create_task(state: dict, country: str, keyword: str, product_id: str) -> dict:
    product = find(state, "products", product_id)
    if product["country"] != country:
        raise HTTPException(409, "产品市场与任务国家不一致")
    task_id = new_id("tsk")
    task = {"id": task_id, "project_id": state["projects"][0]["id"], "country": country, "keyword": keyword,
            "product_id": product_id, "source_video_id": None, "current_stage": "collector", "status": "pending",
            "progress": 0, "thumbnail": "/mock/portrait.svg", "created_at": now(), "updated_at": now()}
    state["tasks"].append(task)
    for name in STAGES:
        state["stages"].append({"id": task_id + "-" + name, "task_id": task_id, "name": name, "status": "pending",
                                "start_time": None, "end_time": None, "duration": 0, "retry_count": 0, "error": None})
    audit(state, "task.created", task_id, "新建本地 Mock 生产任务", task_id)
    return task


def succeed_stage(state: dict, task: dict, name: str) -> None:
    stage = task_stage(state, task["id"], name)
    stage["status"] = "success"
    stage["end_time"] = now()
    stage["start_time"] = stage["start_time"] or stage["end_time"]
    stage["duration"] = max(0, (datetime.fromisoformat(stage["end_time"]) - datetime.fromisoformat(stage["start_time"])).total_seconds())
    stage["error"] = None
    task["updated_at"] = now()


def advance_task(state: dict, task: dict, completed_stage: str) -> None:
    succeed_stage(state, task, completed_stage)
    index = STAGES.index(completed_stage)
    task["progress"] = (index + 1) / len(STAGES) * 100
    if index == len(STAGES) - 1:
        task.update(current_stage="publish", status="completed", progress=100)
    else:
        task.update(current_stage=STAGES[index + 1], status="pending")
        next_stage = task_stage(state, task["id"], task["current_stage"])
        next_stage.update(status="pending", error=None)


def latest_asset(state: dict, task_id: str, kind: str) -> dict:
    assets = [item for item in state["assets"] if item["task_id"] == task_id and item["type"] == kind]
    if not assets:
        raise HTTPException(409, {"source": "请先采集原始视频", "person": "请先完成抠像并接受结果", "background": "请先生成背景"}.get(kind, "所需资产尚未创建"))
    asset = max(assets, key=lambda item: (item["created_at"], item["id"]))
    if asset["status"] in ("failed", "rejected") and kind != "source":
        raise HTTPException(409, "关联资产尚未通过当前处理，请先重试")
    return asset


def recount_account_queues(state: dict) -> None:
    for account in state["accounts"]:
        account["queue"] = sum(item["account_id"] == account["id"] and item["status"] in ("pending", "scheduled", "queued", "running") for item in state["publishing"])
