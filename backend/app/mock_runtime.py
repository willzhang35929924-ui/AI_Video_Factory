import asyncio
from collections.abc import Awaitable, Callable

from fastapi import HTTPException

from app.domain import audit, error, find, new_id, now, recount_account_queues, task_stage
from app.schemas import ProductionEvent, STAGES


class MockRuntime:
    def __init__(self, repository, hub):
        self.repository = repository
        self.hub = hub
        self.simulations: dict[str, asyncio.Task] = {}

    def assert_idle(self, state: dict, task_id: str) -> None:
        if any(run["task_id"] == task_id and run["status"] in ("queued", "running") for run in state["runs"]):
            raise HTTPException(409, "此任务已有排队或运行中的 Mock 处理")

    async def recover(self) -> None:
        def change(state):
            for run in state["runs"]:
                if run["provider"] == "mock" and run["status"] in ("queued", "running"):
                    self._stop(state, run, "MOCK_INTERRUPTED", "上次 Mock 进程已结束，已安全暂停，可重试")
            recount_account_queues(state)
        self.repository.mutate(change)

    def _stop(self, state: dict, run: dict, code: str, message: str) -> None:
        task = find(state, "tasks", run["task_id"])
        stage = task_stage(state, task["id"], run["stage"])
        run.update(status="failed")
        run["logs"].append("[MOCK] " + message)
        task.update(status="waiting", updated_at=now())
        stage.update(status="pending", error=message, end_time=now())
        error(state, task["id"], run["stage"], code, message)
        audit(state, "processing.paused", run["id"], message, task["id"])
        if run["stage"] == "collector":
            for rule in state["collector"]:
                if rule["country"] == task["country"] and rule["keyword"] == task["keyword"]:
                    rule["status"] = "pending"
        if run["stage"] == "publish":
            for publishing in state["publishing"]:
                if publishing["task_id"] == task["id"] and publishing["status"] in ("queued", "running"):
                    publishing.update(status="pending", error=message)

    async def pause(self, task_id: str) -> dict:
        def change(state):
            find(state, "tasks", task_id)
            runs = [run for run in state["runs"] if run["task_id"] == task_id and run["status"] in ("queued", "running")]
            if not runs:
                raise HTTPException(409, "此任务没有可暂停的 Mock 处理")
            for run in runs:
                self._stop(state, run, "MOCK_PAUSED", "用户暂停了本次 Mock 处理")
            return [run["id"] for run in runs]
        run_ids = self.repository.mutate(change)
        for run_id in run_ids:
            simulation = self.simulations.get(run_id)
            if simulation:
                simulation.cancel()
        await self.hub.broadcast(ProductionEvent(type="status", task_id=task_id, status="waiting", message="Mock 任务已暂停"))
        return {"message": "Mock 任务已暂停，可安全重试", "entity_id": task_id}

    async def enqueue(self, task_id: str, stage_name: str, action: str,
                      work: Callable[[], Awaitable[dict]], complete: Callable[[dict, dict], None],
                      asset_links: dict | None = None, retry: bool = False) -> dict:
        run_id = new_id("run")
        def change(state):
            task = find(state, "tasks", task_id)
            self.assert_idle(state, task_id)
            if task["status"] == "completed":
                raise HTTPException(409, "该任务已完成，不能重复提交")
            stage = task_stage(state, task_id, stage_name)
            stage.update(status="pending", start_time=None, end_time=None, error=None)
            if retry:
                stage["retry_count"] += 1
            task.update(current_stage=stage_name, status="pending", updated_at=now())
            state["runs"].append({"id": run_id, "task_id": task_id, "stage": stage_name, "status": "queued", "progress": 0,
                                  "logs": ["[MOCK] 已进入本地模拟队列", "[MOCK] " + action], "created_at": now()})
            audit(state, "processing.queued", run_id, "已排队 Mock " + stage_name, task_id)
            return state["settings"]["mock_delay_ms"] / 1000
        delay = self.repository.mutate(change)
        await self.hub.broadcast(ProductionEvent(type="status", task_id=task_id, run_id=run_id, stage=stage_name, progress=0, status="queued", message="Mock 已排队"))
        simulation = asyncio.create_task(self._simulate(run_id, task_id, stage_name, delay, work, complete, asset_links))
        self.simulations[run_id] = simulation
        simulation.add_done_callback(lambda _: self.simulations.pop(run_id, None))
        return {"message": "Mock 处理已排队", "entity_id": task_id, "run_id": run_id}

    async def _simulate(self, run_id, task_id, stage_name, delay, work, complete, asset_links):
        try:
            for progress in (10, 45, 80):
                await asyncio.sleep(delay)
                def change(state):
                    run = find(state, "runs", run_id)
                    if run["status"] not in ("queued", "running"):
                        return False
                    task = find(state, "tasks", task_id)
                    stage = task_stage(state, task_id, stage_name)
                    run.update(status="running", progress=progress)
                    run["logs"].append(f"[MOCK] {stage_name} 示例进度 {progress}%")
                    stage.update(status="running", start_time=stage["start_time"] or now())
                    task.update(status="running", progress=(STAGES.index(stage_name) + progress / 100) / len(STAGES) * 100, updated_at=now())
                    return True
                if not self.repository.mutate(change):
                    return
                await self.hub.broadcast(ProductionEvent(type="progress", task_id=task_id, run_id=run_id, stage=stage_name, progress=progress, status="running", message=f"Mock {stage_name} {progress}%"))
            result = await work()
            await asyncio.sleep(delay)
            def finish(state):
                run = find(state, "runs", run_id)
                if run["status"] not in ("queued", "running"):
                    return False
                complete(state, result)
                run.update(status="success", progress=100)
                run["logs"].append("[MOCK] 示例处理成功；未执行真实外部能力")
                audit(state, "processing.success", run_id, "Mock " + stage_name + " 成功", task_id)
                return True
            if self.repository.mutate(finish, asset_links):
                await self.hub.broadcast(ProductionEvent(type="status", task_id=task_id, run_id=run_id, stage=stage_name, progress=100, status="success", message="Mock 处理成功"))
        except asyncio.CancelledError:
            return
        except Exception as exception:
            message = str(exception.detail) if isinstance(exception, HTTPException) else "Mock 处理发生异常：" + str(exception)
            def fail(state):
                run = find(state, "runs", run_id)
                if run["status"] not in ("queued", "running"):
                    return
                task = find(state, "tasks", task_id)
                run.update(status="failed")
                run["logs"].append("[MOCK] " + message)
                task.update(status="failed", updated_at=now())
                task_stage(state, task_id, stage_name).update(status="failed", end_time=now(), error=message)
                if stage_name == "collector":
                    for rule in state["collector"]:
                        if rule["country"] == task["country"] and rule["keyword"] == task["keyword"]:
                            rule["status"] = "failed"
                if stage_name == "publish":
                    for publishing in state["publishing"]:
                        if publishing["task_id"] == task_id and publishing["status"] in ("queued", "running"):
                            publishing.update(status="failed", error=message)
                error(state, task_id, stage_name, "MOCK_PROCESSING_FAILED", message)
                audit(state, "processing.failed", run_id, message, task_id)
                recount_account_queues(state)
            self.repository.mutate(fail)
            await self.hub.broadcast(ProductionEvent(type="error", task_id=task_id, run_id=run_id, stage=stage_name, status="failed", message=message))

    async def shutdown(self, *, recover: bool = True) -> None:
        for simulation in list(self.simulations.values()):
            simulation.cancel()
        await asyncio.gather(*self.simulations.values(), return_exceptions=True)
        if recover:
            await self.recover()

    def assert_reset_idle(self) -> None:
        state = self.repository.state()
        if self.simulations or any(run["status"] in ("queued", "running") for run in state["runs"]):
            raise HTTPException(409, "请先暂停或等待所有 Mock 处理结束，再恢复示例数据")
