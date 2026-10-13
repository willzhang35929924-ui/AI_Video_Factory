import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.repository import StateRepository


SEED = Path(__file__).parents[1] / "app/seed.json"


class ProductionStateScopeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.config = Settings(app_mode="integration", integration_dry_run=True,
            database_url="sqlite:///" + str(self.root / "factory.db"), storage_root=self.root / "storage")

    def tearDown(self):
        self.directory.cleanup()

    def mixed_repository(self):
        repository = StateRepository(self.config.database_url, SEED)

        def add(state):
            product = {**deepcopy(state["products"][0]), "id": "unit-real-product", "image": str(self.root / "reference.png")}
            task = {**deepcopy(state["tasks"][0]), "id": "unit-real-task", "product_id": product["id"],
                "source_video_id": None, "execution_mode": "integration", "status": "WAITING_REVIEW", "context": {}}
            state["products"].append(product)
            state["tasks"].append(task)
            source = {**deepcopy(next(row for row in state["assets"] if row["type"] == "source")),
                "id": "unit-source", "task_id": task["id"], "registry_status": "registered", "source_system": "UNIT_TEST_ONLY"}
            candidate = {**source, "id": "unit-missing", "registry_status": "missing"}
            technical = {**source, "id": "unit-technical", "type": "person", "source_system": "FactoryTechnicalValidation",
                "legacy_reference": {"execution_scope": "TECHNICAL_VALIDATION", "processing_run_id": "unit-tech-run"}}
            state["assets"].extend([source, candidate, technical])
            for asset in (source, technical):
                state["reviews"].append({"id": "review-" + asset["id"], "task_id": task["id"], "asset_id": asset["id"],
                    "status": "pending", "issues": [], "note": "UNIT_TEST_ONLY"})
            task["context"] = {"technical_validation": {"current_person_asset_id": technical["id"]}, "matte_repairs": [
                {"id": "unit-tech-repair", "run_id": "unit-repair-run", "person_asset_id": technical["id"], "execution_scope": "TECHNICAL_VALIDATION", "status": "APPLIED"},
                {"id": "unit-real-repair", "run_id": "unit-real-repair-run", "person_asset_id": source["id"], "execution_scope": "PRODUCTION", "status": "APPLIED"}]}
            for run_id, provider in (("unit-business-run", "LegacyCollectorAdapter"), ("unit-tech-run", "FactoryTechnicalRegistration"),
                                     ("unit-repair-run", "FactoryMatteRepair"), ("unit-quality-run", "CutoutQualityAnalyzer"),
                                     ("unit-real-repair-run", "FactoryMatteRepair")):
                state["runs"].append({"id": run_id, "task_id": task["id"], "stage": "collector", "status": "success", "progress": 100,
                    "logs": ["UNIT_TEST_ONLY"], "created_at": task["created_at"], "provider": provider})
            state["quality_assessments"].append({"id": "unit-tech-quality", "task_id": task["id"], "asset_id": technical["id"],
                "processing_run_id": "unit-quality-run", "status": "FAIL", "quality_score": 50, "metrics": {},
                "issue_segments": [], "fingerprint": "UNIT_TEST_ONLY", "created_at": task["created_at"]})
            for ordinal, status in ((1, "READY"), (2, "MOCK")):
                state["prompts"].append({**deepcopy(state["prompts"][0]), "id": "unit-prompt-" + str(ordinal),
                    "task_id": task["id"], "ordinal": ordinal, "status": status})
            state["collector"].append({**deepcopy(state["collector"][0]), "id": "unit-collector", "product_id": product["id"],
                "last_task_id": task["id"], "status": "WAITING_REVIEW"})
            for entity, action in ((technical["id"], "technical.review.approve"), ("unit-tech-repair", "repair.applied"), (source["id"], "source.registered")):
                state["audits"].append({"id": "audit-" + entity, "task_id": task["id"], "entity_id": entity, "action": action,
                    "message": "UNIT_TEST_ONLY", "created_at": task["created_at"]})

        repository.mutate(add)
        return repository

    def test_new_integration_database_has_no_business_demo_records(self):
        app = create_app(self.config)
        with TestClient(app) as client:
            state = client.get("/api/state").json()
            self.assertEqual(state["projects"], [{"id": "proj-local", "name": "默认项目"}])
            self.assertEqual(state["settings"]["theme"], "light")
            for key in ("tasks", "products", "accounts", "assets", "reviews", "prompts", "runs", "collector", "publishing"):
                self.assertEqual(state[key], [], key)

    def test_production_projection_is_read_only_and_preserves_source_candidate_history(self):
        fixture = self.mixed_repository()
        before = json.dumps(fixture.state(), sort_keys=True)
        fixture.engine.dispose()
        app = create_app(self.config)
        with TestClient(app) as client:
            raw = client.get("/api/state").json()
            production = client.get("/api/state?scope=production").json()
            self.assertEqual([row["id"] for row in production["tasks"]], ["unit-real-task"])
            self.assertEqual({row["id"] for row in production["assets"]}, {"unit-source", "unit-missing"})
            self.assertEqual([row["id"] for row in production["products"]], ["unit-real-product"])
            self.assertEqual(production["accounts"], [])
            self.assertEqual(production["publishing"], [])
            self.assertEqual([row["id"] for row in production["reviews"]], ["review-unit-source"])
            self.assertEqual(production["quality_assessments"], [])
            self.assertEqual({row["id"] for row in production["runs"]}, {"unit-business-run", "unit-real-repair-run"})
            self.assertEqual([row["id"] for row in production["prompts"]], ["unit-prompt-1"])
            self.assertNotIn("technical_validation", production["tasks"][0]["context"])
            self.assertEqual([row["id"] for row in production["tasks"][0]["context"]["matte_repairs"]], ["unit-real-repair"])
            self.assertEqual([row["entity_id"] for row in production["audits"]], ["unit-source"])
            self.assertEqual(json.dumps(raw, sort_keys=True), before)
            self.assertEqual(client.get("/api/state?scope=history").json(), raw)
            self.assertEqual(client.get("/api/state?scope=technical").json(), raw)
            self.assertEqual(client.get("/api/state?scope=not-valid").status_code, 422)
            self.assertEqual(json.dumps(app.state.repository.state(), sort_keys=True), before)

    def test_public_lists_are_production_scoped_but_publish_is_raw(self):
        fixture = self.mixed_repository()
        fixture.engine.dispose()
        with TestClient(create_app(self.config)) as client:
            self.assertEqual([row["id"] for row in client.get("/api/tasks").json()], ["unit-real-task"])
            self.assertEqual(len(client.get("/api/assets").json()), 2)
            self.assertEqual(client.get("/api/accounts").json(), [])
            self.assertEqual(len(client.get("/api/publish").json()), 2)
            self.assertEqual(len(client.get("/api/products?scope=raw").json()), 6)
            self.assertEqual(len(client.get("/api/cutout").json()), 1)
            self.assertEqual(client.get("/api/cutout").json()[0]["assets"], [])
            self.assertEqual(client.get("/api/background").json()["assets"], [])
            self.assertEqual(client.get("/api/compose").json()["assets"], [])

    def test_integration_mock_business_actions_are_blocked_without_mutation(self):
        fixture = self.mixed_repository()
        fixture.engine.dispose()
        with TestClient(create_app(self.config)) as client:
            before = client.get("/api/state").json()
            for action in ("start_cutout", "retry_interval", "regenerate_mask", "manual_fix", "accept_cutout", "generate_prompt",
                           "generate_background", "upload_background", "compose", "update_composition", "pause_task", "retry_task",
                           "review_approve", "review_reject", "review_retry", "review_manual", "authorize_account", "update_account_status"):
                response = client.post("/api/actions", json={"action": action, "entity_id": "acc-001" if "account" in action else "tsk-1001"})
                self.assertEqual(response.status_code, 409, action)
                self.assertIn("MOCK_ACTION_ISOLATED", response.text)
            self.assertEqual(client.get("/api/state").json(), before)

    def test_publish_mock_caption_and_original_seed_relations_are_unchanged(self):
        fixture = self.mixed_repository()
        fixture.engine.dispose()
        with TestClient(create_app(self.config)) as client:
            before = client.get("/api/state").json()
            response = client.post("/api/actions", json={"action": "generate_caption", "entity_id": "pub-001"})
            self.assertEqual(response.status_code, 200, response.text)
            after = client.get("/api/state").json()
            for key in ("tasks", "assets", "accounts", "products", "reviews"):
                self.assertEqual(after[key], before[key], key)
            self.assertEqual(len(after["publishing"]), len(before["publishing"]))
            self.assertEqual(client.get("/api/state?scope=production").json()["publishing"], [])
            health = client.get("/api/health").json()
            self.assertEqual(health["publishing_backend"], "CONTROLLED_UPLOAD")
            self.assertEqual(health["publishing_production_readiness"], "NOT_PRODUCTION_READY")
            self.assertEqual(health["comfyui"], "UNKNOWN")

    def test_integration_startup_does_not_recover_mock_history(self):
        fixture = self.mixed_repository()
        fixture.engine.dispose()
        app = create_app(self.config)
        with patch.object(app.state.service.runtime, "recover", new_callable=AsyncMock) as recover:
            with TestClient(app) as client:
                run = next(row for row in client.get("/api/state").json()["runs"] if row["id"] == "run-demo")
                self.assertEqual(run["status"], "running")
                recover.assert_not_awaited()
            recover.assert_not_awaited()
        restarted = StateRepository(self.config.database_url, SEED, app_mode="integration")
        self.assertEqual(next(row for row in restarted.state()["runs"] if row["id"] == "run-demo")["status"], "running")
        restarted.engine.dispose()

    def test_mock_mode_retains_seed_and_production_scope_fixture(self):
        config = Settings(app_mode="mock", database_url="sqlite:///" + str(self.root / "mock.db"), storage_root=self.root / "storage")
        app = create_app(config)
        with patch.object(app.state.service.runtime, "recover", new_callable=AsyncMock) as recover:
            with TestClient(app) as client:
                self.assertEqual(client.get("/api/state?scope=production").json(), client.get("/api/state").json())
                self.assertEqual(len(client.get("/api/tasks").json()), 6)
                self.assertEqual(len(client.get("/api/accounts").json()), 4)
                recover.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
