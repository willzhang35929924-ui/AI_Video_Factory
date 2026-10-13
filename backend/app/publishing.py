from datetime import datetime, timezone

from fastapi import HTTPException

from app.domain import audit, error, find, new_id, now, recount_account_queues


def publishing_gate(state: dict, row: dict) -> tuple[dict, dict, dict]:
    task = find(state, "tasks", row["task_id"])
    account = find(state, "accounts", row["account_id"])
    product = find(state, "products", row["product_id"])
    asset = find(state, "assets", row["video_id"])
    if row["status"] == "published":
        raise HTTPException(409, "该发布记录已完成，不允许重复发布")
    if account["status"] != "authorized":
        raise HTTPException(409, "账号尚未获得有效的 Mock 授权")
    if not (task["country"] == account["country"] == product["country"] == row["country"]):
        raise HTTPException(409, "任务、账号、产品与发布国家必须一致")
    if product["id"] != task["product_id"] or product["id"] not in account["product_ids"]:
        raise HTTPException(409, "发布产品未绑定到当前任务与账号")
    if product["status"] != "active" or not product["binding_reference"] or row["product_binding"] != product["binding_reference"]:
        raise HTTPException(409, "商品绑定标识与当前启用产品不一致")
    if asset["task_id"] != task["id"] or asset["type"] != "final":
        raise HTTPException(409, "发布视频不是当前任务的最终成片")
    if not any(review["task_id"] == task["id"] and review["asset_id"] == asset["id"] and review["status"] == "approved" for review in state["reviews"]):
        raise HTTPException(409, "当前成片尚无对应的审核批准记录")
    if not row["caption"].strip():
        raise HTTPException(409, "请先填写发布文案")
    return task, account, product


def add_publishing(state: dict, task: dict, asset: dict) -> dict:
    existing = next((item for item in state["publishing"] if item["video_id"] == asset["id"]), None)
    if existing:
        return existing
    product = find(state, "products", task["product_id"])
    account = next((item for item in state["accounts"] if item["country"] == task["country"] and product["id"] in item["product_ids"]), None)
    row = {"id": new_id("pub"), "task_id": task["id"], "video_id": asset["id"], "account_id": account["id"] if account else "",
           "country": task["country"], "product_id": product["id"], "caption": "", "product_binding": product["binding_reference"],
           "schedule": None, "status": "pending", "error": None}
    state["publishing"].append(row)
    audit(state, "publishing.created", row["id"], "成片通过审核，创建可编辑的 Mock 发布草稿", task["id"])
    recount_account_queues(state)
    return row


class PublishingService:
    def __init__(self, repository):
        self.repository = repository

    def save(self, rows: list[dict]) -> dict:
        def change(state):
            for incoming in rows:
                current = find(state, "publishing", incoming["id"])
                if current["status"] == "published":
                    if incoming != current:
                        raise HTTPException(409, "历史已发布记录不可修改")
                    continue
                if current["status"] in ("queued", "running"):
                    raise HTTPException(409, "当前 Mock 发布正在处理，不能修改资料")
                for key in ("task_id", "video_id", "country", "status", "error"):
                    if incoming[key] != current[key]:
                        raise HTTPException(409, "不可通过保存发布资料修改任务、视频或执行状态")
                if incoming["account_id"]:
                    find(state, "accounts", incoming["account_id"])
                find(state, "products", incoming["product_id"])
                current.update({key: incoming[key] for key in ("account_id", "product_id", "caption", "product_binding", "schedule")})
                audit(state, "publishing.saved", current["id"], "已保存 Mock 发布草稿", current["task_id"])
            recount_account_queues(state)
        self.repository.mutate(change)
        return {"message": "发布草稿已保存"}

    def caption(self, publishing_id: str) -> dict:
        def change(state):
            row = find(state, "publishing", publishing_id)
            if row["status"] in ("published", "queued", "running"):
                raise HTTPException(409, "当前发布记录不可编辑")
            task = find(state, "tasks", row["task_id"])
            row["caption"] = f"{task['keyword']} · 为{task['country']}的日常生活带来灵感。 #ProductStory #MockVideo"
            audit(state, "publishing.caption_generated", publishing_id, "Mock 文案已生成，未调用大模型", task["id"])
        self.repository.mutate(change)
        return {"message": "Mock 文案已生成", "entity_id": publishing_id}

    def gate(self, publishing_id: str) -> tuple[dict, dict, dict]:
        state = self.repository.state()
        row = find(state, "publishing", publishing_id)
        try:
            task, account, _ = publishing_gate(state, row)
        except HTTPException as exception:
            def record_failure(current):
                row_now = find(current, "publishing", publishing_id)
                if row_now["status"] != "published":
                    row_now["error"] = str(exception.detail)
                error(current, row_now["task_id"], "publish", "PUBLISH_GATE_BLOCKED", str(exception.detail))
                audit(current, "publishing.blocked", publishing_id, str(exception.detail), row_now["task_id"])
            self.repository.mutate(record_failure)
            raise
        return task, account, row

    def schedule(self, publishing_id: str, schedule: datetime | None) -> dict:
        self.gate(publishing_id)
        if schedule is None or schedule.replace(tzinfo=schedule.tzinfo or timezone.utc) <= datetime.now(timezone.utc):
            raise HTTPException(409, "请选择未来的 Mock 排期时间")
        def change(state):
            row = find(state, "publishing", publishing_id)
            publishing_gate(state, row)
            if row["status"] in ("queued", "running"):
                raise HTTPException(409, "当前发布正在模拟处理中")
            row.update(schedule=schedule.isoformat(), status="scheduled", error=None)
            audit(state, "publishing.scheduled", publishing_id, "保存 Mock 排期；本阶段不运行真实定时发布 Worker", row["task_id"])
            recount_account_queues(state)
        self.repository.mutate(change)
        return {"message": "已保存 Mock 排期，可点击模拟发布验证结果", "entity_id": publishing_id}
