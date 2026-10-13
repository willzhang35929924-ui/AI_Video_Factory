import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.integrations.asset_registry import file_hash
from app.main import create_app
from app.repository import StateRepository


class AssetRegistryRouteTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="factory-asset-route-unit-")
        self.root = Path(self.temporary.name)
        self.config = Settings(app_mode="integration", integration_dry_run=True,
            database_url="sqlite:///" + str(self.root / "factory.db"), storage_root=self.root / "storage")
        self.config.storage_root.mkdir()
        fixture = StateRepository(self.config.database_url, Path(__file__).parents[1] / "app/seed.json")
        paths = {}
        for name in ("source", "person", "alpha", "preview", "background", "compose"):
            path = self.config.storage_root / (name + ".unit")
            path.write_bytes(b"UNIT_FIXTURE_NOT_REAL_MEDIA_" + name.encode())
            paths[name] = path

        def add(state):
            task = {**deepcopy(state["tasks"][0]), "id": "unit-assets-task", "execution_mode": "integration",
                "source_video_id": None, "context": {"task_context": {"task_id": "unit-assets-task", "project_id": state["projects"][0]["id"],
                    "country": "巴西", "keyword": "unit-only", "product_id": state["products"][0]["id"], "product_images": [],
                    "source_document": "UNIT_TEST_ONLY", "source_row": 2, "context_id": "unit-context"}}, "collector_result": {}}
            state["tasks"].append(task)
            template = deepcopy(state["assets"][0])
            for name in ("source", "person", "background", "compose"):
                path = paths[name]
                reference = {"version": 1, "processing_run_id": "unit-" + name + "-run"}
                if name == "person":
                    reference.update(source_sha256=file_hash(paths["source"]), alpha_path=str(paths["alpha"]),
                        alpha_sha256=file_hash(paths["alpha"]), preview_path=str(paths["preview"]))
                if name == "compose":
                    reference.update(person_asset_id="unit-person", background_asset_id="unit-background")
                state["assets"].append({**template, "id": "unit-" + name, "task_id": task["id"], "type": name,
                    "asset_type": {"source": "SOURCE_VIDEO", "person": "PERSON_VIDEO", "background": "BACKGROUND_IMAGE", "compose": "COMPOSITION_VIDEO"}[name],
                    "name": path.name, "registry_status": "registered", "storage_path": str(path), "external_path": str(path),
                    "sha256": file_hash(path), "size": path.stat().st_size, "source_system": "UNIT_TEST_ONLY", "legacy_reference": reference})
                state["runs"].append({"id": reference["processing_run_id"], "task_id": task["id"], "stage": "collector", "status": "success",
                    "progress": 100, "logs": ["UNIT_TEST_ONLY"], "created_at": task["created_at"], "provider": "UnitFixture"})
            source = deepcopy(state["assets"][-4])
            state["assets"].append({**source, "id": "unit-missing", "sha256": "b" * 64, "external_path": str(self.config.storage_root / "missing.mp4")})
            state["assets"].append({**source, "id": "unit-candidate", "type": "source_candidate", "asset_type": "SOURCE_CANDIDATE", "width": 0, "height": 0})
            state["assets"].append({**source, "id": "unit-outside", "sha256": "c" * 64, "external_path": str(self.root / "forbidden.mp4")})
            state["assets"].append({**source, "id": "unit-technical", "legacy_reference": {"execution_scope": "TECHNICAL_VALIDATION"}})

        fixture.mutate(add)
        fixture.save_workflow({"id": "unit-person-workflow", "task_id": "unit-assets-task", "version": "UNIT_TEST_ONLY",
            "processing_run_id": "unit-person-run", "workflow": {"scope": "UNIT_TEST_ONLY"}, "sha256": "a" * 64})
        fixture.engine.dispose()
        self.app = create_app(self.config)
        self.client = TestClient(self.app)

    def tearDown(self):
        self.client.close()
        self.app.state.repository.engine.dispose()
        self.temporary.cleanup()

    def test_production_media_status_excludes_mock_and_technical_without_mutation(self):
        before = json.dumps(self.app.state.repository.state(), sort_keys=True)
        rows = self.client.get("/api/assets/media-status").json()
        self.assertNotIn("unit-technical", {row["asset_id"] for row in rows})
        self.assertTrue(all(row["asset_id"].startswith("unit-") for row in rows))
        by_id = {row["asset_id"]: row for row in rows}
        self.assertEqual(by_id["unit-source"]["status"], "FILE_PRESENT")
        self.assertEqual(by_id["unit-source"]["hash_check"], "NOT_RECHECKED")
        self.assertEqual(by_id["unit-missing"]["status"], "OUTPUT_NOT_FOUND")
        self.assertFalse(by_id["unit-missing"]["available"])
        self.assertEqual(by_id["unit-candidate"]["status"], "SOURCE_CANDIDATE")
        self.assertFalse(by_id["unit-candidate"]["available"])
        self.assertEqual(by_id["unit-outside"]["status"], "PATH_NOT_ALLOWED")
        self.assertEqual(json.dumps(self.app.state.repository.state(), sort_keys=True), before)

    def test_registry_returns_explicit_parent_lineage_context_and_exact_workflow(self):
        response = self.client.get("/api/assets/unit-compose/registry")
        self.assertEqual(response.status_code, 200, response.text)
        record = response.json()
        self.assertEqual(record["context_id"], "unit-context")
        self.assertEqual(record["context"]["source_row"], 2)
        self.assertEqual(record["version"], 1)
        self.assertEqual({row["id"] for row in record["lineage"]["assets"]}, {"unit-source", "unit-person", "unit-background", "unit-compose"})
        self.assertEqual([row["id"] for row in record["workflows"]], ["unit-person-workflow"])
        self.assertIn({"from_asset_id": "unit-person", "to_asset_id": "unit-compose", "resolved": True}, record["lineage"]["edges"])
        source = self.client.get("/api/assets/unit-source/registry").json()
        self.assertEqual([row["id"] for row in source["lineage"]["children"]], ["unit-person"])

    def test_source_runs_use_exact_checkpoint_and_review_binding_without_mutation(self):
        def bind(state):
            task = next(row for row in state["tasks"] if row["id"] == "unit-assets-task")
            source = next(row for row in state["assets"] if row["id"] == "unit-source")
            source["legacy_reference"].pop("processing_run_id")
            run_template = next(row for row in state["runs"] if row["id"] == "unit-source-run")
            for identifier in ("unit-review-verification", "unit-wrong-hash", "unit-wrong-asset", "unit-unrelated-latest"):
                state["runs"].append({**deepcopy(run_template), "id": identifier, "run_id": identifier})
            state["runs"].append({**deepcopy(run_template), "id": "unit-other-task-run", "run_id": "unit-other-task-run", "task_id": state["tasks"][0]["id"]})
            checkpoint = {"source_asset_id": source["id"], "sha256": source["sha256"], "verification_run_id": "unit-source-run"}
            task["context"]["download_checkpoints"] = [checkpoint,
                {**checkpoint, "verification_run_id": "unit-missing-run"},
                {**checkpoint, "verification_run_id": "unit-other-task-run"},
                {**checkpoint, "sha256": "f" * 64, "verification_run_id": "unit-wrong-hash"},
                {**checkpoint, "source_asset_id": "unit-missing", "verification_run_id": "unit-wrong-asset"}]
            task["context"]["download_checkpoint"] = checkpoint
            review = {"asset_id": source["id"], "status": "approved", "evidence": {
                "source_asset_id": source["id"], "source_sha256": source["sha256"], "origin_collector_run_id": "unit-source-run"},
                "verification": {"verification_run_id": "unit-review-verification", "sha256": "a" * 64}}
            task["context"]["source_watermark_reviews"] = [review,
                {**review, "asset_id": "unit-missing", "verification": {"verification_run_id": "unit-wrong-asset"}},
                {**review, "evidence": {**review["evidence"], "source_sha256": "f" * 64}, "verification": {"verification_run_id": "unit-wrong-hash"}},
                {**review, "evidence": {**review["evidence"], "source_asset_id": "unit-missing"}, "verification": {"verification_run_id": "unit-wrong-asset"}}]

        self.app.state.repository.mutate(bind)
        before = json.dumps(self.app.state.repository.state(), sort_keys=True)
        source = self.client.get("/api/assets/unit-source/registry?scope=history").json()
        self.assertEqual({row["id"] for row in source["runs"]}, {"unit-source-run", "unit-review-verification"})
        composition = self.client.get("/api/assets/unit-compose/registry").json()
        self.assertEqual({row["id"] for row in composition["runs"]}, {
            "unit-source-run", "unit-review-verification", "unit-person-run", "unit-background-run", "unit-compose-run"})
        self.assertEqual(json.dumps(self.app.state.repository.state(), sort_keys=True), before)

    def test_source_run_can_be_bound_by_single_checkpoint_only(self):
        def bind(state):
            task = next(row for row in state["tasks"] if row["id"] == "unit-assets-task")
            source = next(row for row in state["assets"] if row["id"] == "unit-source")
            source["legacy_reference"].pop("processing_run_id")
            task["context"]["download_checkpoint"] = {"source_asset_id": source["id"], "sha256": source["sha256"],
                "verification_run_id": "unit-source-run"}

        self.app.state.repository.mutate(bind)
        record = self.client.get("/api/assets/unit-source/registry").json()
        self.assertEqual([row["id"] for row in record["runs"]], ["unit-source-run"])

    def test_history_is_explicit_and_invalid_scope_or_id_fails(self):
        self.assertEqual(self.client.get("/api/assets/unit-technical/registry").status_code, 404)
        self.assertEqual(self.client.get("/api/assets/unit-technical/registry?scope=technical").json()["execution_scope"], "TECHNICAL_VALIDATION")
        self.assertEqual(self.client.get("/api/assets/ast-1002-2/registry").status_code, 404)
        self.assertEqual(self.client.get("/api/assets/ast-1002-2/registry?scope=history").json()["execution_scope"], "MOCK")
        self.assertEqual(self.client.get("/api/assets/no-such/registry").status_code, 404)
        self.assertEqual(self.client.get("/api/assets/media-status?scope=bad").status_code, 422)

    def test_missing_person_preview_does_not_claim_preview_available(self):
        (self.config.storage_root / "preview.unit").unlink()
        record = self.client.get("/api/assets/unit-person/registry").json()
        self.assertTrue(record["media"]["available"])
        self.assertFalse(record["media"]["preview_available"])
        self.assertTrue(record["media"]["alpha"]["exists"])

    def test_size_changed_file_does_not_claim_available_or_modify_registration(self):
        path = self.config.storage_root / "source.unit"
        path.write_bytes(b"UNIT_CHANGED_SIZE")
        record = self.client.get("/api/assets/unit-source/registry").json()
        self.assertEqual(record["media"]["status"], "SIZE_MISMATCH")
        self.assertFalse(record["media"]["available"])
        self.assertNotEqual(record["media"]["size"], record["asset"]["size"])
        self.assertEqual(record["asset"]["registry_status"], "registered")
