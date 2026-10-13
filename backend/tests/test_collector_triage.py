import asyncio
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.domain import now
from app.integrations.collector_service import CollectorService
from app.integrations.legacy.collector_adapter import LegacyCollectorAdapter
from app.integrations.collector_triage import ApplyTriage, CollectorAutoTriage, create_collector_triage_router, load_policy, machine_decision
from app.integrations.production_rules import factory_rule_snapshot
from app.models import Task, ProductionBatch
from app.repository import StateRepository

VALUES = {"min_views": 100, "min_likes": 1, "product_match_min_confidence": .85,
    "overlay_requirement": "any", "watermark_requirement": "any", "subtitle_requirement": "any"}
LEGACY = {"sha256": "saved-readonly-config", "rules": {"accepted_product_match": "pass", "accepted_ai_origin": "likely_real",
    "accepted_cutout_suitability": "pass", "accepted_require_cutout_suitability": True, "vidbee_require_watermark_review": True}}


def valid_result():
    return {"views": 1000, "likes": 30, "metric_source": "REAL_DOM_IN_ISOLATED_UNIT", "metric_evidence": "bound video stats",
        "reference_product_match": "pass", "product_match": "pass", "product_match_confidence": .95,
        "ai_origin": "likely_real", "cutout_suitability": "pass", "decision": "accepted", "decision_reason": "semantic_pass",
        "source_asset_metadata": {"verified": True, "sha256": "a" * 64}, "download_status": "completed"}


class CollectorTriageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = Settings(app_mode="integration", integration_dry_run=True,
            storage_root=self.root / "storage", database_url="sqlite:///" + str(self.root / "triage.db"))
        self.repository = StateRepository(self.settings.database_url, Path(__file__).parents[1] / "app/seed.json")
        self.adapter = SimpleNamespace(collect_one=AsyncMock(), dry_run=AsyncMock())
        self.collector = CollectorService(self.repository, SimpleNamespace(broadcast=AsyncMock()), self.settings, self.adapter)
        self.service = CollectorAutoTriage(self.collector)
        original = self.repository.state()["tasks"][0]
        self.task = {**original, "id": "isolated-triage", "current_stage": "collector", "status": "WAITING_REVIEW", "execution_mode": "integration",
            "source_video_id": None, "context": {"production_rules": copy.deepcopy(LEGACY), "task_context": {"factory_rules": factory_rule_snapshot(VALUES)}},
            "collector_result": {**valid_result(), "decision": "manual_review", "reference_product_match": "uncertain", "product_match": "uncertain",
                "decision_reason": "product_reference_uncertain"}, "created_at": now(), "updated_at": now()}
        self.raw = self.settings.storage_root / "isolated/raw.json"
        self.raw.parent.mkdir(parents=True)
        self.raw.write_text('{"decision":"manual_review"}')
        self.task["collector_result"]["legacy_result_reference"] = {"raw_result_path": str(self.raw)}
        self.source = self.settings.storage_root / "isolated/source.bin"
        self.source.write_bytes(b"isolated source fingerprint fixture, not a production video")
        self.verified_result = valid_result()
        self.verified_result.update(legacy_vidbee_task_id="isolated-download", source_asset_metadata={"verified": True,
            "path": str(self.source), "sha256": hashlib.sha256(self.source.read_bytes()).hexdigest(), "file_size": self.source.stat().st_size,
            "full_decode_verified": True, "width": 1080, "height": 1920, "fps": 30, "frame_count": 900, "duration": 30,
            "codec": "h264", "download_verification": {"task_id": "isolated-download", "status": "completed"}})
        def populate(state):
            state["tasks"].append(copy.deepcopy(self.task))
            state["collector"].append({**state["collector"][0], "id": "isolated-rule", "last_task_id": self.task["id"], "status": "WAITING_REVIEW"})
            state["stages"].append({"id": "isolated-stage", "task_id": self.task["id"], "name": "collector", "status": "waiting_review",
                "start_time": None, "end_time": None, "duration": 0, "retry_count": 0, "error": None})
            state["runs"].append({"id": "isolated-run", "task_id": self.task["id"], "stage": "collector", "provider": "LegacyCollectorAdapter",
                "status": "success", "progress": 100, "logs": ["REAL_RESULT_PRESERVED_IN_UNIT"], "created_at": now(), "finished_at": now()})
            state["batches"].append({"id": "isolated-paused", "status": "PAUSED", "task_ids": [self.task["id"]],
                "items": [{"task_id": self.task["id"], "status": "WAITING_REVIEW", "stage": "collector"}], "active_task_id": None,
                "summary": {}, "created_at": now(), "updated_at": now()})
        self.repository.mutate(populate)
        app = FastAPI()
        app.include_router(create_collector_triage_router(self.collector), prefix="/api/collector")
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.repository.engine.dispose()
        self.temp.cleanup()

    def test_unknown_and_conflicting_results_never_become_pass(self):
        for changes, expected in (({"error_code": "UNKNOWN_EXTERNAL_REQUEST"}, "UNKNOWN_EXTERNAL_REQUEST"),
                ({"product_match": "fail"}, "PRODUCT_EVIDENCE_CONFLICT"),
                ({"ai_origin": "uncertain"}, "AI_ORIGIN_UNCERTAIN"),
                ({"product_match_confidence": .2}, "PRODUCT_CONFIDENCE_BELOW_THRESHOLD"),
                ({"decision": "manual_review", "decision_reason": "watermark_unverified"}, "watermark_unverified")):
            result = machine_decision(self.task, {**valid_result(), **changes})
            self.assertEqual((result["decision"], result["reason"]), ("QUARANTINED", expected))

    def test_zero_metrics_and_verified_mismatch_auto_reject_not_review(self):
        for changes, reason in (({"views": 0, "likes": 0}, "factory_metric_threshold"),
                ({"reference_product_match": "fail", "product_match": "fail"}, "PRODUCT_MISMATCH"),
                ({"ai_origin": "likely_ai"}, "AI_ORIGIN_NOT_ACCEPTED"), ({"cutout_suitability": "fail"}, "CUTOUT_NOT_SUITABLE")):
            self.assertEqual(machine_decision(self.task, {**valid_result(), **changes}),
                {"decision": "AUTO_REJECTED", "reason": reason, "next_status": "REJECTED"})

    def test_success_requires_all_evidence_and_original_accepted_decision(self):
        self.assertEqual(machine_decision(self.task, self.verified_result)["decision"], "AUTO_ACCEPTED")
        for changes in ({"metric_evidence": None}, {"source_asset_metadata": None}, {"download_status": "queued"}, {"views": None}):
            self.assertEqual(machine_decision(self.task, {**valid_result(), **changes})["decision"], "QUARANTINED")

    def test_rule_snapshot_tampering_and_disabled_optional_cutout(self):
        task = copy.deepcopy(self.task)
        task["context"]["task_context"]["factory_rules"]["rules"]["min_views"] = 0
        self.assertEqual(machine_decision(task, valid_result())["reason"], "RULE_SNAPSHOT_INVALID")
        task = copy.deepcopy(self.task)
        task["context"]["production_rules"]["rules"]["accepted_require_cutout_suitability"] = False
        self.assertEqual(machine_decision(task, {**self.verified_result, "cutout_suitability": "uncertain"})["decision"], "AUTO_ACCEPTED")

    def test_existing_source_path_hash_and_probe_receipt_are_required(self):
        with patch.object(Path, "open", side_effect=PermissionError("isolated unreadable source")):
            self.assertEqual(machine_decision(self.task, self.verified_result)["reason"], "SOURCE_READ_FAILED")
        result = copy.deepcopy(self.verified_result)
        result["source_asset_metadata"].pop("frame_count")
        self.assertEqual(machine_decision(self.task, result)["reason"], "SOURCE_NOT_VERIFIED")
        self.source.write_bytes(b"source changed after receipt")
        self.assertEqual(machine_decision(self.task, self.verified_result)["reason"], "SOURCE_FINGERPRINT_CHANGED")
        self.source.unlink()
        self.assertEqual(machine_decision(self.task, self.verified_result)["reason"], "SOURCE_FILE_MISSING")

    def test_failed_download_never_bypasses_real_login(self):
        result = {**valid_result(), "decision_reason": "vidbee_download_failed", "download_status": "failed"}
        self.assertEqual(machine_decision(self.task, result)["decision"], "AUTO_FAILED")
        self.assertEqual(machine_decision(self.task, {**result, "error_message": "Log in for access"})["reason"], "SOURCE_AUTH_REQUIRED")

    def test_plan_and_apply_preserve_all_history_and_pause_without_external_call(self):
        before = self.repository.state()
        plan = self.client.post("/api/collector/auto-triage/plan").json()
        row = next(value for value in plan["items"] if value["task_id"] == self.task["id"])
        self.assertEqual((row["decision"], row["eligible"]), ("QUARANTINED", True))
        self.assertEqual(row["evidence"][0]["sha256"], hashlib.sha256(self.raw.read_bytes()).hexdigest())
        payload = {key: plan[key] for key in ("plan_id", "fingerprint")}
        response = self.client.post("/api/collector/auto-triage/apply", json={**payload, "confirmed": True})
        self.assertEqual(response.status_code, 200, response.text)
        after = self.repository.state()
        task = next(value for value in after["tasks"] if value["id"] == self.task["id"])
        self.assertEqual(task["status"], "QUARANTINED")
        self.assertEqual(task["collector_result"], self.task["collector_result"])
        self.assertEqual(before["runs"], after["runs"])
        self.assertEqual(before["reviews"], after["reviews"])
        self.assertEqual(after["batches"][-1]["status"], "PAUSED")
        self.assertEqual(after["batches"][-1]["items"][0]["status"], "QUARANTINED")
        self.assertTrue(self.raw.is_file())
        self.assertEqual(response.json()["paid_requests_submitted"], 0)
        self.adapter.collect_one.assert_not_called()
        self.adapter.dry_run.assert_not_called()
        repeat = self.client.post("/api/collector/auto-triage/apply", json={**payload, "confirmed": True})
        self.assertEqual(repeat.json()["summary"]["IDEMPOTENT"], 1)
        reopened = StateRepository(self.settings.database_url, self.repository.seed_path)
        try:
            self.assertEqual(reopened.read_task(self.task["id"])["status"], "QUARANTINED")
        finally:
            reopened.engine.dispose()

    def test_changed_evidence_is_not_applied_and_does_not_stop_other_rows(self):
        plan = self.service.plan()
        self.raw.write_text('{"decision":"changed"}')
        result = self.service.apply(ApplyTriage(plan_id=plan["plan_id"], fingerprint=plan["fingerprint"], confirmed=True))
        row = next(value for value in result["items"] if value["task_id"] == self.task["id"])
        self.assertEqual(row["status"], "CHANGED")
        self.assertEqual(self.repository.read_task(self.task["id"])["status"], "WAITING_REVIEW")

    def test_active_or_downstream_reviews_are_not_collector_triage_targets(self):
        for changes, reason in (({"current_stage": "background"}, "DOWNSTREAM_REVIEW_NOT_COLLECTOR"),):
            self.repository.mutate(lambda state: next(value for value in state["tasks"] if value["id"] == self.task["id"]).update(**changes))
            row = next(value for value in self.service.plan()["items"] if value["task_id"] == self.task["id"])
            self.assertEqual((row["eligible"], row["reason"]), (False, reason))
        self.repository.mutate(lambda state: next(value for value in state["runs"] if value["id"] == "isolated-run").update(status="running"))
        row = next(value for value in self.service.plan()["items"] if value["task_id"] == self.task["id"])
        self.assertEqual(row["reason"], "ACTIVE_TASK")

    def test_plan_confirmation_policy_and_path_security(self):
        plan = self.service.plan()
        self.assertEqual(self.client.post("/api/collector/auto-triage/apply", json={"plan_id": "../file", "fingerprint": "a" * 64, "confirmed": True}).status_code, 422)
        self.assertEqual(self.client.post("/api/collector/auto-triage/apply", json={"plan_id": plan["plan_id"], "fingerprint": plan["fingerprint"], "confirmed": False}).status_code, 422)
        response = self.client.put("/api/collector/unattended-policy", json={"enabled": False, "safe_pre_submit_retry_limit": 0, "retry_backoff_seconds": 2})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(load_policy(self.settings)["enabled"])
        self.assertEqual(self.client.post("/api/collector/auto-triage/apply", json={"plan_id": plan["plan_id"], "fingerprint": plan["fingerprint"], "confirmed": True}).status_code, 409)

    def test_quarantined_task_cannot_be_blindly_requeued(self):
        self.task["status"] = "QUARANTINED"
        with self.assertRaises(HTTPException) as failure:
            CollectorService._guard_collection_start({"batches": []}, self.task)
        self.assertEqual(failure.exception.status_code, 409)

    def test_manual_mode_is_explicit_and_persistent_not_a_fake_pass(self):
        self.client.put("/api/collector/unattended-policy", json={"enabled": False, "safe_pre_submit_retry_limit": 0, "retry_backoff_seconds": 2})
        self.assertFalse(self.client.get("/api/collector/unattended-policy").json()["enabled"])
        with patch("app.integrations.collector_service.record_machine_decision") as record_decision:
            self.assertFalse(load_policy(self.settings)["enabled"])
            record_decision.assert_not_called()

    def test_known_unsubmitted_network_error_has_bounded_new_runs_then_stops(self):
        self.repository.mutate(lambda state: next(value for value in state["batches"] if value["id"] == "isolated-paused").update(status="COMPLETED"))
        self.client.put("/api/collector/unattended-policy", json={"enabled": True, "safe_pre_submit_retry_limit": 2, "retry_backoff_seconds": .1})
        adapter = LegacyCollectorAdapter(self.root / "readonly-legacy", self.settings.storage_root)
        adapter.dry_run = lambda _context: {"ok": True, "checks": []}
        adapter.checkpoint = lambda _context: {"phase": "NOT_SUBMITTED"}
        async def fail_before_external(context, **_options):
            return {**context, "decision": "manual_review", "decision_reason": "network unavailable before any external submission",
                "error_code": "NETWORK_ERROR", "error_message": "isolated pre-submit failure", "legacy_csv_path": "unit-only",
                "legacy_result_reference": {}, "_process": {"exit_code": 1}}
        adapter.collect_one = AsyncMock(side_effect=fail_before_external)
        service = CollectorService(self.repository, self.collector.hub, self.settings, adapter)
        service.rules = lambda: LEGACY
        async def scenario():
            created = await service.create({"country": "MX", "keyword": "isolated bounded pre-submit retries",
                "product_images": [str(self.raw)], "factory_rules": VALUES, "video_id": "901"})
            service.settings.integration_dry_run = False
            queued = await service.queue(created["task_id"])
            await service.run(created["task_id"], queued["run_id"])
            return created["task_id"]
        task_id = asyncio.run(scenario())
        runs = [value for value in self.repository.state()["runs"] if value["task_id"] == task_id]
        self.assertEqual(adapter.collect_one.await_count, 3)
        self.assertEqual([value["retry_count"] for value in runs], [0, 1, 2])
        self.assertEqual(len({value["id"] for value in runs}), 3)
        self.assertTrue(all(value["status"] == "failed" for value in runs))
        self.assertEqual(self.repository.read_task(task_id)["context"]["collector_auto_triage"]["decision"], "AUTO_FAILED")

    def test_unknown_request_is_never_retried_even_if_checkpoint_claims_unsubmitted(self):
        self.repository.mutate(lambda state: next(value for value in state["batches"] if value["id"] == "isolated-paused").update(status="COMPLETED"))
        adapter = LegacyCollectorAdapter(self.root / "readonly-legacy", self.settings.storage_root)
        adapter.dry_run = lambda _context: {"ok": True, "checks": []}
        adapter.checkpoint = lambda _context: {"phase": "NOT_SUBMITTED"}
        async def unknown(context, **_options):
            return {**context, "decision": "manual_review", "decision_reason": "unknown_external_request",
                "error_code": "UNKNOWN_EXTERNAL_REQUEST", "error_message": "isolated unknown request",
                "legacy_csv_path": "unit-only", "legacy_result_reference": {}, "_process": {"exit_code": 1}}
        adapter.collect_one = AsyncMock(side_effect=unknown)
        service = CollectorService(self.repository, self.collector.hub, self.settings, adapter)
        service.rules = lambda: LEGACY
        async def scenario():
            created = await service.create({"country": "MX", "keyword": "isolated unknown request",
                "product_images": [str(self.raw)], "factory_rules": VALUES, "video_id": "902"})
            service.settings.integration_dry_run = False
            queued = await service.queue(created["task_id"])
            await service.run(created["task_id"], queued["run_id"])
            return created["task_id"]
        task_id = asyncio.run(scenario())
        self.assertEqual(adapter.collect_one.await_count, 1)
        self.assertEqual(self.repository.read_task(task_id)["status"], "QUARANTINED")
        self.assertEqual(len([value for value in self.repository.state()["runs"] if value["task_id"] == task_id]), 1)
