"""Isolated read-path regression tests; no production DB or media rendering."""
import json
import unittest
from copy import deepcopy
from unittest.mock import patch

from sqlalchemy import event

from tests.test_asset_registry_routes import AssetRegistryRouteTests


class ScopedAssetReadTests(unittest.TestCase):
    def setUp(self):
        self.fixture = AssetRegistryRouteTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.repository = self.fixture.app.state.repository
        self.client = self.fixture.client
        self.task_id = "unit-assets-task"

    def test_row_and_scoped_asset_reads_equal_existing_state_views(self):
        for scope in ("production", "raw", "history", "technical"):
            expected = self.repository.state(scope=scope)["assets"]
            self.assertEqual(self.repository.read_assets(scope=scope), expected)
            for task_id in (self.task_id, "tsk-1001", "no-such-task"):
                self.assertEqual(self.repository.read_assets(scope=scope, task_id=task_id),
                    [row for row in expected if row["task_id"] == task_id])
        state = self.repository.state()
        self.assertEqual(self.repository.read_task(self.task_id), next(row for row in state["tasks"] if row["id"] == self.task_id))
        self.assertEqual(self.repository.read_asset("unit-person"), next(row for row in state["assets"] if row["id"] == "unit-person"))
        self.assertIsNone(self.repository.read_task("no-such"))
        self.assertIsNone(self.repository.read_asset("no-such"))

    def test_asset_context_preserves_technical_run_filtering_and_lineage_rows(self):
        def add_cross_task_technical_reference(state):
            technical = deepcopy(next(row for row in state["assets"] if row["id"] == "unit-technical"))
            technical.update(id="unit-cross-task-technical", task_id=state["tasks"][0]["id"],
                legacy_reference={"execution_scope": "TECHNICAL_VALIDATION", "processing_run_id": "unit-source-run"})
            state["assets"].append(technical)
        self.repository.mutate(add_cross_task_technical_reference)
        for scope in ("production", "raw", "history", "technical"):
            expected = self.repository.state(scope=scope)
            actual = self.repository.read_asset_context("unit-compose", scope=scope)
            for key in ("tasks", "assets", "runs", "reviews", "quality_assessments"):
                self.assertEqual([row for row in actual[key] if row.get("task_id", row["id"]) == self.task_id],
                    [row for row in expected[key] if row.get("task_id", row["id"]) == self.task_id], (scope, key))

    def test_single_asset_media_registry_versions_and_queue_do_not_read_full_state(self):
        before = json.dumps(self.repository.state(), sort_keys=True)
        with patch.object(self.repository, "_read", side_effect=AssertionError("whole-state read forbidden")):
            for path in ("/api/assets/unit-source", "/api/assets/unit-source/content", "/api/assets/unit-source/preview",
                         "/api/assets/unit-compose/registry", "/api/assets/media-status",
                         f"/api/assets/media-status?task_id={self.task_id}",
                         f"/api/manual-compose/tasks/{self.task_id}/transforms", "/api/production/queue"):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200, (path, response.text))
            self.assertEqual(self.client.get("/api/assets/no-such").status_code, 404)
            self.assertEqual(self.client.get("/api/assets/unit-technical/registry").status_code, 404)
            self.assertEqual(self.client.get("/api/manual-compose/tasks/no-such/transforms").status_code, 404)
        self.assertEqual(json.dumps(self.repository.state(), sort_keys=True), before)

    def test_nonperson_preview_queries_asset_once_and_keeps_size_validation(self):
        statements = []
        def capture(_connection, _cursor, statement, _parameters, _context, _executemany):
            statements.append(statement)
        event.listen(self.repository.engine, "before_cursor_execute", capture)
        self.addCleanup(event.remove, self.repository.engine, "before_cursor_execute", capture)
        self.assertEqual(self.client.get("/api/assets/unit-source/preview").status_code, 200)
        self.assertEqual(len(statements), 1)
        self.assertIn("video_assets.id =", statements[0])
        (self.fixture.config.storage_root / "source.unit").write_bytes(b"changed size")
        self.assertEqual(self.client.get("/api/assets/unit-source/content").status_code, 404)
        self.assertEqual(self.client.get("/api/assets/unit-source/preview").status_code, 404)

    def test_media_status_task_scope_does_not_expose_technical_or_foreign_assets(self):
        response = self.client.get(f"/api/assets/media-status?task_id={self.task_id}")
        self.assertEqual(response.status_code, 200)
        identifiers = {row["asset_id"] for row in response.json()}
        self.assertIn("unit-source", identifiers)
        self.assertNotIn("unit-technical", identifiers)
        self.assertTrue(all(row["task_id"] == self.task_id for row in self.repository.read_assets(scope="production", task_id=self.task_id)))
        self.assertEqual(self.client.get("/api/assets/media-status?task_id=no-such").json(), [])
        history = self.client.get(f"/api/assets/media-status?scope=history&task_id={self.task_id}").json()
        self.assertIn("unit-technical", {row["asset_id"] for row in history})

    def test_queue_keeps_latest_active_preference_and_latest_finished_fallback(self):
        def add_batches(state):
            for identifier, status, day in (("unit-older-paused", "PAUSED", 1), ("unit-active", "RUNNING", 2), ("unit-latest-done", "COMPLETED", 3)):
                timestamp = f"2026-10-0{day}T00:00:00+00:00"
                state["batches"].append({"id": identifier, "status": status, "task_ids": [self.task_id], "items": [],
                    "active_task_id": None, "summary": {}, "created_at": timestamp, "updated_at": timestamp})
        self.repository.mutate(add_batches)
        with patch.object(self.repository, "_read", side_effect=AssertionError("whole-state read forbidden")):
            self.assertEqual(self.fixture.app.state.production.queue()["batch_id"], "unit-active")
        self.repository.mutate(lambda state: [row.update(status="COMPLETED") for row in state["batches"]])
        with patch.object(self.repository, "_read", side_effect=AssertionError("whole-state read forbidden")):
            self.assertEqual(self.fixture.app.state.production.queue()["batch_id"], "unit-latest-done")

    def test_manual_run_poll_reads_only_target_run_and_completed_task_results(self):
        run_id = "unit-manual-compose-run"
        def add_run(state):
            template = next(row for row in state["runs"] if row["id"] == "unit-compose-run")
            state["runs"].append({**deepcopy(template), "id": run_id, "run_id": run_id,
                "provider": "FactoryManualCompose", "status": "running", "resource_info": {"manual_compose_phase": "RENDERING"}})
        self.repository.mutate(add_run)
        with patch.object(self.repository, "_read", side_effect=AssertionError("whole-state read forbidden")), \
                patch.object(self.repository, "read_assets", side_effect=AssertionError("running poll must not read assets")):
            running = self.client.get(f"/api/manual-compose/runs/{run_id}")
            self.assertEqual(running.status_code, 200)
            self.assertEqual((running.json()["status"], running.json()["phase"]), ("RUNNING", "RENDERING"))
            self.assertIsNone(running.json()["result"])
            self.assertEqual(self.client.get("/api/manual-compose/runs/no-such").status_code, 404)
            self.assertEqual(self.client.get("/api/manual-compose/runs/unit-source-run").status_code, 404)
        def complete(state):
            run = next(row for row in state["runs"] if row["id"] == run_id)
            run.update(status="success", resource_info={"manual_compose": {"mode": "COMPOSITION"}})
            asset = next(row for row in state["assets"] if row["id"] == "unit-compose")
            asset["legacy_reference"]["processing_run_id"] = run_id
            state["reviews"].append({"id": "unit-manual-review", "task_id": self.task_id,
                "asset_id": asset["id"], "status": "pending", "issues": [], "note": ""})
        self.repository.mutate(complete)
        with patch.object(self.repository, "_read", side_effect=AssertionError("whole-state read forbidden")):
            result = self.client.get(f"/api/manual-compose/runs/{run_id}").json()
        self.assertEqual(result["status"], "READY")
        self.assertEqual(result["result"]["asset"]["id"], "unit-compose")
        self.assertEqual(result["result"]["review"]["id"], "unit-manual-review")
        self.assertEqual(result["result"]["review"]["status"], "pending")
