import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from app.config import Settings
from app.domain import find, now
from app.integrations.asset_registry import file_hash
from app.integrations.matte_repair import MatteRepairService
from app.integrations.production_service import ProductionService
from app.repository import StateRepository
import test_real_review as review_fixtures


class WorkflowExecutionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.settings = Settings(_env_file=None, storage_root=self.root / "storage", database_url="sqlite:///" + str(self.root / "workflow.db"))
        self.repository = StateRepository(self.settings.database_url, Path(__file__).parents[1] / "app/seed.json")
        self.addCleanup(self.repository.engine.dispose)
        self.production = object.__new__(ProductionService)
        self.production.repository, self.production.settings = self.repository, self.settings
        self.task_id, self.run_id = "tsk-1002", "current-wrapper-run"
        self.graph = {"1": {"class_type": "ImageCompositeMasked", "inputs": {"x": 0, "y": 0}}}
        origin_root, run_root = self.settings.storage_root / "original", self.settings.storage_root / "current"
        origin_root.mkdir(parents=True)
        run_root.mkdir()
        self.source = origin_root / "part_000000.api.json"
        self.path = run_root / "workflow-api.json"
        self.source.write_text(json.dumps(self.graph, ensure_ascii=False, indent=2))
        self.path.write_bytes(self.source.read_bytes())
        self.history_path = origin_root / "part_000000.history.json"
        self.origin = {"processing_run_id": "original-comfy-run", "task_id": "original-technical-task",
            "prompt_id": "real-origin-prompt", "submitted_graph_sha256": file_hash(self.source),
            "workflow_path": str(origin_root.with_name("original.partial") / self.source.name),
            "history": {"prompt": [7, "real-origin-prompt", self.graph, {}, ["1"]], "status": {"completed": True, "status_str": "success"}}}
        self.write_history()
        self.snapshot = {"workflow_snapshot_id": "workflow-unit-cache", "task_id": self.task_id, "processing_run_id": self.run_id,
            "prompt_id": self.origin["prompt_id"], "submitted": False, "cache_hit": True,
            "origin_processing_run_id": self.origin["processing_run_id"], "origin_task_id": self.origin["task_id"],
            "origin_history_path": str(self.history_path), "origin_history_sha256": file_hash(self.history_path),
            "path": str(self.path), "sha256": file_hash(self.path)}

    def write_history(self):
        self.history_path.write_text(json.dumps(self.origin))

    def save(self, snapshot=None):
        return self.production.save_snapshot(self.task_id, self.run_id, snapshot or self.snapshot)

    def test_cached_registration_keeps_original_identity_graph_bytes_and_flags(self):
        original = self.source.read_bytes()
        saved = self.save()
        self.assertEqual(saved["processing_run_id"], self.run_id)
        self.assertEqual(saved["execution"]["origin_processing_run_id"], "original-comfy-run")
        self.assertEqual(saved["execution"]["origin_task_id"], "original-technical-task")
        self.assertTrue(saved["execution"]["cache_hit"])
        self.assertFalse(saved["execution"]["submitted"])
        self.assertEqual(saved["execution"]["origin_workflow_path"], self.origin["workflow_path"])
        self.assertEqual(saved["execution"]["resolved_origin_workflow_path"], str(self.source))
        self.assertEqual(self.source.read_bytes(), original)
        self.assertEqual(self.path.read_bytes(), original)
        fresh = StateRepository(self.settings.database_url, Path(__file__).parents[1] / "app/seed.json")
        self.addCleanup(fresh.engine.dispose)
        record = next(row for row in fresh.workflows(self.task_id) if row["id"] == saved["id"])
        self.assertEqual(record["execution"], saved["execution"])
        self.assertEqual(record["workflow"], self.graph)

    def test_history_hash_and_actual_origin_graph_must_match(self):
        self.history_path.write_text("{}")
        with self.assertRaisesRegex(ValueError, "HISTORY_NOT_VERIFIED"):
            self.save()
        self.write_history()
        self.source.write_bytes(b"changed original graph")
        with self.assertRaisesRegex(ValueError, "IDENTITY_OR_GRAPH_MISMATCH"):
            self.save()

    def test_claimed_original_run_task_prompt_and_finished_history_are_verified(self):
        for field, value in (("processing_run_id", "false-run"), ("task_id", "false-task"), ("prompt_id", "false-prompt")):
            original = self.origin[field]
            self.origin[field] = value
            self.write_history()
            snapshot = {**self.snapshot, "origin_history_sha256": file_hash(self.history_path)}
            with self.assertRaisesRegex(ValueError, "IDENTITY_OR_GRAPH_MISMATCH"):
                self.save(snapshot)
            self.origin[field] = original
        self.origin["history"]["status"]["completed"] = False
        self.write_history()
        with self.assertRaisesRegex(ValueError, "IDENTITY_OR_GRAPH_MISMATCH"):
            self.save({**self.snapshot, "origin_history_sha256": file_hash(self.history_path)})

    def test_cache_cannot_claim_new_submission_or_missing_original_evidence(self):
        with self.assertRaisesRegex(ValueError, "CACHE_CANNOT_CLAIM_SUBMISSION"):
            self.save({**self.snapshot, "submitted": True})
        snapshot = copy.deepcopy(self.snapshot)
        snapshot.pop("origin_history_sha256")
        with self.assertRaisesRegex(ValueError, "ORIGIN_EVIDENCE_REQUIRED"):
            self.save(snapshot)

    def test_draft_workflow_preserves_not_submitted_without_inventing_origin(self):
        snapshot = {key: value for key, value in self.snapshot.items() if not key.startswith("origin_") and key != "cache_hit"}
        snapshot.update(prompt_id=None, submitted=False)
        saved = self.save(snapshot)
        self.assertFalse(saved["execution"]["submitted"])
        self.assertNotIn("origin_processing_run_id", saved["execution"])
        self.assertNotIn("cache_hit", saved["execution"])

    def test_idempotent_snapshot_never_overwrites_old_wrapper_or_execution(self):
        self.save()
        same = self.save()
        self.assertEqual(same["id"], self.snapshot["workflow_snapshot_id"])
        with self.assertRaisesRegex(ValueError, "ID_CONFLICT"):
            self.save({**self.snapshot, "cache_hit": False})
        rows = [row for row in self.repository.workflows(self.task_id) if row["id"] == self.snapshot["workflow_snapshot_id"]]
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["execution"]["cache_hit"])

    def test_untrusted_graph_credentials_are_rejected_not_redacted_into_fake_snapshot(self):
        self.graph["1"]["inputs"]["api_key"] = "unit-secret-not-a-real-key"
        self.path.write_text(json.dumps(self.graph))
        with self.assertRaisesRegex(ValueError, "CONTAINS_SECRET"):
            self.save({**self.snapshot, "sha256": file_hash(self.path)})

    def test_record_result_uses_shared_snapshot_helper(self):
        self.production.save_snapshot = Mock()
        self.repository.mutate(lambda state: find(state, "tasks", self.task_id)["context"].update(production={}))
        self.repository.mutate(lambda state: state["runs"].append({"id": self.run_id, "task_id": self.task_id, "stage": "cutout",
            "provider": "unit", "status": "running", "progress": 0, "logs": [], "created_at": now()}))
        self.production._record_result(self.task_id, self.run_id, "cutout", {"status": "NOT_CONNECTED", "metadata": {"workflow_snapshot": self.snapshot}}, {},
                                       time.monotonic(), None, None)
        self.production.save_snapshot.assert_called_once_with(self.task_id, self.run_id, self.snapshot)


class TechnicalCompositionSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_technical_composition_saves_snapshot_without_changing_business_task(self):
        repository = review_fixtures.MemoryRepository({"tasks": [{"id": "unit-task", "status": "CREATED", "context": {}, "collector_result": {}}],
            "assets": [], "runs": [{"id": "unit-run", "task_id": "unit-task", "status": "queued", "logs": []}], "reviews": [], "audits": []})
        snapshot = {"workflow_snapshot_id": "unit-wrapper-proof"}
        provider = SimpleNamespace(execute=AsyncMock(return_value={"status": "READY", "assets": [], "metadata": {"workflow_snapshot": snapshot}}))
        production = SimpleNamespace(repository=repository, save_snapshot=Mock(), providers={"compose": provider}, event=AsyncMock())
        service = MatteRepairService(production)
        await service.run_technical_compose("unit-task", "unit-run", {}, Path("/unused-unit-directory"))
        production.save_snapshot.assert_called_once_with("unit-task", "unit-run", snapshot)
        self.assertEqual(repository.state()["tasks"][0]["status"], "CREATED")
        self.assertEqual(repository.state()["tasks"][0]["collector_result"], {})
        self.assertEqual(repository.state()["runs"][0]["status"], "failed")
        self.assertEqual(repository.state()["runs"][0]["error_code"], "TECHNICAL_COMPOSE_OUTPUT_MISSING")


if __name__ == "__main__":
    unittest.main()
