import asyncio
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.config import Settings
from app.integrations.contracts import CollectorResult
from app.main import create_app
from app.repository import StateRepository

RULES = {"source": "/legacy/config.yaml", "sha256": "test-config", "read_only": True, "rules": {
    "min_views": 100, "min_like_view_ratio": .001, "product_match_min_confidence": .85,
    "reference_product_match_required": "pass", "accepted_product_match": "pass", "accepted_ai_origin": "likely_real",
    "accepted_cutout_suitability": "pass", "manual_review_on_uncertain": True}}


class FakeCollector:
    def __init__(self):
        self.calls = 0
        self.error_code = None

    def dry_run(self, context):
        return {"ok": True, "checks": [], "status": "READY", "task_id": context["task_id"], "business_executed": False}

    async def collect_one(self, context, on_event, cancel_event, run_id):
        self.calls += 1
        await asyncio.to_thread(on_event, {"type": "PROCESS_STARTED", "pid": 12345, "command": ["python", "factory-wrapper"], "cwd": "/legacy"})
        return {**context, "video_id": "unit-test-video", "video_url": "https://www.tiktok.com/@test/video/123",
            "views": 128000, "likes": 3420, "like_view_ratio": 999,
            "metric_source": "UNIT_TEST_ONLY", "metric_evidence": "unit evidence", "reference_product_match": "fail",
            "product_match": "fail", "product_match_confidence": .92, "product_match_reason": "different product",
            "ai_origin": "uncertain", "cutout_suitability": "uncertain", "overlay_detected": None, "watermark_detected": None,
            "decision": "manual_review" if self.error_code else "rejected", "decision_reason": "unit mismatch",
            "legacy_csv_path": "/legacy/output/candidates.csv", "legacy_result_reference": {},
            "error_code": self.error_code, "error_message": "unit safe error" if self.error_code else None,
            "_process": {"exit_code": 0, "stdout_log_path": "/unit/stdout.log", "stderr_log_path": "/unit/stderr.log"}}


class RealCollectorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.config = Settings(app_mode="integration", integration_dry_run=False,
            database_url="sqlite:///" + str(self.root / "test.db"), storage_root=self.root / "storage")
        self.adapter = FakeCollector()
        self.rules_patch = patch("app.integrations.collector_service.read_production_rules", return_value=RULES)
        self.rules_patch.start()
        self.app = create_app(self.config, collector_adapter=self.adapter)
        self.client = TestClient(self.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.client.close()
        self.app.state.repository.engine.dispose()
        self.rules_patch.stop()
        self.directory.cleanup()

    def create(self, country="USA"):
        response = self.client.post("/api/actions", json={"action": "create_collection", "payload": {
            "country": country, "keyword": "lipstick", "product_images": [str(self.root / "product.jpg")], "source_row": 7}})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["task_id"]

    def settled(self, task):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            detail = self.client.get("/api/tasks/" + task).json()
            if detail["task"]["status"] != "COLLECTING":
                return detail
            time.sleep(.01)
        self.fail("isolated test task did not finish")

    def test_country_contexts_are_independent_and_dry_run_never_collects(self):
        one, two = self.create(), self.create("FR")
        first, second = [self.client.get("/api/tasks/" + task).json()["task"] for task in (one, two)]
        self.assertNotEqual(first["context"]["task_context"]["context_id"], second["context"]["task_context"]["context_id"])
        self.assertNotEqual(one, first["context"]["task_context"]["context_id"])
        self.assertEqual(first["context"]["task_context"]["source_row"], 7)
        self.assertEqual(self.client.get("/api/integrations/collector/dry-run", params={"task_id": one}).json()["status"], "READY")
        self.assertEqual(self.adapter.calls, 0)

    def test_result_is_persisted_and_nonzero_exit_is_not_the_decision(self):
        task = self.create()
        with self.client.websocket_connect("/ws/tasks/" + task) as socket:
            self.assertEqual(socket.receive_json()["type"], "snapshot")
            response = self.client.post("/api/actions", json={"action": "start_collection", "entity_id": task})
            self.assertEqual(response.status_code, 202, response.text)
            types = []
            while "STAGE_COMPLETED" not in types:
                event = socket.receive_json()
                self.assertEqual(event["task_id"], task)
                types.append(event["type"])
            self.assertTrue({"STAGE_STARTED", "STAGE_PROGRESS", "METRICS_UPDATED", "AI_ANALYSIS_UPDATED", "TASK_DECISION"}.issubset(types))
        detail = self.client.get("/api/tasks/" + task).json()
        self.assertEqual(detail["task"]["status"], "REJECTED")
        self.assertEqual(detail["task"]["collector_result"]["decision"], "rejected")
        self.assertEqual(next(row for row in detail["stages"] if row["name"] == "collector")["status"], "success")
        self.assertEqual(detail["runs"][0]["status"], "success")
        self.assertIsNone(detail["runs"][0]["error_code"])
        self.assertAlmostEqual(detail["task"]["collector_result"]["like_view_ratio"], 2.671875)
        self.assertEqual(detail["assets"][0]["asset_type"], "SOURCE_CANDIDATE")
        self.assertIsNone(detail["task"]["source_video_id"])
        self.assertTrue(all(row["status"] == "not_connected" for row in detail["stages"] if row["name"] != "collector"))
        self.assertEqual(detail["runs"][0]["pid"], 12345)
        self.assertIsNotNone(detail["runs"][0]["finished_at"])
        response = self.client.post("/api/actions", json={"action": "start_collection", "entity_id": task})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"], "ALREADY_COMPLETED")
        self.assertEqual(self.client.post("/api/actions", json={"action": "retry_collection", "entity_id": task}).status_code, 409)
        self.assertEqual(self.adapter.calls, 1)
        self.assertEqual(self.client.post("/api/actions", json={"action": "start_cutout", "entity_id": task}).status_code, 409)
        restarted = StateRepository(self.config.database_url, Path(__file__).parents[1] / "app/seed.json")
        persisted = next(row for row in restarted.state()["tasks"] if row["id"] == task)
        self.assertEqual(persisted["collector_result"]["video_id"], "unit-test-video")
        self.assertEqual(persisted["status"], "REJECTED")
        restarted.engine.dispose()

    def test_errors_keep_run_history_and_retry_creates_another_run(self):
        task = self.create()
        self.adapter.error_code = "COLLECTOR_ERROR"
        self.client.post("/api/actions", json={"action": "start_collection", "entity_id": task})
        self.assertEqual(self.settled(task)["task"]["status"], "FAILED")
        self.adapter.error_code = None
        self.assertEqual(self.client.post("/api/actions", json={"action": "retry_collection", "entity_id": task}).status_code, 202)
        runs = self.settled(task)["runs"]
        self.assertEqual(len(runs), 2)
        self.assertEqual([run["status"] for run in runs], ["failed", "success"])
        self.assertNotEqual(runs[0]["id"], runs[1]["id"])

    def test_unknown_paid_request_is_not_retried(self):
        task = self.create()
        self.adapter.error_code = "UNKNOWN_EXTERNAL_REQUEST"
        self.client.post("/api/actions", json={"action": "start_collection", "entity_id": task})
        detail = self.settled(task)
        self.assertEqual(detail["task"]["status"], "QUARANTINED")
        self.assertEqual(detail["task"]["context"]["collector_auto_triage"]["reason"], "UNKNOWN_EXTERNAL_REQUEST")
        self.assertEqual(detail["runs"][0]["status"], "failed")
        self.assertEqual(self.client.post("/api/actions", json={"action": "retry_collection", "entity_id": task}).status_code, 409)
        self.assertEqual(self.adapter.calls, 1)

    def test_unverified_or_missing_source_is_a_failure_not_a_download(self):
        for code in ("OUTPUT_NOT_FOUND", "SOURCE_NOT_VERIFIED"):
            task = self.create()
            self.adapter.error_code = code
            self.client.post("/api/actions", json={"action": "start_collection", "entity_id": task})
            detail = self.settled(task)
            self.assertEqual(detail["task"]["status"], "FAILED")
            self.assertEqual(detail["runs"][0]["error_code"], code)
            self.assertFalse(detail["assets"])

    def test_single_task_limit_and_duplicate_click(self):
        one, two = self.create(), self.create("FR")
        service = self.app.state.collector
        asyncio.run(service.queue(one))
        from fastapi import HTTPException
        for task, code in ((one, "ALREADY_RUNNING"), (two, "SINGLE_TASK_LIMIT")):
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(service.queue(task))
            self.assertIn(code, str(raised.exception.detail))

    def test_zero_views_ratio_is_zero(self):
        context = self.app.state.collector.context(self.create())
        result = CollectorResult(**context, views=0, likes=0, decision="rejected", decision_reason="zero",
                                 legacy_csv_path="/legacy/candidates.csv", legacy_result_reference={})
        self.assertEqual(result.like_view_ratio, 0)

    def test_state_must_be_rechecked_after_awaited_preflight(self):
        task = self.create()
        service = self.app.state.collector
        def preflight(context):
            service.repository.mutate(lambda state: next(row for row in state["tasks"] if row["id"] == task).update(status="COLLECTED"))
            return {"ok": True}
        from fastapi import HTTPException
        with patch.object(self.adapter, "dry_run", side_effect=preflight):
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(service.queue(task))
        self.assertEqual(raised.exception.detail, "ALREADY_COMPLETED")
        self.assertFalse(self.client.get("/api/tasks/" + task).json()["runs"])

    def test_cancelled_or_skipped_after_preflight_never_creates_run(self):
        from fastapi import HTTPException
        for status in ("CANCELLED", "SKIPPED"):
            with self.subTest(status=status):
                task = self.create()
                service = self.app.state.collector
                def preflight(context):
                    service.repository.mutate(lambda state: next(row for row in state["tasks"] if row["id"] == task).update(status=status))
                    return {"ok": True}
                with patch.object(self.adapter, "dry_run", side_effect=preflight):
                    with self.assertRaises(HTTPException) as stopped:
                        asyncio.run(service.queue(task))
                self.assertEqual(stopped.exception.status_code, 409)
                detail = self.client.get("/api/tasks/" + task).json()
                self.assertEqual(detail["task"]["status"], status)
                self.assertFalse(detail["runs"])
        self.assertEqual(self.adapter.calls, 0)

    def test_cancelled_bound_batch_cannot_queue_even_if_task_status_is_stale(self):
        from fastapi import HTTPException
        task = self.create()
        service = self.app.state.collector
        def cancel_batch(state):
            current = next(row for row in state["tasks"] if row["id"] == task)
            current["context"]["production"] = {"batch_id": "unit-cancelled", "automatic": True}
            timestamp = datetime.now(timezone.utc).isoformat()
            state["batches"].append({"id": "unit-cancelled", "task_ids": [task], "status": "CANCELLED",
                "items": [{"task_id": task, "status": "CANCELLED", "stage": "collector"}],
                "created_at": timestamp, "updated_at": timestamp})
        service.repository.mutate(cancel_batch)
        with self.assertRaises(HTTPException):
            asyncio.run(service.queue(task))
        self.assertFalse(self.client.get("/api/tasks/" + task).json()["runs"])
        self.assertEqual(self.adapter.calls, 0)

    def test_parent_cancel_event_during_preflight_prevents_collection(self):
        from fastapi import HTTPException
        task = self.create()
        service, stop = self.app.state.collector, threading.Event()
        def preflight(context):
            stop.set()
            return {"ok": True}
        with patch.object(self.adapter, "dry_run", side_effect=preflight):
            with self.assertRaises(HTTPException):
                asyncio.run(service.queue(task, cancel_event=stop))
        detail = self.client.get("/api/tasks/" + task).json()
        self.assertEqual(detail["task"]["status"], "CREATED")
        self.assertFalse(detail["runs"])
        self.assertEqual(self.adapter.calls, 0)

    def test_cancellation_before_spawn_does_not_persist_zero_pid(self):
        task = self.create()
        service = self.app.state.collector
        async def cancelled(context, **kwargs):
            self.assertTrue(kwargs["cancel_event"].is_set())
            return {**context, "decision": "manual_review", "decision_reason": "cancelled",
                    "error_code": "PROCESS_CANCELLED", "error_message": "cancelled before spawn",
                    "legacy_csv_path": "/legacy/candidates.csv", "legacy_result_reference": {},
                    "_process": {"pid": 0, "exit_code": -15}}
        async def scenario():
            queued = await service.queue(task)
            await service.cancel(task)
            with patch.object(self.adapter, "collect_one", side_effect=cancelled):
                await service.run(task, queued["run_id"])
        asyncio.run(scenario())
        detail = self.client.get("/api/tasks/" + task).json()
        self.assertEqual(detail["task"]["status"], "FAILED")
        self.assertIsNone(detail["runs"][0]["pid"])
        self.assertEqual(detail["runs"][0]["error_code"], "PROCESS_CANCELLED")

    def test_http_returns_before_long_job_and_shutdown_cancels_it(self):
        task = self.create()
        async def blocked(context, on_event, cancel_event, **kwargs):
            await asyncio.to_thread(on_event, {"type": "PROCESS_STARTED", "pid": 67890,
                "command": ["unit-wrapper"], "cwd": "/unit", "stdout_log_path": "/unit/stdout",
                "stderr_log_path": "/unit/stderr"})
            while not cancel_event.is_set():
                await asyncio.sleep(.01)
            return {**context, "decision": "manual_review", "decision_reason": "unknown request",
                    "error_code": "UNKNOWN_EXTERNAL_REQUEST", "error_message": "cancelled safely",
                    "legacy_csv_path": "/legacy/candidates.csv", "legacy_result_reference": {}}
        with patch.object(self.adapter, "collect_one", side_effect=blocked):
            started = time.monotonic()
            response = self.client.post("/api/actions", json={"action": "start_collection", "entity_id": task})
            self.assertEqual(response.status_code, 202)
            self.assertLess(time.monotonic() - started, 1)
            time.sleep(.05)
            self.client.portal.call(self.app.state.collector.shutdown)
        detail = self.settled(task)
        self.assertEqual(detail["task"]["status"], "QUARANTINED")
        self.assertEqual(detail["runs"][0]["stdout_log_path"], "/unit/stdout")

    def test_recovery_keeps_original_logs_and_elapsed_time(self):
        task = self.create()
        service = self.app.state.collector
        queued = asyncio.run(service.queue(task))
        timestamp = (datetime.now(timezone.utc) - timedelta(seconds=20)).isoformat()
        service.repository.mutate(lambda state: next(row for row in state["runs"] if row["id"] == queued["run_id"]).update(
            status="running", started_at=timestamp, pid=12345, stdout_log_path="/unit/stdout", stderr_log_path="/unit/stderr"))
        asyncio.run(service.recover())
        detail = self.client.get("/api/tasks/" + task).json()
        self.assertEqual(detail["task"]["status"], "QUARANTINED")
        self.assertGreaterEqual(detail["runs"][0]["duration_ms"], 20000)
        self.assertEqual(detail["runs"][0]["stdout_log_path"], "/unit/stdout")


if __name__ == "__main__":
    unittest.main()
