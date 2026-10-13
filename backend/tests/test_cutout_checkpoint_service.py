"""Factory queue contracts for partial-cutout restoration; isolated fixtures only."""

import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from test_real_review import ReviewFixture


class CutoutCheckpointServiceTests(ReviewFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.prepare()
        self.source = {"id": "unit-source", "sha256": "a" * 64}
        self.context = {"task_id": "unit-task", "project_id": "unit-project", "country": "MX", "keyword": "body",
            "product_id": "unit-product", "context_id": "unit-context", "source_document": "unit-source",
            "source_row": 2, "video_id": "123", "source_video": str(self.production.settings.storage_root / "source.mp4")}
        self.plan = {"task_id": "unit-task", "context_id": "unit-context", "generation": 1,
            "source_sha256": self.source["sha256"], "input_fingerprint": "b" * 64, "checkpoint_fingerprint": "c" * 64,
            "completed_frames": 56, "receipt_lost_frames": 8, "total_frames": 683,
            "business_executed": False, "paid_requests": 0, "comfy_submissions": 0,
            "chunks": [{"start_frame": 0, "frame_count": 8, "status": "COMPLETED"}]}
        self.result = {"status": "READY", "metadata": {"checkpoint": self.plan}}
        self.checkpoint = AsyncMock(return_value=self.result)
        self.production.providers["cutout"].assets.checkpoint = self.checkpoint
        self.production.providers["cutout"].execute = AsyncMock(return_value={"status": "READY"})
        self.production._saved_context = AsyncMock(return_value=(self.source, self.context))
        self.production.launch = AsyncMock()
        self.production.cancels = {"unit-batch": None}
        def setup(state):
            task = state["tasks"][0]
            task.update(status="FAILED", current_stage="cutout")
            task["context"]["production"].update(batch_id="unit-batch", cutout_generation=1, automatic=True)
            state["batches"] = [{"id": "unit-batch", "status": "PAUSED", "task_ids": ["unit-task"],
                "active_task_id": None, "summary": {}, "items": [{"task_id": "unit-task", "status": "FAILED", "stage": "cutout"}]}]
            state["runs"] = [{"id": "unit-failed-cutout", "task_id": "unit-task", "stage": "cutout",
                "provider": "LegacyComfyUIAdapter", "status": "failed", "error_code": "STAGE_RESULT_INVALID",
                "error_message": "TimeoutError", "pid": 123456, "logs": ["old failed run retained"]}]
        self.repository.mutate(setup)
        self.old_run = copy.deepcopy(self.repository.state()["runs"][0])

    async def resume(self):
        with patch("app.integrations.production_service.probe_process", side_effect=ProcessLookupError):
            return await self.production.resume_cutout_checkpoint("unit-task")

    async def test_same_generation_partial_returns_to_original_paused_queue(self):
        result = await self.resume()
        state = self.repository.state()
        self.assertEqual(result["status"], "PAUSED")
        self.assertFalse(result["launch"])
        self.production.launch.assert_not_awaited()
        self.assertEqual(len(state["batches"]), 1)
        self.assertEqual(state["batches"][0]["items"][0]["status"], "QUEUED")
        self.assertEqual(state["runs"], [self.old_run])
        production = state["tasks"][0]["context"]["production"]
        self.assertEqual(production["cutout_generation"], 1)
        self.assertFalse(production["retry"])
        self.assertEqual(production["requested_stage"], "cutout")
        self.assertEqual(production["cutout_checkpoint_resume"]["pinned"], {key: self.plan[key]
            for key in ("generation", "input_fingerprint", "checkpoint_fingerprint")})
        self.assertEqual(production["cutout_checkpoint_resume"]["origin_run_id"], self.old_run["id"])
        self.assertEqual(self.checkpoint.call_args.args[1].parts[-3:], ("production", "v1", "cutout"))

    async def test_completed_original_queue_is_requeued_not_replaced(self):
        self.repository.mutate(lambda state: state["batches"][0].update(status="COMPLETED_WITH_ERRORS"))
        result = await self.resume()
        self.assertTrue(result["launch"])
        self.assertEqual(result["batch_id"], "unit-batch")
        self.assertEqual(self.repository.state()["batches"][0]["status"], "QUEUED")

    async def test_live_original_child_cannot_be_restarted(self):
        with patch("app.integrations.production_service.probe_process"):
            with self.assertRaises(HTTPException) as caught:
                await self.production.resume_cutout_checkpoint("unit-task")
        self.assertIn("WORKER_STILL_RUNNING", caught.exception.detail)
        self.checkpoint.assert_not_awaited()

    async def test_unknown_paid_history_does_not_enter_local_restoration(self):
        self.repository.mutate(lambda state: state["runs"].insert(0, {"id": "unit-paid", "task_id": "unit-task",
            "status": "failed", "error_code": "UNKNOWN_EXTERNAL_REQUEST"}))
        with self.assertRaises(HTTPException) as caught:
            await self.resume()
        self.assertIn("UNKNOWN_EXTERNAL_REQUEST", caught.exception.detail)
        self.checkpoint.assert_not_awaited()

    async def test_changed_source_blocks_before_checkpoint_submission(self):
        self.production._saved_context.side_effect = HTTPException(409, "SOURCE_VIDEO_HASH_CHANGED")
        with self.assertRaises(HTTPException):
            await self.resume()
        self.checkpoint.assert_not_awaited()
        self.assertEqual(self.repository.state()["tasks"][0]["status"], "FAILED")

    async def test_invalid_or_running_checkpoint_does_not_change_failed_queue(self):
        for key, value in (("generation", 2), ("source_sha256", "changed"), ("business_executed", True),
                           ("chunks", [{"status": "RUNNING"}])):
            with self.subTest(key=key):
                self.checkpoint.return_value = {"status": "READY", "metadata": {"checkpoint": {**self.plan, key: value}}}
                with self.assertRaises(HTTPException):
                    await self.resume()
                self.assertEqual(self.repository.state()["batches"][0]["items"][0]["status"], "FAILED")
                self.assertEqual(self.repository.state()["runs"], [self.old_run])

    async def test_new_execution_during_readonly_check_blocks_queue_write(self):
        async def inspected(*args):
            self.repository.mutate(lambda state: state["runs"].append({"id": "unit-concurrent", "task_id": "unit-other", "status": "running"}))
            return self.result
        self.checkpoint.side_effect = inspected
        with self.assertRaises(HTTPException) as caught:
            await self.resume()
        self.assertIn("CUTOUT_RECOVERY_CHANGED", caught.exception.detail)
        self.assertEqual(self.repository.state()["tasks"][0]["status"], "FAILED")

    async def test_local_stage_consumes_only_three_pinned_worker_fields(self):
        await self.resume()
        async def stage(batch_id, task_id, stage, provider, invoke, **kwargs):
            return await invoke("unit-new-run", self.production.settings.storage_root, None)
        self.production._stage = stage
        await self.production._video_stage("unit-batch", "unit-task", "cutout", self.context)
        consumed = self.production.providers["cutout"].execute.call_args.args[1]
        self.assertEqual(consumed["cutout_checkpoint_resume"], {key: self.plan[key]
            for key in ("generation", "input_fingerprint", "checkpoint_fingerprint")})
        self.assertEqual(consumed["cutout_generation"], 1)
        self.assertNotIn("origin_run_id", consumed["cutout_checkpoint_resume"])


class CutoutCheckpointActionTests(unittest.TestCase):
    def test_formal_action_preserves_paused_queue_without_launch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = create_app(Settings(app_mode="integration", database_url="sqlite:///" + str(root / "unit.db"), storage_root=root / "storage"))
            app.state.production.resume_cutout_checkpoint = AsyncMock(return_value={"accepted": True, "task_id": "unit-task",
                "batch_id": "unit-batch", "status": "PAUSED", "launch": False})
            app.state.production.launch = AsyncMock()
            response = TestClient(app).post("/api/actions", json={"action": "production.resume_cutout", "entity_id": "unit-task"})
            self.assertEqual(response.status_code, 202)
            app.state.production.resume_cutout_checkpoint.assert_awaited_once_with("unit-task")
            app.state.production.launch.assert_not_awaited()
            app.state.repository.engine.dispose()

    def test_formal_action_requires_task_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = create_app(Settings(app_mode="integration", database_url="sqlite:///" + str(root / "unit.db"), storage_root=root / "storage"))
            app.state.production.resume_cutout_checkpoint = AsyncMock()
            response = TestClient(app).post("/api/actions", json={"action": "production.resume_cutout"})
            self.assertEqual(response.status_code, 422)
            app.state.production.resume_cutout_checkpoint.assert_not_awaited()
            app.state.repository.engine.dispose()


if __name__ == "__main__":
    unittest.main()
