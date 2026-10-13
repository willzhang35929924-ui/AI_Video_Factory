import asyncio
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.integrations.contracts import FactoryCollectorRules, TaskContext
from app.config import Settings
from app.integrations.collector_service import CollectorService
from app.integrations.input_routes import create_input_router
from app.integrations.input_service import CollectorInputService
from app.integrations.legacy.collector_worker import collector_result, single_task_pipeline
from app.integrations.production_rules import (
    factory_metric_gate, factory_presence_gate, factory_rule_snapshot,
    read_factory_rules, save_factory_rules, verified_factory_rules,
)
from app.repository import StateRepository


VALUES = {"min_views": 100, "min_likes": 20, "product_match_min_confidence": .85,
          "overlay_requirement": "any", "watermark_requirement": "any", "subtitle_requirement": "any"}


class FactoryRuleTests(unittest.TestCase):
    def test_ratio_is_derived_and_uneditable(self):
        snapshot = FactoryCollectorRules.model_validate(VALUES).snapshot()
        self.assertEqual(snapshot["rules"]["min_like_view_ratio"], .2)
        self.assertIsNone(factory_metric_gate(snapshot["rules"], 20, 100))
        self.assertEqual(factory_metric_gate(snapshot["rules"], 20, 1000), ("rejected", "factory_metric_threshold"))
        self.assertEqual(factory_rule_snapshot({**VALUES, "min_views": 0})["rules"]["min_like_view_ratio"], 0)
        with self.assertRaises(ValidationError):
            FactoryCollectorRules.model_validate({**VALUES, "min_like_view_ratio": .001})

    def test_strict_inputs_do_not_accept_result_labels_or_secrets(self):
        for invalid in ({"min_views": "100"}, {"min_likes": True}, {"min_likes": -1},
                        {"watermark_requirement": "PASS"}, {"api_key": "never-save"},
                        {"product_match_min_confidence": 1.5}):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                FactoryCollectorRules.model_validate({**VALUES, **invalid})

    def test_presence_is_three_state_and_unknown_never_passes(self):
        rules = factory_rule_snapshot(VALUES)["rules"]
        self.assertIsNone(factory_presence_gate(rules, {}))
        required = {**rules, "watermark_requirement": "absent"}
        self.assertEqual(factory_presence_gate(required, {}), ("manual_review", "factory_detection_unverified"))
        self.assertIsNone(factory_presence_gate(required, {"watermark_detected": False}))
        self.assertEqual(factory_presence_gate(required, {"watermark_detected": True}), ("rejected", "factory_watermark_requirement"))
        self.assertIsNone(factory_presence_gate({**rules, "subtitle_requirement": "present"}, {"subtitle_detected": True}))

    def test_default_does_not_weaken_legacy_ratio(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = SimpleNamespace(storage_root=Path(directory))
            default = read_factory_rules(settings, {"rules": {"min_views": 100, "min_like_view_ratio": .001,
                "product_match_min_confidence": .85}})
            self.assertFalse(default["persisted"])
            self.assertEqual(default["snapshot"]["rules"]["min_likes"], 1)
            self.assertGreaterEqual(default["snapshot"]["rules"]["min_like_view_ratio"], .001)

    def test_fingerprint_binds_thresholds_and_presence_requirements(self):
        snapshot = factory_rule_snapshot(VALUES)
        altered = {**snapshot, "rules": {**snapshot["rules"], "watermark_requirement": "absent"}}
        with self.assertRaises(ValueError):
            verified_factory_rules(altered)
        self.assertNotEqual(snapshot["sha256"], factory_rule_snapshot({**VALUES, "min_likes": 21})["sha256"])

    def test_saved_rules_reload_and_create_batch_carries_snapshot_input(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = SimpleNamespace(storage_root=Path(directory), legacy_tiktok_auto_root=Path(directory),
                legacy_tiktok_auto_python=Path("/not-invoked"))
            collector = SimpleNamespace(create=AsyncMock(return_value={"task_id": "unit-only"}))
            app = FastAPI()
            app.include_router(create_input_router(collector, settings))
            with TestClient(app) as client:
                response = client.put("/api/collector/rules", json=VALUES)
                self.assertEqual(response.status_code, 200, response.text)
                saved = response.json()
                self.assertTrue(saved["persisted"])
                self.assertEqual(client.get("/api/collector/rules").json()["snapshot"], saved["snapshot"])
                self.assertEqual(client.put("/api/collector/rules", json={**VALUES, "api_key": "never-save"}).status_code, 422)
            self.assertNotIn("never-save", (Path(directory) / "collector_inputs/rules-production.json").read_text())
            service = CollectorInputService(collector, settings)
            image = service.root / "product.png"
            image.write_bytes(b"unit-only")
            result = asyncio.run(service.create_batch([{"country": "US", "keyword": "lipstick", "product_images": [str(image)]}], VALUES))
            self.assertFalse(result["started"])
            self.assertEqual(collector.create.call_args.args[0]["factory_rules"], VALUES)

    def test_legacy_context_dump_retains_identity_without_new_null_fields(self):
        original = {"task_id": "one", "project_id": "project", "country": "US", "keyword": "lipstick",
            "product_id": "product", "product_images": ["/unit/reference.png"], "source_document": "manual",
            "source_row": 1, "context_id": "unit-context"}
        self.assertEqual(TaskContext.model_validate(original).model_dump(exclude_none=True), original)

    def test_task_rules_are_immutable_persisted_and_do_not_update_old_tasks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = Settings(app_mode="integration", database_url="sqlite:///" + str(root / "test.db"), storage_root=root / "storage")
            seed = Path(__file__).parents[1] / "app/seed.json"
            repository = StateRepository(settings.database_url, seed)
            legacy = {"source": "/unit/legacy/config.yaml", "sha256": "unit-config", "read_only": True,
                "rules": {"min_views": 100, "min_like_view_ratio": .001, "product_match_min_confidence": .85}}
            previous = repository.state()["tasks"]
            service = CollectorService(repository, SimpleNamespace(broadcast=AsyncMock()), settings)
            with patch("app.integrations.collector_service.read_production_rules", return_value=legacy):
                created = asyncio.run(service.create({"country": "US", "keyword": "lipstick", "product_images": [str(root / "unit.png")],
                    "factory_rules": VALUES}))
            task = next(row for row in repository.state()["tasks"] if row["id"] == created["task_id"])
            immutable = task["context"]["task_context"]["factory_rules"]
            save_factory_rules(settings, {**VALUES, "min_likes": 50})
            self.assertEqual(service.context(created["task_id"])["factory_rules"], immutable)
            self.assertEqual(task["context"]["production_rules"], legacy)
            self.assertEqual([row for row in repository.state()["tasks"] if row["id"] != created["task_id"]], previous)
            repository.engine.dispose()
            reopened = StateRepository(settings.database_url, seed)
            try:
                persisted = next(row for row in reopened.state()["tasks"] if row["id"] == created["task_id"])
                self.assertEqual(persisted["context"]["task_context"]["factory_rules"], immutable)
                collector_row = next(row for row in reopened.state()["collector"] if row["last_task_id"] == created["task_id"])
                self.assertEqual((collector_row["min_views"], collector_row["min_likes"], collector_row["min_ratio"]), (100, 20, 20.0))
            finally:
                reopened.engine.dispose()


class FactoryPrepaidGateTests(unittest.TestCase):
    def pipeline(self, rules, likes, views, detected=None):
        class LegacyPipeline:
            def __init__(self, *_args):
                self.config = {"min_views": 100, "min_like_view_ratio": .001, "product_match_min_confidence": .85}
                self.ai_calls = self.download_calls = self.closed = 0

            def _config_fingerprint(self):
                return hashlib.sha256(json.dumps(self.config, sort_keys=True).encode()).hexdigest()

            def _set_metrics(self, row, metrics):
                row.update(likes=metrics.likes, views=metrics.views, metric_source="ISOLATED_TEST",
                           metric_evidence="real metric boundary contract")

            def process_hit(self, *_args):
                row = dict(detected or {})
                try:
                    self._set_metrics(row, SimpleNamespace(likes=likes, views=views))
                    self.ai_calls += 1
                    self.download_calls += 1
                    row.update(decision="accepted", reason="unit-only-success")
                except Exception as exc:
                    row.update(decision="manual_review", reason="processing_error", error=str(exc))
                finally:
                    self.closed += 1
                return row
        return single_task_pipeline(LegacyPipeline, Path("/unit-only"), factory_rule_snapshot(rules))

    def test_min_likes_gate_before_any_ai_or_download_and_preserves_metrics(self):
        pipeline = self.pipeline(VALUES, 19, 100)
        row = pipeline.process_hit(None, None, None, {})
        self.assertEqual((pipeline.ai_calls, pipeline.download_calls, pipeline.closed), (0, 0, 1))
        self.assertEqual((row["decision"], row["reason"]), ("rejected", "factory_metric_threshold"))
        self.assertEqual((row["likes"], row["views"], row["metric_source"]), (19, 100, "ISOLATED_TEST"))
        self.assertNotEqual(row["reason"], "processing_error")

    def test_unknown_required_detection_waits_before_any_ai_or_download(self):
        pipeline = self.pipeline({**VALUES, "watermark_requirement": "absent"}, 20, 100)
        row = pipeline.process_hit(None, None, None, {})
        self.assertEqual((pipeline.ai_calls, pipeline.download_calls, pipeline.closed), (0, 0, 1))
        self.assertEqual((row["decision"], row["reason"]), ("manual_review", "factory_detection_unverified"))
        result = collector_result({"task_id": "unit-only"}, row, Path("/unit-only"), Path("/unit-only/result.json"))
        self.assertEqual(result["status"], "WAITING_REVIEW")
        self.assertIsNone(result["watermark_detected"])

    def test_verified_detection_and_metrics_continue_through_existing_pipeline(self):
        pipeline = self.pipeline({**VALUES, "watermark_requirement": "absent"}, 20, 100, {"watermark_detected": False})
        row = pipeline.process_hit(None, None, None, {})
        self.assertEqual((pipeline.ai_calls, pipeline.download_calls), (1, 1))
        self.assertEqual(row["decision"], "accepted")


if __name__ == "__main__":
    unittest.main()
