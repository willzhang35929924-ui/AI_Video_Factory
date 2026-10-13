import asyncio
import copy
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from fastapi import HTTPException

from app.integrations.production_readiness import ProductionReadiness, STAGE_REQUIREMENTS
from app.integrations.production_service import ProductionService, TaskStopped
from test_real_review import MemoryRepository, ReviewFixture


class StageOnlyTests(ReviewFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.prepare("person")
        self.production.collector = SimpleNamespace(queue=AsyncMock(side_effect=AssertionError("no search")))
        self.production._saved_context = AsyncMock(return_value=({}, {"task_id": "unit-task", "source_video": "saved-source"}))
        self.production._video_stage = AsyncMock(return_value={"status": "READY"})
        self.production._asset = Mock(return_value=self.repository.state()["assets"][0])
        self.production._stage_status = Mock()
        self.production._quality = AsyncMock(return_value={"status": "PASS"})

    async def execute_stage(self, name, **extra):
        def queued(state):
            state["tasks"][0]["context"]["production"].update(requested_stage=name, **extra)
            state["batches"].append({"id": "isolated-batch", "status": "RUNNING", "task_ids": ["unit-task"],
                "items": [{"task_id": "unit-task", "status": "RUNNING", "stage": name}], "active_task_id": "unit-task"})
        self.repository.mutate(queued)
        self.production.cancels["isolated-batch"] = threading.Event()
        await self.production._task("isolated-batch", "unit-task")

    async def test_cutout_only_registers_person_quality_and_returns_before_paid_stages(self):
        self.production._asset.side_effect = [None, self.original_asset]
        await self.execute_stage("cutout")
        self.assertEqual([call.args[2] for call in self.production._video_stage.await_args_list], ["cutout"])
        self.production._quality.assert_awaited_once_with("isolated-batch", "unit-task", self.original_asset)
        self.production.collector.queue.assert_not_awaited()
        self.assertEqual(self.repository.state()["tasks"][0]["status"], "WAITING_REVIEW")

    async def test_collector_only_accepted_result_stops_before_cutout_and_paid_stages(self):
        self.production._checkpoint = AsyncMock()
        self.repository.mutate(lambda state: state["tasks"][0]["context"]["task_context"].update(video_id="unit-video"))
        await self.execute_stage("collector", previous_status="CREATED", retry=False)
        self.assertEqual(self.repository.state()["tasks"][0]["status"], "COLLECTED")
        self.production._checkpoint.assert_awaited_once_with("isolated-batch")
        self.production._video_stage.assert_not_awaited()
        self.production._quality.assert_not_awaited()
        self.production.collector.queue.assert_not_awaited()

    async def test_discovered_children_inherit_collector_only_boundary(self):
        def setup(state):
            parent = state["tasks"][0]
            parent["context"]["production"].update(requested_stage="collector", batch_id="isolated-batch", retry=False)
            child = copy.deepcopy(parent)
            child.update(id="unit-child", status="CREATED")
            child["context"]["production"] = {}
            state["tasks"].append(child)
            state["batches"].append({"id": "isolated-batch", "task_ids": ["unit-task"], "items": []})
        self.repository.mutate(setup)
        self.production._append_discovered("isolated-batch", "unit-task", ["unit-task", "unit-child"])
        state = self.repository.state()
        child = next(task for task in state["tasks"] if task["id"] == "unit-child")
        self.assertEqual(child["context"]["production"]["requested_stage"], "collector")
        self.assertEqual(child["status"], "QUEUED")
        self.assertEqual(state["batches"][0]["task_ids"], ["unit-task", "unit-child"])

    async def test_prompt_only_does_not_run_cutout_background_or_compose(self):
        await self.execute_stage("prompt")
        self.assertEqual([call.args[2] for call in self.production._video_stage.await_args_list], ["prompt"])
        self.production._quality.assert_not_awaited()
        self.production.collector.queue.assert_not_awaited()

    async def test_quality_only_reads_saved_person_without_other_providers(self):
        await self.execute_stage("quality")
        self.production._video_stage.assert_not_awaited()
        self.production._quality.assert_awaited_once()

    async def test_background_requires_explicit_prompt_before_queuing(self):
        with self.assertRaises(HTTPException) as error:
            await self.production.start(["unit-task"], retry=True, stage="background")
        self.assertEqual(error.exception.status_code, 422)
        self.assertEqual(self.repository.state()["batches"], [])

    async def test_missing_required_dependency_cannot_fake_ready(self):
        self.production.readiness.check = AsyncMock(return_value={"ready": True, "checks": [{"name": "ComfyUI", "status": "READY"}]})
        with self.assertRaises(HTTPException) as error:
            await self.production.start(["unit-task"], retry=True, stage="cutout")
        self.assertEqual(error.exception.detail["code"], "PRODUCTION_NOT_READY")

    async def test_rejected_review_stops_current_task_at_real_stage_boundary(self):
        self.production._checkpoint = AsyncMock()
        self.repository.mutate(lambda state: state["tasks"][0]["context"]["production"].update(rejected_review={"review_id": "unit-rejected"}))
        with self.assertRaises(TaskStopped):
            await self.production._task_checkpoint("isolated-batch", "unit-task")

    async def test_explicit_local_retry_preserves_rejection_history_and_clears_current_block(self):
        self.repository.mutate(lambda state: state["tasks"][0]["context"]["production"].update(rejected_review={"review_id": "unit-rejected"}))
        await self.production.start(["unit-task"], retry=True, stage="cutout")
        production = self.repository.state()["tasks"][0]["context"]["production"]
        self.assertNotIn("rejected_review", production)
        self.assertEqual(production["rejected_review_history"], [{"review_id": "unit-rejected"}])


class StageReadinessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.collector = SimpleNamespace(rules=Mock(return_value={"sha256": "unit-rule"}), dry_run=Mock(return_value={"status": "MISSING", "checks": [{"name": "product_images", "ok": False}]}))
        self.vidbee = SimpleNamespace(readiness=AsyncMock(return_value={"status": "READY"}))
        self.video = SimpleNamespace(assets=SimpleNamespace(readiness=AsyncMock(return_value={"metadata": {"checks": [
            {"name": name, "status": "READY"} for name in ("ComfyUI", "Person Asset", "Configuration")]}})))
        self.prompt = SimpleNamespace(readiness=AsyncMock(return_value={"metadata": {"checks": []}}))
        settings = SimpleNamespace(storage_root=self.root, app_mode="integration", integration_dry_run=False)
        self.service = ProductionReadiness(settings, self.collector, self.vidbee, self.video, self.prompt)
        self.service._quality = Mock(return_value={"status": "READY"})
        self.service._tiktok = Mock(return_value={"status": "READY"})

    async def test_cutout_checks_actual_local_dependencies_without_browser_or_paid_probes(self):
        value = await self.service.check(True, stage="cutout", task_id="unit-task")
        self.assertTrue(value["ready"])
        self.assertEqual({row["name"] for row in value["checks"] if row["required"]}, STAGE_REQUIREMENTS["cutout"])
        self.video.assets.readiness.assert_awaited_once()
        self.vidbee.readiness.assert_not_awaited()
        self.prompt.readiness.assert_not_awaited()
        self.service._tiktok.assert_not_called()
        self.collector.dry_run.assert_not_called()
        self.assertEqual(next(row["status"] for row in value["checks"] if row["name"] == "Execution"), "READY")

    async def test_collector_input_dry_run_is_required_and_scoped_per_task(self):
        value = await self.service.check(True, stage="collector", task_id="unit-task")
        self.assertFalse(value["ready"])
        self.collector.dry_run.assert_called_once_with("unit-task")
        self.assertEqual(next(row["status"] for row in value["checks"] if row["name"] == "Collector"), "MISSING")
        self.video.assets.readiness.assert_not_awaited()
        self.prompt.readiness.assert_not_awaited()

    async def test_cache_does_not_reuse_local_ready_as_background_ready(self):
        self.assertTrue((await self.service.check(stage="cutout"))["ready"])
        value = await self.service.check(stage="background")
        self.assertFalse(value["ready"])
        self.prompt.readiness.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
