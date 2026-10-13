import copy
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from fastapi import HTTPException

from app.domain import find
from app.integrations.comfy_asset_adapter import CutoutContext
from app.integrations.local_cutout import LocalCutoutService
from app.integrations.production_service import ProductionService
import test_manual_compose


class LocalCutoutTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_manual_compose.ManualComposeTests.asyncSetUp
    asyncTearDown = test_manual_compose.ManualComposeTests.asyncTearDown
    ffmpeg = test_manual_compose.ManualComposeTests.ffmpeg
    upload = test_manual_compose.ManualComposeTests.upload

    def local(self):
        production = self.service.production
        production.start = AsyncMock(return_value={"batch_id": "unit-batch", "accepted": True})
        production.launch = AsyncMock()
        production._process_event = AsyncMock()
        return LocalCutoutService(production, self.service)

    async def test_local_import_real_decode_registers_source_not_fake_person_or_ai_pass(self):
        local = self.local()
        response = await local.import_video(self.upload(self.rgb))
        task_id, run_id = response["task_id"], response["run_id"]
        task = find(self.repository.state(), "tasks", task_id)
        self.assertEqual(task["collector_result"], {})
        self.assertEqual(task["country"], "未分类")
        local.cancels[run_id] = threading.Event()
        await local.validate(task_id, run_id)
        task = find(self.repository.state(), "tasks", task_id)
        self.assertTrue(task["context"]["local_cutout"].get("full_decode_verified"), task["context"]["local_cutout"])
        source = find(self.repository.state(), "assets", task["context"]["local_cutout"]["source_asset_id"])
        self.assertEqual(source["type"], "source")
        self.assertEqual(source["registry_status"], "registered")
        self.assertTrue(source["legacy_reference"]["full_decode_verified"])
        self.assertEqual(task["collector_result"], {})
        self.assertFalse(any(row["type"] == "person" and row["task_id"] == task_id for row in self.repository.state()["assets"]))
        local.production.start.assert_awaited_once_with([task_id], stage="cutout")
        local.production.launch.assert_awaited_once_with("unit-batch")
        _, context = await ProductionService._saved_context(local.production, task)
        self.assertEqual(context["source_video"], source["storage_path"])
        self.assertEqual(CutoutContext.model_validate(context).product_images, [])
        self.assertEqual(find(self.repository.state(), "runs", run_id)["status"], "success")

    async def test_queue_wait_keeps_real_source_and_does_not_mark_import_failed(self):
        local = self.local()
        local.production.start.side_effect = HTTPException(409, "BATCH_ALREADY_ACTIVE")
        response = await local.import_video(self.upload(self.rgb))
        local.cancels[response["run_id"]] = threading.Event()
        await local.validate(response["task_id"], response["run_id"])
        task = find(self.repository.state(), "tasks", response["task_id"])
        self.assertTrue(task["context"]["local_cutout"]["full_decode_verified"])
        self.assertEqual(task["context"]["production"]["waiting"]["reason"], "LOCAL_CUTOUT_DEPENDENCY_OR_QUEUE")
        self.assertEqual(find(self.repository.state(), "runs", response["run_id"])["status"], "success")
        local.production.launch.assert_not_awaited()

    async def test_unknown_file_format_has_no_task_or_asset_side_effect(self):
        local = self.local()
        before = copy.deepcopy(self.repository.state())
        with self.assertRaises(HTTPException):
            await local.import_video(self.upload(self.rgb, "not-video.txt"))
        self.assertEqual(self.repository.state(), before)

    async def test_changed_local_proof_cannot_bypass_collector_approval(self):
        local = self.local()
        response = await local.import_video(self.upload(self.rgb))
        local.cancels[response["run_id"]] = threading.Event()
        await local.validate(response["task_id"], response["run_id"])
        task = find(self.repository.state(), "tasks", response["task_id"])
        task["context"]["local_cutout"]["source_asset_id"] = "not-this-source"
        with self.assertRaises(HTTPException) as error:
            await ProductionService._saved_context(local.production, task)
        self.assertEqual(error.exception.detail, "LOCAL_SOURCE_PROOF_CHANGED")

    async def test_interrupted_import_retains_file_and_resumes_in_new_run(self):
        local = self.local()
        response = await local.import_video(self.upload(self.rgb))
        task_id, first_run = response["task_id"], response["run_id"]
        path = Path(find(self.repository.state(), "tasks", task_id)["context"]["local_cutout"]["path"])
        await local.recover()
        failed = find(self.repository.state(), "runs", first_run)
        self.assertEqual(failed["error_code"], "LOCAL_IMPORT_INTERRUPTED")
        self.assertTrue(path.is_file())
        resumed = await local.resume_import(task_id)
        self.assertNotEqual(resumed["run_id"], first_run)
        self.assertEqual(find(self.repository.state(), "runs", first_run)["status"], "failed")
        local.cancels[resumed["run_id"]] = threading.Event()
        await local.validate(task_id, resumed["run_id"])
        self.assertEqual(find(self.repository.state(), "runs", resumed["run_id"])["status"], "success")
        local.production.start.assert_awaited_once()

    async def test_resume_missing_upload_does_not_create_new_run(self):
        local = self.local()
        response = await local.import_video(self.upload(self.rgb))
        await local.recover()
        path = Path(find(self.repository.state(), "tasks", response["task_id"])["context"]["local_cutout"]["path"])
        path.unlink()
        before = len(self.repository.state()["runs"])
        with self.assertRaises(HTTPException) as error:
            await local.resume_import(response["task_id"])
        self.assertIn("LOCAL_UPLOAD_NOT_FOUND", error.exception.detail)
        self.assertEqual(len(self.repository.state()["runs"]), before)
