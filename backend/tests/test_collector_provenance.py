import asyncio
import copy
import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.integrations.collector_service import CollectorService
from app.integrations.contracts import CollectorResult
from app.integrations.legacy.collector_worker import (
    cached_observations, collector_result, emit_metrics, execute, regate_cached_row,
    single_task_pipeline, write_json,
)
from app.integrations.production_rules import factory_rule_snapshot
from app.repository import StateRepository


RULES = {"min_views": 100, "min_likes": 20, "product_match_min_confidence": .85,
    "overlay_requirement": "any", "watermark_requirement": "any", "subtitle_requirement": "any"}


class CollectorProvenanceTests(unittest.TestCase):
    def row(self):
        return {"checkpoint_key": "context:123", "video_id": "123", "url": "https://www.tiktok.com/@one/video/123",
            "likes": "20", "views": "100", "processed_at": "2026-10-07T00:00:00+00:00",
            "metric_source": "TikTok-page-data", "metric_evidence": "video_id=123; stats.diggCount/stats.playCount",
            "reference_product_match": "pass", "reference_product_confidence": ".92", "analysis_status": "completed",
            "ai_origin": "likely_real", "cutout_suitability": "pass", "decision": "accepted", "reason": "accepted"}

    def pipeline(self, row, fresh=False):
        class LegacyPipeline:
            def __init__(self, *_args):
                self.config = {}
                self.calls = 0

            def _config_fingerprint(self):
                return "unit-config"

            def _checkpoint_key(self, *_args):
                return "context:123"

            def _set_metrics(self, target, metrics):
                target.update(likes=metrics.likes, views=metrics.views)

            def process_hit(self, browser, spec, hit, checkpoint):
                self.calls += 1
                if not fresh:
                    return checkpoint["context:123"]
                result = dict(row)
                self._set_metrics(result, SimpleNamespace(likes=20, views=100))
                return result

        return single_task_pipeline(LegacyPipeline, Path("/unit/legacy"), factory_rule_snapshot(RULES))

    def invoke(self, pipeline, checkpoint):
        return pipeline.process_hit(None, SimpleNamespace(keyword="body", context_id="context"), SimpleNamespace(video_id="123"), checkpoint)

    def test_actual_metrics_boundary_is_fresh_even_when_task_identity_is_reused(self):
        old = self.row()
        row = self.invoke(self.pipeline(old, fresh=True), {"context:123": old})
        metrics = row["metrics_provenance"]
        self.assertEqual(metrics["mode"], "fresh")
        self.assertRegex(metrics["observed_at"], r"^\d{4}-\d{2}-\d{2}T")
        self.assertIsNone(metrics["reused_at"])
        self.assertEqual(row["semantic_provenance"]["mode"], "cached", "新指标不代表重新执行付费语义分析")
        self.assertNotIn("metrics_provenance", old, "不能修改历史 checkpoint")

    def test_legacy_skip_is_cached_and_old_processed_time_is_not_claimed_as_observed(self):
        old = self.row()
        row = self.invoke(self.pipeline(old), {"context:123": old})
        self.assertEqual(row["metrics_provenance"]["mode"], "cached")
        self.assertIsNone(row["metrics_provenance"]["observed_at"])
        self.assertEqual(row["metrics_provenance"]["original_checkpoint"]["processed_at"], old["processed_at"])
        self.assertTrue(row["metrics_provenance"]["reused_at"])
        self.assertEqual(row["decision"], "accepted")
        self.assertNotIn("metrics_provenance", old)

    def test_stricter_cached_metrics_reject_before_any_existing_pipeline_or_paid_request(self):
        for changes in ({"likes": "19"}, {"views": "99"}, {"views": "1000"}):
            with self.subTest(changes=changes):
                old = {**self.row(), **changes}
                before = copy.deepcopy(old)
                pipeline = self.pipeline(old)
                row = self.invoke(pipeline, {"context:123": old})
                self.assertEqual((row["decision"], row["reason"]), ("rejected", "factory_metric_threshold"))
                self.assertEqual(pipeline.calls, 0)
                self.assertEqual(row["metrics_provenance"]["mode"], "cached")
                self.assertEqual(old, before)

    def test_cached_product_confidence_and_unknown_detection_are_regated_without_ai(self):
        low = regate_cached_row({**self.row(), "reference_product_confidence": ".8"}, factory_rule_snapshot(RULES))
        self.assertEqual((low["decision"], low["reason"]), ("rejected", "factory_product_match_threshold"))
        unknown = regate_cached_row(self.row(), factory_rule_snapshot({**RULES, "watermark_requirement": "absent"}))
        self.assertEqual((unknown["decision"], unknown["reason"]), ("manual_review", "factory_detection_unverified"))
        missing = regate_cached_row({**self.row(), "likes": ""}, factory_rule_snapshot(RULES))
        self.assertEqual((missing["decision"], missing["reason"]), ("manual_review", "factory_metrics_unverified"))
        semantic_confidence = regate_cached_row({**self.row(), "product_match_confidence": .1}, factory_rule_snapshot(RULES))
        self.assertEqual(semantic_confidence["decision"], "accepted", "旧生产门槛使用参考图匹配置信度，不能用语义置信度改变规则")

    def test_old_rejection_is_not_promoted_by_looser_rules_and_unknown_paid_result_stays_frozen(self):
        original = {**self.row(), "decision": "rejected", "reason": "product_reference_mismatch"}
        checked = regate_cached_row(original, factory_rule_snapshot(RULES))
        self.assertEqual((checked["decision"], checked["reason"]), ("rejected", "product_reference_mismatch"))
        unknown = {**self.row(), "analysis_status": "response_unknown"}
        self.assertEqual(regate_cached_row(unknown, factory_rule_snapshot({**RULES, "min_views": 100000})), unknown)

    def test_cached_metrics_event_retains_original_observation_and_checkpoint_without_fake_refresh(self):
        observed = "2026-10-07T10:00:00+00:00"
        original = {**self.row(), "metrics_provenance": {"mode": "fresh", "observed_at": observed,
            "reused_at": None, "original_checkpoint": {"checkpoint_key": "context:123", "raw_result_path": "/unit/origin/collector-legacy-row.json"}}}
        row = cached_observations(original, Path("/unit/legacy"), Path("/unit/newrun/collector-legacy-row.json"))
        output = io.StringIO()
        with redirect_stdout(output):
            emit_metrics(row)
        event = json.loads(output.getvalue().removeprefix("FACTORY_EVENT "))
        self.assertEqual(event["type"], "METRICS_UPDATED")
        self.assertEqual((event["likes"], event["views"]), (20, 100))
        self.assertEqual(event["metrics_provenance"]["mode"], "cached")
        self.assertEqual(event["metrics_provenance"]["observed_at"], observed)
        self.assertEqual(event["metrics_provenance"]["original_checkpoint"]["raw_result_path"], "/unit/origin/collector-legacy-row.json")
        self.assertEqual(original["metrics_provenance"]["mode"], "fresh")

    def test_finished_factory_cache_is_regated_without_importing_legacy_or_overwriting_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            old_path = root / "old-run/collector-legacy-row.json"
            old_path.parent.mkdir()
            old = {**self.row(), "likes": "19"}
            write_json(old_path, old)
            context = {"task_id": "one", "context_id": "context", "factory_rules": factory_rule_snapshot(RULES)}
            identity = hashlib.sha256(json.dumps(context, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            state_path = root / "collector-state.json"
            write_json(state_path, {"identity": identity, "status": "finished", "legacy_processing_entered": True,
                "result": {"decision": "accepted", "status": "COMPLETED", "legacy_result_reference": {"raw_result_path": str(old_path)}}})
            history = (old_path.read_bytes(), state_path.read_bytes())
            result_path = root / "new-run/result.json"
            result_path.parent.mkdir()
            with redirect_stdout(io.StringIO()):
                result = execute(context, root / "legacy-not-present", root, result_path, state_path)
            self.assertEqual((result["decision"], result["status"]), ("rejected", "REJECTED"))
            self.assertEqual(result["metrics_provenance"]["mode"], "cached")
            self.assertEqual(result["semantic_provenance"]["mode"], "cached")
            self.assertEqual((old_path.read_bytes(), state_path.read_bytes()), history)
            self.assertFalse((root / "legacy-not-present").exists())

    def test_result_contract_keeps_provenance_and_computes_ratio_on_server(self):
        context = {"task_id": "one", "project_id": "project", "country": "US", "keyword": "body", "product_id": "p",
            "product_images": ["/unit/reference.png"], "source_document": "manual", "source_row": 1, "context_id": "context"}
        row = cached_observations({**self.row(), "decision": "rejected"}, Path("/unit/legacy"))
        result = CollectorResult.model_validate(collector_result(context, row, Path("/unit/legacy"), Path("/unit/run-1/result.json")))
        self.assertEqual(result.like_view_ratio, 20)
        self.assertEqual(result.metrics_provenance.mode, "cached")
        self.assertEqual(result.metrics_provenance.original_checkpoint["run_id"], "run-1")

    def test_live_metrics_storage_and_websocket_preserve_cache_source(self):
        directory = tempfile.TemporaryDirectory(prefix="collector-provenance-unit-")
        self.addCleanup(directory.cleanup)
        repository = StateRepository("sqlite:///" + str(Path(directory.name) / "events.db"), Path(__file__).parents[1] / "app/seed.json")
        self.addCleanup(repository.engine.dispose)
        task_id = repository.state()["tasks"][0]["id"]
        repository.mutate(lambda state: state["runs"].append({"id": "run", "task_id": task_id,
            "stage": "collector", "status": "running", "provider": "UnitCollector", "progress": 0,
            "logs": [], "created_at": datetime.now(timezone.utc).isoformat()}))
        service = CollectorService(repository, SimpleNamespace(broadcast=AsyncMock()), SimpleNamespace(), adapter=SimpleNamespace())
        row = cached_observations(self.row(), Path("/unit/legacy"))
        event = {"type": "METRICS_UPDATED", "views": 100, "likes": 20, "metric_source": row["metric_source"],
            "metric_evidence": row["metric_evidence"], "metrics_provenance": row["metrics_provenance"]}
        asyncio.run(service._event(task_id, "run", event))
        live = repository.read_task(task_id)["context"]["live_metrics"]
        self.assertEqual(live["metrics_provenance"]["mode"], "cached")
        self.assertEqual(live["like_view_ratio"], 20)
        self.assertEqual(service.hub.broadcast.call_args.args[0].data["metrics_provenance"], row["metrics_provenance"])


if __name__ == "__main__":
    unittest.main()
