import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app import models
from app.config import Settings
from app.domain import now
from app.main import create_app
from test_real_collector import FakeCollector, RULES


class TaskDetailAPITests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.rules = patch("app.integrations.collector_service.read_production_rules", return_value=RULES)
        self.rules.start()
        self.app = create_app(Settings(app_mode="integration", integration_dry_run=True, storage_root=root / "storage",
            database_url="sqlite:///" + str(root / "isolated.db")), collector_adapter=FakeCollector())
        self.client = TestClient(self.app)
        self.client.__enter__()
        result = self.client.post("/api/actions", json={"action": "create_collection", "payload": {
            "country": "MX", "keyword": "isolated task contract", "product_images": [str(root / "reference.jpg")], "source_row": 3}})
        self.assertEqual(result.status_code, 200, result.text)
        self.task_id = result.json()["task_id"]

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.client.close()
        self.app.state.repository.engine.dispose()
        self.rules.stop()
        self.temporary.cleanup()

    def add_evidence(self):
        def evidence(state):
            asset = {"name": "unit contract fixture", "thumbnail": "", "duration": 1, "width": 10, "height": 20,
                "fps": 30, "codec": "unit", "country": "MX", "keyword": "isolated task contract", "storage_path": "unit-only",
                "created_at": now()}
            run = {"stage": "cutout", "progress": 100, "logs": ["isolated contract, not media execution"], "created_at": now()}
            for suffix, scope in (("normal", None), ("technical", "TECHNICAL_VALIDATION")):
                reference = {"processing_run_id": "run-" + suffix, "parent_asset_id": "source-unit", "execution_scope": scope}
                state["assets"].append({**asset, "id": "asset-" + suffix, "task_id": self.task_id, "type": "person",
                    "registry_status": "registered", "status": "READY", "legacy_reference": reference})
                state["runs"].append({**run, "id": "run-" + suffix, "task_id": self.task_id,
                    "provider": "FactoryTechnicalSAM2" if scope else "LegacyComfyUIAdapter", "status": "success"})
                state["reviews"].append({"id": "review-" + suffix, "task_id": self.task_id, "asset_id": "asset-" + suffix,
                    "status": "pending", "issues": [], "note": "unit contract evidence only"})
                state["quality_assessments"].append({"id": "quality-" + suffix, "task_id": self.task_id, "asset_id": "asset-" + suffix,
                    "processing_run_id": "run-" + suffix, "status": "PASS", "quality_score": 90, "metrics": {}, "issue_segments": [],
                    "fingerprint": "unit-" + suffix, "created_at": now()})
            state["batches"].append({"id": "unit-batch", "status": "COMPLETED_WITH_ERRORS", "task_ids": [self.task_id],
                "items": [{"task_id": self.task_id, "status": "WAITING_REVIEW"}], "active_task_id": None,
                "summary": {}, "created_at": now(), "updated_at": now()})
        self.app.state.repository.mutate(evidence)
        for suffix in ("normal", "technical"):
            self.app.state.repository.save_workflow({"id": "workflow-" + suffix, "task_id": self.task_id, "version": "unit-" + suffix,
                "workflow": {"unit": {"class_type": "UnitContractOnly", "inputs": {}}}, "created_at": now(),
                "processing_run_id": "run-" + suffix, "prompt_id": None, "sha256": "unit-workflow", "execution": {}})

    def test_task_detail_exposes_real_relations_without_technical_evidence(self):
        self.add_evidence()
        response = self.client.get("/api/tasks/" + self.task_id)
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual([row["id"] for row in result["assets"]], ["asset-normal"])
        self.assertEqual([row["id"] for row in result["reviews"]], ["review-normal"])
        self.assertEqual([row["id"] for row in result["quality_assessments"]], ["quality-normal"])
        self.assertEqual([row["id"] for row in result["workflows"]], ["workflow-normal"])
        self.assertEqual(result["batch"]["id"], "unit-batch")
        self.assertEqual(result["lineage"][0]["parent_asset_id"], "source-unit")

    def test_workflow_history_requires_explicit_technical_or_history_scope(self):
        self.add_evidence()
        path = "/api/tasks/" + self.task_id + "/workflows"
        self.assertEqual([row["id"] for row in self.client.get(path).json()], ["workflow-normal"])
        self.assertEqual([row["id"] for row in self.client.get(path + "?scope=technical").json()], ["workflow-technical"])
        self.assertEqual(len(self.client.get(path + "?scope=history").json()), 2)

    def test_metadata_links_cutout_to_normal_person_and_workflow(self):
        self.add_evidence()
        with Session(self.app.state.repository.engine) as session:
            for suffix in ("normal", "technical"):
                session.add(models.CutoutAsset(id="cutout-" + suffix, task_id=self.task_id,
                    asset_id="asset-" + suffix, workflow_id="workflow-" + suffix,
                    model="model-" + suffix, model_version="unit", alpha_quality=90, temporal_stability=90,
                    missing_body_score=0.1 if suffix == "normal" else 0.99,
                    flicker_score=0.2 if suffix == "normal" else 0.98, intervals=[]))
            session.commit()
        path = "/api/tasks/" + self.task_id + "/metadata"
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200, response.text)
        metadata = response.json()
        self.assertEqual(metadata["cutout"]["model"], "model-normal")
        self.assertEqual(metadata["cutout"]["workflow_version"], "unit-normal")
        self.assertEqual(metadata["quality"]["missing_body_score"], 0.1)
        self.assertEqual(metadata["quality"]["mask_flicker_score"], 0.2)
        self.assertEqual(self.client.get(path + "?scope=history").json()["cutout"]["model"], "model-technical")

    def test_metadata_does_not_borrow_technical_cutout_when_normal_person_is_missing(self):
        self.add_evidence()
        def remove_normal(state):
            for row in state["assets"]:
                if row["id"] == "asset-normal":
                    row["legacy_reference"]["execution_scope"] = "TECHNICAL_VALIDATION"
        self.app.state.repository.mutate(remove_normal)
        with Session(self.app.state.repository.engine) as session:
            session.add(models.CutoutAsset(id="cutout-technical", task_id=self.task_id,
                asset_id="asset-technical", workflow_id="workflow-technical", model="technical-only", model_version="unit",
                alpha_quality=90, temporal_stability=90, missing_body_score=0.99, flicker_score=0.98, intervals=[]))
            session.commit()
        response = self.client.get("/api/tasks/" + self.task_id + "/metadata")
        self.assertEqual(response.status_code, 200, response.text)
        metadata = response.json()
        self.assertIsNone(metadata["cutout"]["model"])
        self.assertIsNone(metadata["cutout"]["workflow_version"])
        self.assertIsNone(metadata["quality"]["missing_body_score"])


if __name__ == "__main__":
    unittest.main()
