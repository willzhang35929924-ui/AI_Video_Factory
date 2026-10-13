from copy import deepcopy

from fastapi import HTTPException

from app.domain import audit, create_task, find, new_id, recount_account_queues


class CatalogService:
    def __init__(self, repository):
        self.repository = repository

    def create(self, payload: dict) -> dict:
        task = self.repository.mutate(lambda state: create_task(state, payload["country"].strip(), payload["keyword"].strip(), payload["product_id"]))
        return {"message": "Mock 生产任务已创建", "entity_id": task["id"]}

    def save_collector(self, rows: list[dict]) -> dict:
        if len({row["id"] for row in rows}) != len(rows):
            raise HTTPException(422, "采集规则ID不能重复")
        def change(state):
            existing = {item["id"]: item for item in state["collector"]}
            normalized = []
            for item in rows:
                if not item["country"].strip() or not item["keyword"].strip():
                    raise HTTPException(422, "国家与关键词不能为空")
                if item["product_id"]:
                    product = find(state, "products", item["product_id"])
                    if product["country"] != item["country"]:
                        raise HTTPException(409, "采集规则国家与产品市场不一致")
                previous = existing.get(item["id"])
                row = item.copy()
                row.update({key: previous[key] if previous else default for key, default in (("views", 0), ("likes", 0), ("status", "draft"))})
                normalized.append(row)
            state["collector"] = normalized
            audit(state, "collector.saved", "collector", "已保存采集规则，播放/点赞/状态由后端维护")
        self.repository.mutate(change)
        return {"message": "规则已保存；播放量、点赞量及比例由系统维护"}

    def import_collector(self, payload: dict) -> dict:
        source = payload["source"]
        if source == "tencent" and not (payload.get("url") or "").startswith("https://docs.qq.com/"):
            raise HTTPException(422, "请输入腾讯文档 HTTPS 示例链接")
        if source == "excel" and not payload.get("filename"):
            raise HTTPException(422, "请选择 Excel 示例文件")
        lines = list(dict.fromkeys(line.strip() for line in (payload.get("lines") or "").splitlines() if line.strip()))
        if source == "batch" and not lines:
            raise HTTPException(422, "请输入至少一个关键词")
        if len(lines) > 100:
            raise HTTPException(422, "一次最多添加100条 Mock 关键词")
        def change(state):
            seed = self.repository.seed()["collector"]
            additions = [deepcopy(seed[0]) for _ in lines] if source == "batch" else deepcopy(seed[:2])
            for index, row in enumerate(additions):
                row.update(id=new_id("col"), views=0, likes=0, status="draft")
                if source == "batch":
                    row["keyword"] = lines[index]
                state["collector"].append(row)
            audit(state, "collector.imported", "collector", f"Mock {source} 导入 {len(additions)} 条规则；没有请求云文档或读取Excel内容")
            return len(additions)
        count = self.repository.mutate(change)
        return {"message": f"已添加 {count} 条 Mock 规则，请核对国家与产品；未读取云文档或文件内容"}

    def save_product(self, payload: dict) -> dict:
        def change(state):
            if payload["status"] == "active" and not payload["binding_reference"].strip():
                raise HTTPException(422, "启用产品需要商品绑定标识")
            if not payload["country"].strip() or not payload["name"].strip():
                raise HTTPException(422, "产品名称与国家不能为空")
            current = next((item for item in state["products"] if item["id"] == payload["id"]), None)
            linked = [task for task in state["tasks"] if task["product_id"] == payload["id"]]
            if any(task["country"] != payload["country"] for task in linked):
                raise HTTPException(409, "关联任务存在，请创建独立市场产品而非修改已有产品国家")
            if current:
                current.update(payload)
            else:
                state["products"].append(payload)
            audit(state, "product.saved", payload["id"], "已保存本地产品资料")
        self.repository.mutate(change)
        return {"message": "产品资料已保存", "entity_id": payload["id"]}

    def account(self, account_id: str, status: str) -> dict:
        def change(state):
            account = find(state, "accounts", account_id)
            account.update(status=status, authorization="本地 Mock 授权有效" if status == "authorized" else "本地 Mock 授权状态：" + status)
            if status == "authorized":
                account["errors"] = []
            elif status == "error":
                account["errors"] = ["MOCK_ACCOUNT_ERROR：模拟账号连接异常"]
            audit(state, "account.authorization_changed", account_id, "Mock 账号状态更新为 " + status + "；未保存密码、Cookie或Token")
            recount_account_queues(state)
        self.repository.mutate(change)
        return {"message": "Mock 账号状态已更新", "entity_id": account_id}

    def settings(self, payload: dict) -> dict:
        def change(state):
            state["settings"] = payload
            audit(state, "settings.saved", "settings", "本地 Mock 系统设置已保存")
        self.repository.mutate(change)
        return {"message": "设置已保存，下次 Mock 处理使用新配置"}
