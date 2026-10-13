from pathlib import PurePosixPath

from fastapi import HTTPException
from pydantic import ValidationError

from app.action_schemas import ACTION_SCHEMAS
from app.adapters import get_mock_providers
from app.catalog import CatalogService
from app.domain import advance_task, audit, create_task, error, find, latest_asset, new_id, now, recount_account_queues, succeed_stage, task_stage
from app.mock_runtime import MockRuntime
from app.publishing import PublishingService, add_publishing, publishing_gate
from app.schemas import ActionRequest, ProductionEvent


class FactoryService:
    def __init__(self, repository, hub, storage):
        self.repository = repository
        self.hub = hub
        self.storage = storage
        self.providers = get_mock_providers()
        self.catalog = CatalogService(repository)
        self.publishing = PublishingService(repository)
        self.runtime = MockRuntime(repository, hub)

    async def execute(self, request: ActionRequest) -> dict:
        schema = ACTION_SCHEMAS.get(request.action)
        if not schema:
            raise HTTPException(400, "未知的 Mock 操作")
        try:
            validated = schema.model_validate(request.payload)
        except ValidationError as exception:
            messages = [".".join(str(item) for item in issue["loc"]) + "：" + issue["msg"] for issue in exception.errors()]
            raise HTTPException(422, "参数不符合接口要求；" + "；".join(messages)) from exception
        payload = validated.model_dump(mode="json")
        action, entity_id = request.action, request.entity_id
        global_actions = {"create_task", "save_collector", "collect", "import_collector", "save_publishing", "save_product", "save_settings", "reset_demo"}
        if action not in global_actions and not entity_id:
            raise HTTPException(422, "此操作必须提供 entity_id")
        if action == "create_task":
            result = self.catalog.create(payload)
        elif action == "save_collector":
            result = self.catalog.save_collector(payload["rows"])
        elif action == "import_collector":
            result = self.catalog.import_collector(payload)
        elif action == "save_product":
            result = self.catalog.save_product(payload)
        elif action == "save_settings":
            result = self.catalog.settings(payload)
        elif action in ("authorize_account", "update_account_status"):
            result = self.catalog.account(entity_id, "authorized" if action == "authorize_account" else payload["status"])
        elif action == "save_publishing":
            result = self.publishing.save(payload["rows"])
        elif action == "generate_caption":
            result = self.publishing.caption(entity_id)
        elif action == "schedule_publish":
            result = self.publishing.schedule(entity_id, validated.schedule)
        elif action == "pause_task":
            return await self.runtime.pause(entity_id)
        elif action == "reset_demo":
            self.runtime.assert_reset_idle()
            self.repository.reset()
            await self.runtime.recover()
            self.repository.mutate(lambda state: audit(state, "system.reset", "system", "已恢复本地 Mock 示例数据"))
            result = {"message": "本地 Mock 示例已恢复；现有业务项目未改变"}
        elif action == "collect":
            return await self.collect(payload["row_ids"])
        elif action in ("start_cutout", "retry_interval", "regenerate_mask", "manual_fix"):
            return await self.cutout(entity_id, action, payload)
        elif action == "accept_cutout":
            result = self.accept_cutout(entity_id)
        elif action == "generate_prompt":
            result = await self.generate_prompt(entity_id, payload)
        elif action in ("generate_background", "upload_background"):
            return await self.background(entity_id, payload, action == "upload_background")
        elif action in ("compose", "update_composition"):
            settings = self.repository.state()["settings"]
            for key in ("width", "height", "fps"):
                if key not in validated.model_fields_set:
                    payload[key] = settings["output_" + key]
            return await self.compose(entity_id, payload)
        elif action.startswith("review_"):
            return await self.review(entity_id, action, payload)
        elif action == "publish_now":
            return await self.publish(entity_id)
        elif action == "retry_task":
            return await self.retry(entity_id)
        else:
            raise HTTPException(400, "未注册的 Mock 操作")
        await self.hub.broadcast(ProductionEvent(type="status", task_id=result.get("entity_id") if action in ("create_task", "accept_cutout", "generate_prompt") else None, message=result["message"]))
        return result

    def _context(self, task_id: str) -> tuple[dict, dict]:
        state = self.repository.state()
        task = find(state, "tasks", task_id)
        self.runtime.assert_idle(state, task_id)
        if task["status"] == "completed":
            raise HTTPException(409, "当前任务已完成，不能重复处理")
        return state, task

    def _asset(self, state: dict, task: dict, kind: str, info: dict, asset_id: str) -> dict:
        filename = info.get("filename") or asset_id + (".png" if kind == "background" else ".mov" if kind == "person" else ".mp4")
        try:
            path = self.storage.asset_path(task["project_id"], task["id"], "logs" if kind == "exception" else kind, filename)
        except ValueError as exception:
            raise HTTPException(422, str(exception)) from exception
        asset = {"id": asset_id, "task_id": task["id"], "type": kind, "name": filename,
                 "thumbnail": info.get("thumbnail", "/mock/portrait.svg"), "duration": info.get("duration", 0),
                 "width": info.get("width", 1080), "height": info.get("height", 1920), "fps": info.get("fps", 0),
                 "codec": info.get("codec", "png" if kind == "background" else "h264"), "country": task["country"],
                 "keyword": task["keyword"], "status": "completed", "storage_path": path, "created_at": now()}
        state["assets"].append(asset)
        return asset

    async def collect(self, row_ids: list[str]) -> dict:
        state = self.repository.state()
        rows = [find(state, "collector", row_id) for row_id in row_ids] if row_ids else state["collector"]
        if not rows:
            raise HTTPException(409, "请先添加采集规则")
        if len({row["id"] for row in rows}) != len(rows):
            raise HTTPException(422, "采集行ID不能重复")
        for row in rows:
            product = find(state, "products", row["product_id"])
            if product["country"] != row["country"]:
                raise HTTPException(409, "采集规则与产品市场不一致")
            related = [task for task in state["tasks"] if task["country"] == row["country"] and task["keyword"] == row["keyword"] and task["current_stage"] == "collector"]
            for task in related:
                self.runtime.assert_idle(state, task["id"])
        results = []
        for row in rows:
            task = self.repository.mutate(lambda current: create_task(current, row["country"], row["keyword"], row["product_id"]))
            results.append(await self._collect_task(task["id"], row))
        return {"message": f"{len(results)} 条 Mock 采集任务已排队", "entity_id": results[0]["entity_id"], "run_id": results[0]["run_id"]}

    async def _collect_task(self, task_id: str, rule: dict, retry: bool = False) -> dict:
        state, task = self._context(task_id)
        product = find(state, "products", task["product_id"])
        source_id, asset_id = new_id("source"), new_id("ast")
        links = {asset_id: {}}
        async def work():
            result = await self.providers.collector.collect(task, rule)
            views, likes = result["views"], result["likes"]
            ratio = likes / views * 100 if views > 0 else 0
            if views < rule["min_views"] or likes < rule["min_likes"] or ratio < rule["min_ratio"]:
                raise HTTPException(409, f"Mock 指标未通过：views={views} / 门槛={rule['min_views']}；likes={likes} / 门槛={rule['min_likes']}；ratio={ratio:.2f}% / 门槛={rule['min_ratio']}%")
            result["analysis"] = await self.providers.analyzer.analyze(task, product)
            analysis = result["analysis"]
            if analysis["product_match_score"] < rule["match_threshold"]:
                raise HTTPException(409, "Mock 产品匹配分数未达到筛选门槛")
            if any(rule["filter_" + key] and analysis[key + "_detected"] for key in ("overlay", "watermark", "subtitle")):
                raise HTTPException(409, "Mock 遮挡或水印筛选未通过")
            links[asset_id]["analysis"] = {key: analysis[key] for key in ("ai_match_score", "product_match_score", "overlay_detected", "watermark_detected")}
            links[asset_id].update(views=views, likes=likes)
            return result
        def complete(current, result):
            task_now = find(current, "tasks", task_id)
            info = {**result["source_video"], "filename": result["source_video"]["original_filename"]}
            self._asset(current, task_now, "source", info, asset_id)
            task_now["source_video_id"] = source_id
            rule_now = next((row for row in current["collector"] if row["id"] == rule["id"]), None)
            if rule_now:
                rule_now.update(views=result["views"], likes=result["likes"], status="success")
            succeed_stage(current, task_now, "collector")
            advance_task(current, task_now, "analysis")
            audit(current, "analysis.success", task_id, "Mock 人物与产品匹配、遮挡/水印筛选通过", task_id)
        result = await self.runtime.enqueue(task_id, "collector", "Mock 视频采集与指标分析", work, complete, links, retry=retry)
        def mark_running(current):
            row = next((row for row in current["collector"] if row["id"] == rule["id"]), None)
            if row:
                row["status"] = "running"
        self.repository.mutate(mark_running)
        return result

    async def cutout(self, task_id: str, action: str, interval: dict) -> dict:
        state, task = self._context(task_id)
        source = latest_asset(state, task_id, "source")
        if interval.get("end") is not None and interval["end"] > source["duration"]:
            raise HTTPException(422, "异常区间不能超出原片时长")
        asset_id = new_id("ast")
        links = {asset_id: {}}
        async def work():
            result = await self.providers.comfyui.start_cutout(task, source)
            links[asset_id]["cutout"] = result
            return result
        def complete(current, result):
            task_now = find(current, "tasks", task_id)
            self._asset(current, task_now, "person", result, asset_id)
            succeed_stage(current, task_now, "cutout")
            task_stage(current, task_id, "person").update(status="waiting_review")
            task_now.update(current_stage="person", status="review", progress=50, updated_at=now())
            audit(current, "cutout.result_created", asset_id, "Mock 纯人像资产已创建，等待接受结果" + (f"；区间={interval}" if interval else ""), task_id)
        return await self.runtime.enqueue(task_id, "cutout", action, work, complete, links, retry=action != "start_cutout")

    def accept_cutout(self, task_id: str) -> dict:
        def change(state):
            task = find(state, "tasks", task_id)
            self.runtime.assert_idle(state, task_id)
            if task["status"] == "completed":
                raise HTTPException(409, "该任务已经完成")
            latest_asset(state, task_id, "person")
            succeed_stage(state, task, "cutout")
            advance_task(state, task, "person")
            audit(state, "cutout.accepted", task_id, "Mock 人物抠像结果已接受", task_id)
        self.repository.mutate(change)
        return {"message": "抠像结果已接受，进入背景阶段", "entity_id": task_id}

    async def generate_prompt(self, task_id: str, options: dict) -> dict:
        state, task = self._context(task_id)
        product = find(state, "products", task["product_id"])
        prompt = await self.providers.background.generate_prompt(task, {**options, "product_name": product["name"]})
        prompt.update(id=new_id("prompt"), created_at=now())
        def change(current):
            current["prompts"].append(prompt)
            audit(current, "prompt.generated", prompt["id"], "按国家、产品与场景配置生成 Mock 提示词", task_id)
        self.repository.mutate(change)
        return {"message": "Mock 正负提示词已生成", "entity_id": task_id, "prompt_id": prompt["id"]}

    async def background(self, task_id: str, options: dict, upload: bool = False) -> dict:
        state, task = self._context(task_id)
        latest_asset(state, task_id, "person")
        prompts = [prompt for prompt in state["prompts"] if prompt["task_id"] == task_id]
        prompt = find(state, "prompts", options["prompt_id"]) if options.get("prompt_id") else prompts[-1] if prompts else None
        if not upload and not prompt:
            raise HTTPException(409, "请先生成背景提示词")
        if prompt and prompt["task_id"] != task_id:
            raise HTTPException(409, "提示词不属于当前任务")
        filename = options.get("filename")
        if upload and (PurePosixPath(filename).name != filename or "\\" in filename or not filename.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))):
            raise HTTPException(422, "请提供 PNG/JPEG/WebP 图片文件名，不接受路径")
        provider = "manual" if upload else options["provider"]
        asset_id = new_id("ast")
        links = {asset_id: {"prompt_id": prompt["id"] if prompt and not upload else None, "provider": provider}}
        async def work():
            if upload:
                return {"filename": filename, "width": 1080, "height": 1920, "fps": 0, "duration": 0, "codec": "png", "thumbnail": "/mock/room.svg"}
            return await self.providers.background.generate_image(task, prompt, provider)
        def complete(current, result):
            task_now = find(current, "tasks", task_id)
            self._asset(current, task_now, "background", result, asset_id)
            advance_task(current, task_now, "background")
            audit(current, "background.created", asset_id, "Mock 背景已创建，未调用浏览器或图像模型", task_id)
        return await self.runtime.enqueue(task_id, "background", "添加 Mock 背景" if upload else "生成 Mock 背景", work, complete, links)

    async def compose(self, task_id: str, options: dict) -> dict:
        state, task = self._context(task_id)
        person, background = latest_asset(state, task_id, "person"), latest_asset(state, task_id, "background")
        asset_id = new_id("ast")
        links = {asset_id: {"parameters": options, "person_asset_id": person["id"], "background_id": "background-" + background["id"]}}
        async def work():
            return await self.providers.processor.compose(task, person, background, options)
        def complete(current, result):
            task_now = find(current, "tasks", task_id)
            asset = self._asset(current, task_now, "final", result, asset_id)
            succeed_stage(current, task_now, "compose")
            review = {"id": new_id("review"), "task_id": task_id, "asset_id": asset["id"], "status": "pending", "issues": [], "note": "Mock 成片已生成，未处理实际媒体。"}
            current["reviews"].append(review)
            if current["settings"]["review_required"]:
                task_now.update(current_stage="review", status="review", progress=87.5, updated_at=now())
                task_stage(current, task_id, "review").update(status="waiting_review")
            else:
                review.update(status="approved", note="按Mock设置自动批准；保留独立审核记录。")
                asset["status"] = "approved"
                advance_task(current, task_now, "review")
                add_publishing(current, task_now, asset)
                audit(current, "review.auto_approved", review["id"], "按Mock设置自动批准该成片", task_id)
            audit(current, "composition.created", asset_id, "Mock 合成成功，实际参数与人物/背景关联已保存", task_id)
        return await self.runtime.enqueue(task_id, "compose", "Mock 合成与调色", work, complete, links)

    async def review(self, review_id: str, action: str, payload: dict) -> dict:
        state = self.repository.state()
        review = find(state, "reviews", review_id)
        task = find(state, "tasks", review["task_id"])
        self.runtime.assert_idle(state, task["id"])
        if action == "review_retry":
            product = find(state, "products", task["product_id"])
            async def work():
                return await self.providers.analyzer.analyze(task, product)
            def complete(current, result):
                task_now = find(current, "tasks", task["id"])
                review_now = find(current, "reviews", review_id)
                review_now.update(status="pending", issues=[], note=payload["note"] or "Mock 复核完成，等待确认")
                find(current, "assets", review_now["asset_id"])["status"] = "review"
                task_now.update(current_stage="review", status="review", updated_at=now())
                task_stage(current, task["id"], "review").update(status="waiting_review")
            return await self.runtime.enqueue(task["id"], "review", "Mock 画面复核重试", work, complete, retry=True)
        def change(current):
            review_now = find(current, "reviews", review_id)
            task_now = find(current, "tasks", review["task_id"])
            asset = find(current, "assets", review_now["asset_id"])
            if review_now["status"] in ("approved", "rejected"):
                raise HTTPException(409, "此审核记录已处理，如需重新审核请先重试")
            review_now["note"] = payload["note"]
            if action == "review_approve":
                review_now["status"] = "approved"
                asset["status"] = "approved"
                advance_task(current, task_now, "review")
                add_publishing(current, task_now, asset)
            elif action == "review_reject":
                review_now["status"] = "rejected"
                asset["status"] = "rejected"
                task_now.update(current_stage="review", status="failed", updated_at=now())
                task_stage(current, task_now["id"], "review").update(status="failed", error=payload["note"] or "Mock 人工审核拒绝")
                error(current, task_now["id"], "review", "REVIEW_REJECTED", payload["note"] or "Mock 人工审核拒绝")
            else:
                review_now["status"] = "manual_fix"
                task_now.update(current_stage="review", status="review", updated_at=now())
                task_stage(current, task_now["id"], "review").update(status="waiting_review")
            audit(current, "review." + action.removeprefix("review_"), review_id, payload["note"] or "Mock 审核状态已更新", task_now["id"])
        self.repository.mutate(change)
        await self.hub.broadcast(ProductionEvent(type="status", task_id=task["id"], stage="review", message="Mock 审核结果已保存"))
        return {"message": "审核结果已保存", "entity_id": review_id}

    async def publish(self, publishing_id: str) -> dict:
        task, account, row = self.publishing.gate(publishing_id)
        async def work():
            publishing_gate(self.repository.state(), find(self.repository.state(), "publishing", publishing_id))
            return await self.providers.publishing.publish(task, account, row)
        def complete(current, result):
            row_now = find(current, "publishing", publishing_id)
            publishing_gate(current, row_now)
            row_now.update(status="published", error=None)
            account_now = find(current, "accounts", row_now["account_id"])
            account_now["last_publish"] = now()
            advance_task(current, find(current, "tasks", row_now["task_id"]), "publish")
            audit(current, "publishing.published", publishing_id, "Mock 发布成功，回执=" + result["receipt_id"] + "；未向TikTok发送请求", task["id"])
            recount_account_queues(current)
        result = await self.runtime.enqueue(task["id"], "publish", "Mock 发布", work, complete)
        self.repository.mutate(lambda current: find(current, "publishing", publishing_id).update(status="queued", error=None))
        return result

    async def retry(self, task_id: str) -> dict:
        state, task = self._context(task_id)
        stage = task["current_stage"]
        if stage == "collector":
            rule = next((row for row in state["collector"] if row["country"] == task["country"] and row["keyword"] == task["keyword"]), None)
            if not rule:
                rule = {"id": "", "country": task["country"], "keyword": task["keyword"], "product_id": task["product_id"], "views": 0, "likes": 0,
                        "min_views": 100000, "min_likes": 1000, "min_ratio": 1.2, "match_threshold": 0.85, "filter_overlay": True, "filter_watermark": True, "filter_subtitle": False}
            return await self._collect_task(task_id, rule, retry=True)
        if stage in ("cutout", "person"):
            return await self.cutout(task_id, "retry_interval", {})
        if stage == "background":
            return await self.background(task_id, {"provider": state["settings"]["default_provider"]})
        if stage == "compose":
            return await self.compose(task_id, {"exposure": 0, "contrast": 1, "saturation": 1, "crop": "center", "width": state["settings"]["output_width"], "height": state["settings"]["output_height"], "fps": state["settings"]["output_fps"]})
        if stage == "review":
            reviews = [review for review in state["reviews"] if review["task_id"] == task_id]
            if not reviews:
                raise HTTPException(409, "当前任务尚未生成成片，请先合成")
            return await self.review(reviews[-1]["id"], "review_retry", {"note": ""})
        if stage == "publish":
            rows = [row for row in state["publishing"] if row["task_id"] == task_id and row["status"] != "published"]
            if not rows:
                raise HTTPException(409, "请先审核成片并创建发布草稿")
            return await self.publish(rows[-1]["id"])
        if stage == "analysis":
            source = latest_asset(state, task_id, "source")
            product = find(state, "products", task["product_id"])
            links = {source["id"]: {}}
            async def work():
                result = await self.providers.analyzer.analyze(task, product)
                links[source["id"]]["analysis"] = {key: result[key] for key in ("ai_match_score", "product_match_score", "overlay_detected", "watermark_detected")}
                return result
            def complete(current, result):
                advance_task(current, find(current, "tasks", task_id), "analysis")
            return await self.runtime.enqueue(task_id, "analysis", "Mock 分析重试", work, complete, links, retry=True)
        raise HTTPException(409, "当前步骤没有可重试的处理")
