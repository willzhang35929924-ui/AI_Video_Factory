"""Restore only persisted proof of the zero-Run shared-CDP preflight regression."""

import copy
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.integrations.production_routes import create_production_router
from test_real_review import ReviewFixture


class EnvironmentBatchRestoreTests(ReviewFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.prepare()
        def setup(state):
            template = copy.deepcopy(state["tasks"][0])
            state.update(tasks=[], assets=[], prompts=[], reviews=[], quality_assessments=[], errors=[], batches=[])
            for index in range(3):
                task = {**copy.deepcopy(template), "id": f"unit-env-{index}", "status": "FAILED", "current_stage": "collector", "collector_result": {}}
                task["context"]["production"] = {"batch_id": "unit-batch", "previous_status": "CREATED"}
                task["context"]["collector_environment"] = {"schema": "collector-environment-v1", "status": "MISSING",
                    "business_executed": False, "paid_requests": 0, "checked_at": "2026-10-09T09:10:56.628604+00:00",
                    "checks": [{"name": "existing_cdp_get", "status": "MISSING", "error_code": "CHROME_CDP_UNAVAILABLE"}]}
                state["tasks"].append(task)
                state["errors"].append({"id": f"unit-env-error-{index}", "task_id": task["id"], "stage": "collector",
                    "provider": "FactoryProductionQueue", "code": "PRODUCTION_ERROR", "message": "生产阶段异常：HTTPException",
                    "created_at": "2026-10-09T09:10:58.821022"})
            state["batches"] = [{"id": "unit-batch", "status": "PAUSED", "task_ids": [task["id"] for task in state["tasks"]],
                "items": [{"task_id": task["id"], "status": "FAILED", "stage": "collector"} for task in state["tasks"]],
                "active_task_id": None, "summary": {}, "updated_at": "unit-paused"}]
        self.repository.mutate(setup)
        self.original_errors = copy.deepcopy(self.repository.state()["errors"])
        self.production.launch = AsyncMock()
        self.production.collector = AsyncMock()

    async def restore(self, plan=None):
        plan = plan or self.production.environment_restore_plan("unit-batch")
        return await self.production.restore_environment("unit-batch", confirmed=True, fingerprint=plan["fingerprint"])

    async def test_three_verified_environment_failures_requeue_with_history_and_pause(self):
        plan = self.production.environment_restore_plan("unit-batch")
        self.assertEqual(plan["eligible_count"], 3)
        result = await self.restore(plan)
        self.assertEqual(result["restored_count"], 3)
        state = self.repository.state()
        self.assertEqual(state["errors"], self.original_errors)
        self.assertEqual(state["runs"], [])
        self.assertEqual(state["batches"][0]["status"], "PAUSED")
        self.assertTrue(all(item["status"] == "QUEUED" for item in state["batches"][0]["items"]))
        self.assertEqual(state["tasks"][0]["context"]["production"]["environment_restorations"][0]["error_id"], "unit-env-error-0")
        self.production.launch.assert_not_awaited()

    async def test_any_run_asset_or_review_is_preserved_not_restored(self):
        def protected(state):
            state["runs"].append({"id": "unit-cutout", "task_id": "unit-env-0", "status": "failed"})
            state["assets"].append({"id": "unit-asset", "task_id": "unit-env-1"})
            state["reviews"].append({"id": "unit-review", "task_id": "unit-env-2"})
        self.repository.mutate(protected)
        plan = self.production.environment_restore_plan("unit-batch")
        self.assertEqual(plan["eligible_count"], 0)
        self.assertEqual(plan["preserved_count"], 3)

    async def test_generic_error_without_same_preflight_proof_is_not_restored(self):
        for field, value in (("checked_at", "2026-10-08T09:10:56+00:00"), ("business_executed", True),
                             ("paid_requests", 1), ("schema", "other")):
            with self.subTest(field=field):
                original = self.repository.state()
                self.repository.mutate(lambda state: state["tasks"][0]["context"]["collector_environment"].update({field: value}))
                self.assertEqual(self.production.environment_restore_plan("unit-batch")["eligible_count"], 2)
                self.repository.value = original

    async def test_required_missing_check_is_not_ignored_when_required_flag_absent(self):
        self.assertEqual(self.production.environment_restore_plan("unit-batch")["eligible_count"], 3)
        self.repository.mutate(lambda state: state["tasks"][0]["context"]["collector_environment"]["checks"].append(
            {"name": "unknown_required_dependency", "status": "MISSING"}))
        self.assertEqual(self.production.environment_restore_plan("unit-batch")["eligible_count"], 2)

    async def test_fingerprint_or_fresh_missing_environment_blocks_mutation(self):
        plan = self.production.environment_restore_plan("unit-batch")
        self.repository.mutate(lambda state: state["errors"][0].update(message="different cause"))
        with self.assertRaises(HTTPException):
            await self.restore(plan)
        self.production.readiness.check = AsyncMock(return_value={"checks": []})
        with self.assertRaises(HTTPException) as caught:
            await self.restore()
        self.assertEqual(caught.exception.detail["code"], "PRODUCTION_NOT_READY")
        self.assertTrue(all(item["status"] == "FAILED" for item in self.repository.state()["batches"][0]["items"]))

    async def test_confirmation_and_running_repair_are_never_bypassed(self):
        plan = self.production.environment_restore_plan("unit-batch")
        with self.assertRaises(HTTPException):
            await self.production.restore_environment("unit-batch", confirmed=False, fingerprint=plan["fingerprint"])
        self.repository.mutate(lambda state: state["runs"].append({"id": "unit-repair", "task_id": "unit-other", "status": "running"}))
        with self.assertRaises(HTTPException) as caught:
            await self.restore()
        self.assertIn("ALREADY_RUNNING", caught.exception.detail)

    async def test_existing_resume_button_restores_preflight_failures_then_launches_original_batch(self):
        self.production._download_resume = lambda _: False
        self.production._local_resume = lambda _: False
        result = await self.production.control("resume", batch_id="unit-batch")
        self.assertEqual(result["batch_id"], "unit-batch")
        self.production.launch.assert_awaited_once_with("unit-batch")
        self.assertEqual(self.repository.state()["errors"], self.original_errors)
        self.assertTrue(all(item["status"] == "QUEUED" for item in self.repository.state()["batches"][0]["items"]))

    async def test_formal_restore_endpoint_keeps_batch_paused(self):
        app = FastAPI()
        app.include_router(create_production_router(self.production))
        plan = self.production.environment_restore_plan("unit-batch")
        with patch.object(self.production, "restore_environment", new=AsyncMock(return_value={"accepted": True, "status": "PAUSED", "restored_count": 3})) as restore:
            response = TestClient(app).post("/api/production/batches/unit-batch/restore-environment", json={"confirmed": True, "fingerprint": plan["fingerprint"]})
        self.assertEqual(response.status_code, 202)
        restore.assert_awaited_once_with("unit-batch", confirmed=True, fingerprint=plan["fingerprint"])
        self.production.launch.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
