"""Isolated queue/policy contracts, not evidence of real media production."""

import copy
import json
import signal
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException

from app.integrations.process_runner import ProcessRunner
from test_real_review import ReviewFixture


class ProductionAutomationTests(ReviewFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.prepare("person")
        self.repository.mutate(lambda state: state["settings"].update(production_policy={}))
        self.production.jobs = {}
        self.production.cancels = {"unit-batch": threading.Event()}
        self.production.closing = False
        self.production.launch = AsyncMock()

    def quality_evidence(self, status="PASS", *, semantic="UNKNOWN"):
        person = self.verified_person()
        fingerprint = "isolated-quality-fingerprint"
        report_path = self.production.settings.storage_root / "quality.json"
        report_path.write_text(json.dumps({"mode": "REAL", "provider": "OpenCVCutoutQualityAnalyzer",
            "status": status, "fingerprint": fingerprint,
            "input_sha256": person["legacy_reference"]["alpha_sha256"], "semantic_status": semantic,
            "unit_fixture": True}))
        assessment = {"id": "unit-quality", "task_id": "unit-task", "asset_id": person["id"],
            "processing_run_id": "unit-quality-run", "status": status, "fingerprint": fingerprint,
            "quality_score": 98, "semantic_status": semantic, "issue_segments": []}
        def save(state):
            state["reviews"] = []
            state["quality_assessments"] = [assessment]
            state["runs"] = [{"id": "unit-quality-run", "task_id": "unit-task", "stage": "quality",
                "provider": "OpenCVCutoutQualityAnalyzer", "status": "success" if status == "PASS" else "failed"}]
            state["tasks"][0]["context"]["production"]["results"] = {"quality": {
                "status": status, "run_id": "unit-quality-run", "fingerprint": fingerprint,
                "report_path": str(report_path), "input_sha256": person["legacy_reference"]["alpha_sha256"]}}
        self.repository.mutate(save)
        return person

    def batch(self, status="RUNNING", items=None, runs=None):
        items = items or [{"task_id": "unit-task", "status": "RUNNING", "stage": "cutout"}]
        def save(state):
            template = state["tasks"][0]
            for item in items:
                task = next((row for row in state["tasks"] if row["id"] == item["task_id"]), None)
                if task is None:
                    task = {**copy.deepcopy(template), "id": item["task_id"]}
                    state["tasks"].append(task)
                task.update(status=item["status"], current_stage=item["stage"])
                task["context"]["production"].update(batch_id="unit-batch", automatic=True)
            state["batches"] = [{"id": "unit-batch", "status": status, "task_ids": [row["task_id"] for row in items],
                "items": copy.deepcopy(items), "active_task_id": items[0]["task_id"], "summary": {}}]
            state["runs"] = copy.deepcopy(runs or [])
        self.repository.mutate(save)

    def collector_checkpoint(self, phase, code="PROCESS_INTERRUPTED", *, download_resume=True, pid=None):
        self.batch(runs=[{"id": "unit-interrupted-collector", "task_id": "unit-task", "stage": "collector",
            "provider": "LegacyCollectorAdapter", "status": "failed", "error_code": code, "pid": pid}])
        self.production.collector = SimpleNamespace(
            adapter=SimpleNamespace(checkpoint=Mock(return_value={"phase": phase})),
            context=lambda task_id: {"task_id": task_id, "context_id": "unit-context"},
            can_resume_download=Mock(return_value=download_resume))

    async def test_geometry_pass_advances_without_manufactured_human_review(self):
        person = self.quality_evidence()
        self.assertTrue(self.production._person_review_approved("unit-task", person))
        self.assertEqual(self.repository.state()["reviews"], [])
        self.assertEqual(self.repository.state()["quality_assessments"][0]["semantic_status"], "UNKNOWN")

    async def test_explicit_person_review_policy_holds_geometry_pass(self):
        person = self.quality_evidence()
        self.repository.mutate(lambda state: state["settings"]["production_policy"].update(person_review_required=True))
        self.assertFalse(self.production._person_review_approved("unit-task", person))

    async def test_fail_or_unknown_quality_never_passes_despite_high_score(self):
        for status in ("FAIL", "UNKNOWN"):
            with self.subTest(status=status):
                person = self.quality_evidence(status)
                self.assertFalse(self.production._person_review_approved("unit-task", person))

    async def test_missing_saved_quality_does_not_infer_pass(self):
        person = self.verified_person()
        self.repository.mutate(lambda state: state.update(reviews=[], quality_assessments=[]))
        self.assertFalse(self.production._person_review_approved("unit-task", person))

    async def test_changed_alpha_invalidates_saved_quality(self):
        person = self.quality_evidence()
        Path(person["legacy_reference"]["alpha_path"]).write_bytes(b"changed isolated alpha")
        self.assertFalse(self.production._person_review_approved("unit-task", person))

    async def test_foreign_quality_run_cannot_pass_current_person(self):
        person = self.quality_evidence()
        self.repository.mutate(lambda state: state["runs"][0].update(task_id="another-unit-task"))
        self.assertFalse(self.production._person_review_approved("unit-task", person))

    async def test_latest_pending_review_wins_across_id_prefixes_and_iso_time(self):
        person = self.verified_person()
        for previous_prefix, current_prefix, created_at in (
            ("review", "rev", None), ("rev", "review", None),
            ("review", "rev", "2026-10-08T10:00:00.000000+00:00"),
            ("rev", "review", "2026-10-08T10:00:00Z"),
        ):
            with self.subTest(previous=previous_prefix, current=current_prefix, created_at=created_at):
                older = {"id": previous_prefix + "-20261007190000000000-old", "task_id": "unit-task",
                    "asset_id": person["id"], "status": "approved"}
                pending = {"id": current_prefix + "-20261008100000000000-new", "task_id": "unit-task",
                    "asset_id": person["id"], "status": "pending"}
                if created_at:
                    pending["created_at"] = created_at
                self.repository.mutate(lambda state: state.update(reviews=[pending, older]))
                self.assertFalse(self.production._person_review_approved("unit-task", person))

    async def test_latest_human_decision_can_approve_geometry_failure(self):
        person = self.quality_evidence("FAIL")
        self.repository.mutate(lambda state: state["reviews"].append({"id": "rev-20261008100000000000-human",
            "task_id": "unit-task", "asset_id": person["id"], "status": "approved"}))
        self.assertTrue(self.production._person_review_approved("unit-task", person))
        self.assertEqual(self.repository.state()["quality_assessments"][0]["status"], "FAIL")

    async def test_latest_rejected_review_blocks_prior_approval(self):
        person = self.verified_person()
        self.repository.mutate(lambda state: state.update(reviews=[
            {"id": "review-20261007100000000000-old", "task_id": "unit-task", "asset_id": person["id"], "status": "approved"},
            {"id": "rev-20261008100000000000-current", "task_id": "unit-task", "asset_id": person["id"], "status": "rejected"}]))
        self.assertFalse(self.production._person_review_approved("unit-task", person))

    async def test_person_approval_default_queues_automatic_full_pipeline(self):
        self.verified_person()
        outcome = await self.review.action("approve", "unit-review-v1", {})
        self.assertTrue(outcome["accepted"])
        state = self.repository.state()
        production = state["tasks"][0]["context"]["production"]
        self.assertEqual(state["tasks"][0]["status"], "QUEUED")
        self.assertIsNone(production["requested_stage"])
        self.assertTrue(production["automatic"])
        self.assertFalse(production["retry"])
        self.assertTrue(production["policy_snapshot"]["auto_advance"])
        self.assertEqual(state["batches"][0]["task_ids"], ["unit-task"])
        self.assertEqual(state["reviews"][0]["status"], "approved")
        self.assertEqual(state["runs"], [])

    async def test_auto_advance_false_saves_approval_without_queue(self):
        self.verified_person()
        self.repository.mutate(lambda state: state["settings"]["production_policy"].update(auto_advance=False))
        outcome = await self.review.action("approve", "unit-review-v1", {})
        self.assertFalse(outcome.get("accepted", False))
        self.assertEqual(self.repository.state()["reviews"][0]["status"], "approved")
        self.assertEqual(self.repository.state()["batches"], [])

    async def test_first_composition_approval_queues_rest_without_new_stage_click(self):
        self.repository.mutate(lambda state: state["assets"][0].update(type="compose"))
        def prompts(state):
            state["prompts"] = [{"id": "unit-prompt-" + str(i), "task_id": "unit-task", "ordinal": i,
                "status": "READY", "fingerprint": "unit-prompt-fingerprint-" + str(i)} for i in range(1, 21)]
            state["assets"][0]["legacy_reference"]["prompt_id"] = "unit-prompt-1"
            state["tasks"][0]["context"]["production"]["first_composition"] = {
                "composition_asset_id": "unit-asset-v1", "human_review": "PENDING"}
        self.repository.mutate(prompts)
        outcome = await self.review.action("approve", "unit-review-v1", {})
        self.assertTrue(outcome["accepted"])
        production = self.repository.state()["tasks"][0]["context"]["production"]
        self.assertIsNone(production["requested_stage"])
        self.assertEqual(len(production["incomplete_prompt_ids"]), 19)
        self.assertEqual(production["first_composition"]["human_review"], "APPROVED")

    async def test_unknown_paid_request_holds_automatic_continue(self):
        self.repository.mutate(lambda state: state["runs"].append({"id": "unit-paid", "task_id": "unit-task",
            "stage": "background", "status": "failed", "provider": "BackgroundRPAAdapter",
            "error_code": "UNKNOWN_EXTERNAL_REQUEST"}))
        result = await self.production.continue_task("unit-task")
        self.assertFalse(result.get("accepted", False))
        state = self.repository.state()
        self.assertEqual(state["batches"], [])
        self.assertEqual(state["tasks"][0]["context"]["production"]["waiting"]["reason"], "UNKNOWN_EXTERNAL_REQUEST")

    async def test_running_stage_is_not_queued_twice(self):
        self.repository.mutate(lambda state: state["runs"].append({"id": "unit-running", "task_id": "unit-task",
            "status": "running", "stage": "cutout", "provider": "LegacyComfyUIAdapter"}))
        result = await self.production.continue_task("unit-task")
        self.assertFalse(result.get("accepted", False))
        self.assertEqual(self.repository.state()["batches"], [])

    async def test_apply_quality_automatic_pass_continues_without_recutout(self):
        person = self.verified_person()
        self.repository.mutate(lambda state: state["tasks"][0]["context"]["production"].update(
            requested_stage="quality", automatic=True, retry=False))
        self.production._saved_context = AsyncMock(return_value=({}, {"isolated_source": True}))
        self.production._asset = lambda *args, **kwargs: person
        self.production._quality = AsyncMock(return_value={"status": "PASS"})
        self.production._person_review_approved = lambda *args: True
        self.production._task = AsyncMock()
        self.production._video_stage = AsyncMock()
        await self.production._requested_stage("unit-batch", self.repository.state()["tasks"][0])
        self.production._quality.assert_awaited_once()
        self.production._task.assert_awaited_once_with("unit-batch", "unit-task")
        self.production._video_stage.assert_not_awaited()
        self.assertIsNone(self.repository.state()["tasks"][0]["context"]["production"]["requested_stage"])

    async def test_apply_quality_pending_review_still_waits(self):
        person = self.verified_person()
        self.repository.mutate(lambda state: state["tasks"][0]["context"]["production"].update(
            requested_stage="quality", automatic=True, retry=False))
        self.production._saved_context = AsyncMock(return_value=({}, {}))
        self.production._asset = lambda *args, **kwargs: person
        self.production._quality = AsyncMock(return_value={"status": "PASS"})
        self.production._task = AsyncMock()
        await self.production._requested_stage("unit-batch", self.repository.state()["tasks"][0])
        self.production._task.assert_not_awaited()
        self.assertEqual(self.repository.state()["tasks"][0]["status"], "WAITING_REVIEW")

    async def run_quality_policy(self, status="PASS", composition=None):
        person = self.verified_person()
        self.repository.mutate(lambda state: state.update(reviews=[], quality_assessments=[]))
        result = {"status": status, "quality_score": 98, "metrics": {"hole_ratio": 0.01},
                  "semantic_status": "UNKNOWN", "geometry_status": status, "issue_segments": [],
                  "fingerprint": "unit-service-quality-fingerprint"}
        async def stage(batch_id, task_id, stage, provider, invoke):
            self.assertEqual(provider, "OpenCVCutoutQualityAnalyzer")
            self.repository.mutate(lambda state: state["runs"].append({"id": "unit-service-quality-run",
                "task_id": task_id, "stage": stage, "provider": provider,
                "status": "success" if status == "PASS" else "failed"}))
            return result
        self.production._stage = stage
        with patch("app.integrations.quality_adapter.CutoutQualityAdapter") as adapter:
            assessment = await self.production._quality("unit-batch", "unit-task", person, composition)
            adapter.assert_called_once_with(self.production.settings, thresholds=self.production.policy()["quality_thresholds"])
        return assessment

    async def test_quality_service_pass_records_geometry_without_extra_person_review(self):
        assessment = await self.run_quality_policy()
        self.assertEqual(assessment["status"], "PASS")
        self.assertEqual(assessment["metrics"]["semantic_status"], "UNKNOWN")
        self.assertEqual(self.repository.state()["reviews"], [])

    async def test_quality_service_failure_or_unknown_creates_real_waiting_review_record(self):
        for status in ("FAIL", "UNKNOWN"):
            with self.subTest(status=status):
                await self.run_quality_policy(status)
                self.assertEqual(self.repository.state()["reviews"][0]["status"], "pending")
                self.assertEqual(self.repository.state()["reviews"][0]["asset_id"], "unit-asset-v1")

    async def test_quality_service_human_person_policy_is_consumed(self):
        self.repository.mutate(lambda state: state["settings"]["production_policy"].update(person_review_required=True))
        await self.run_quality_policy()
        self.assertEqual(self.repository.state()["reviews"][0]["status"], "pending")

    async def test_quality_service_composition_always_requires_first_human_review(self):
        await self.run_quality_policy(composition={"id": "unit-first-composition"})
        self.assertEqual(self.repository.state()["reviews"][0]["asset_id"], "unit-first-composition")
        self.assertEqual(self.repository.state()["reviews"][0]["status"], "pending")

    async def test_quality_service_consumes_saved_thresholds(self):
        self.repository.mutate(lambda state: state["settings"]["production_policy"].update(
            quality_thresholds={"max_hole_ratio": 0.04}))
        await self.run_quality_policy()
        self.assertEqual(self.production.policy()["quality_thresholds"], {"max_hole_ratio": 0.04})

    async def test_retry_limit_bounds_local_attempts(self):
        self.repository.mutate(lambda state: state["settings"]["production_policy"].update(retry_limit=2))
        self.production._stage = AsyncMock(side_effect=[
            {"status": "FAILED", "retryable": True, "error_code": "LOCAL_CONNECTION_FAILED"},
            {"status": "FAILED", "retryable": True, "error_code": "LOCAL_CONNECTION_FAILED"},
            {"status": "READY"}])
        with patch("app.integrations.production_service.asyncio.sleep", new=AsyncMock()):
            result = await self.production._video_stage("unit-batch", "unit-task", "cutout", {})
        self.assertEqual(result["status"], "READY")
        self.assertEqual(self.production._stage.await_count, 3)

    async def test_zero_retry_limit_executes_local_stage_once(self):
        self.repository.mutate(lambda state: state["settings"]["production_policy"].update(retry_limit=0))
        self.production._stage = AsyncMock(return_value={"status": "FAILED", "retryable": True})
        await self.production._video_stage("unit-batch", "unit-task", "compose", {})
        self.production._stage.assert_awaited_once()

    async def test_nonretryable_or_unknown_local_result_never_repeats(self):
        for result in ({"status": "FAILED", "retryable": False},
                       {"status": "FAILED", "retryable": True, "error_code": "UNKNOWN_EXTERNAL_REQUEST"},
                       {"status": "WAITING_MANUAL", "retryable": True}):
            with self.subTest(result=result):
                self.production._stage = AsyncMock(return_value=result)
                await self.production._video_stage("unit-batch", "unit-task", "compose", {})
                self.production._stage.assert_awaited_once()

    async def test_paid_prompt_and_background_never_automatic_retry(self):
        for stage in ("prompt", "background"):
            with self.subTest(stage=stage):
                self.production.providers[stage] = object()
                self.production._stage = AsyncMock(return_value={"status": "FAILED", "retryable": True,
                    "error_code": "NETWORK_TIMEOUT"})
                await self.production._video_stage("unit-batch", "unit-task", stage, {})
                self.production._stage.assert_awaited_once()

    async def test_quality_provider_is_safe_local_resume(self):
        self.repository.mutate(lambda state: state["runs"].append({"id": "unit-quality", "task_id": "unit-task",
            "provider": "OpenCVCutoutQualityAnalyzer", "status": "failed", "error_code": "LOCAL_EXECUTION_INTERRUPTED"}))
        self.assertTrue(self.production._local_resume("unit-task"))

    async def test_recovery_marks_local_interruption_without_launching_during_recover(self):
        self.batch(runs=[{"id": "unit-cutout-run", "task_id": "unit-task", "stage": "cutout",
            "provider": "LegacyComfyUIAdapter", "status": "running", "pid": 123456}])
        await self.production.recover()
        state = self.repository.state()
        self.assertEqual(state["runs"][0]["error_code"], "LOCAL_EXECUTION_INTERRUPTED")
        self.assertEqual(state["runs"][0]["status"], "failed")
        self.assertEqual(state["batches"][0]["status"], "PAUSED")
        self.production.launch.assert_not_awaited()

    async def test_recovery_resumes_same_authorized_batch_with_dead_local_worker(self):
        self.batch(runs=[{"id": "unit-cutout-run", "task_id": "unit-task", "stage": "cutout",
            "provider": "LegacyComfyUIAdapter", "status": "running", "pid": 123456}])
        await self.production.recover()
        with patch("app.integrations.production_service.probe_process", side_effect=ProcessLookupError):
            await self.production.resume_recovered()
        self.production.launch.assert_awaited_once_with("unit-batch")
        state = self.repository.state()
        self.assertEqual(len(state["tasks"]), 1)
        self.assertEqual(len(state["batches"]), 1)
        self.assertEqual(len(state["assets"]), 1)
        self.assertEqual(state["tasks"][0]["status"], "QUEUED")
        self.assertEqual(state["batches"][0]["items"][0]["status"], "QUEUED")
        self.assertEqual(state["runs"][0]["id"], "unit-cutout-run")

    async def test_auto_resume_false_preserves_interrupted_batch_for_manual_continue(self):
        self.batch(runs=[{"id": "unit-run", "task_id": "unit-task", "stage": "cutout",
            "provider": "LegacyComfyUIAdapter", "status": "running"}])
        self.repository.mutate(lambda state: state["settings"]["production_policy"].update(auto_resume=False))
        await self.production.recover()
        await self.production.resume_recovered()
        self.production.launch.assert_not_awaited()
        self.assertEqual(self.repository.state()["batches"][0]["status"], "PAUSED")

    async def test_explicitly_paused_or_cancelled_batches_never_auto_resume(self):
        for status in ("PAUSED", "CANCELLED"):
            with self.subTest(status=status):
                self.batch(status=status, runs=[{"id": "unit-run", "task_id": "unit-task", "stage": "cutout",
                    "provider": "LegacyComfyUIAdapter", "status": "running"}])
                self.production.launch.reset_mock()
                await self.production.recover()
                await self.production.resume_recovered()
                self.production.launch.assert_not_awaited()

    async def test_paused_unstarted_claim_is_released_without_automatic_launch(self):
        self.batch(status="PAUSED", items=[{"task_id": "unit-task", "status": "RUNNING", "stage": "collector"},
            {"task_id": "unit-next", "status": "QUEUED", "stage": "collector"}])
        self.repository.mutate(lambda state: state["tasks"][0].update(collector_result={}))
        await self.production.recover()
        await self.production.resume_recovered()
        state = self.repository.state()
        self.production.launch.assert_not_awaited()
        self.assertEqual(state["batches"][0]["status"], "PAUSED")
        self.assertIsNone(state["batches"][0]["active_task_id"])
        self.assertTrue(all(item["status"] == "QUEUED" for item in state["batches"][0]["items"]))
        self.assertEqual(state["tasks"][0]["status"], "QUEUED")
        self.assertEqual(state["runs"], [])
        self.assertTrue(any(row["action"] == "production.unstarted_claim_recovered" for row in state["audits"]))

    async def test_paused_claim_with_prior_run_is_not_reset_as_unstarted(self):
        self.batch(status="PAUSED", items=[{"task_id": "unit-task", "status": "RUNNING", "stage": "collector"}],
            runs=[{"id": "unit-prior-run", "task_id": "unit-task", "provider": "LegacyCollectorAdapter",
                "status": "failed", "stage": "collector", "error_code": "UNKNOWN_EXTERNAL_REQUEST"}])
        self.repository.mutate(lambda state: state["tasks"][0].update(collector_result={}))
        await self.production.recover()
        self.assertEqual(self.repository.state()["batches"][0]["items"][0]["status"], "WAITING_REVIEW")
        self.assertEqual(self.repository.state()["runs"][0]["error_code"], "UNKNOWN_EXTERNAL_REQUEST")

    async def test_existing_created_task_outside_batch_is_not_started_at_boot(self):
        self.repository.mutate(lambda state: state["tasks"][0].update(status="CREATED"))
        await self.production.recover()
        await self.production.resume_recovered()
        self.production.launch.assert_not_awaited()
        self.assertEqual(self.repository.state()["tasks"][0]["status"], "CREATED")

    async def test_recovered_queue_reads_and_writes_do_not_scale_with_item_count(self):
        self.batch(status="QUEUED", items=[{"task_id": f"unit-queued-{index}", "status": "QUEUED", "stage": "collector"}
            for index in range(134)])
        self.production.recovered_batches = ["unit-batch"]
        self.production.recovered_run_ids = set()
        self.production.recovered_item_ids = set()
        with patch.object(self.repository, "state", wraps=self.repository.state) as read, patch.object(
                self.repository, "mutate", wraps=self.repository.mutate) as mutate:
            await self.production.resume_recovered()
        self.assertLessEqual(read.call_count, 3)
        self.assertEqual(mutate.call_count, 1)
        self.production.launch.assert_awaited_once_with("unit-batch")
        batch = self.repository.state()["batches"][0]
        self.assertEqual(len(batch["items"]), 134)
        self.assertTrue(all(item["status"] == "QUEUED" for item in batch["items"]))

    async def test_cancel_during_checkpoint_verification_prevents_recovered_handoff(self):
        self.collector_checkpoint("NOT_SUBMITTED")
        await self.production.recover()
        def cancel(_):
            def stop(state):
                state["batches"][0]["status"] = "CANCELLED"
                state["batches"][0]["items"][0]["status"] = "CANCELLED"
                state["tasks"][0]["status"] = "CANCELLED"
            self.repository.mutate(stop)
            return {"phase": "NOT_SUBMITTED"}
        self.production.collector.adapter.checkpoint.side_effect = cancel
        await self.production.resume_recovered()
        self.production.launch.assert_not_awaited()
        state = self.repository.state()
        self.assertEqual(state["batches"][0]["status"], "CANCELLED")
        self.assertEqual(state["batches"][0]["items"][0]["status"], "CANCELLED")
        self.assertEqual(state["tasks"][0]["status"], "CANCELLED")

    async def test_recovery_shared_missing_environment_pauses_without_consuming_queue(self):
        self.batch(status="QUEUED", items=[{"task_id": f"unit-pending-{index}", "status": "QUEUED", "stage": "collector"}
            for index in range(3)])
        self.production.recovered_batches = ["unit-batch"]
        self.production.recovered_run_ids = set()
        self.production.recovered_item_ids = set()
        check = {"name": "ComfyUI", "status": "MISSING", "message": "ComfyUI unavailable", "error_code": "CONNECTION_REFUSED"}
        self.production.readiness.check = AsyncMock(return_value={"ready": False, "checks": [check]})
        await self.production.resume_recovered()
        self.production.launch.assert_not_awaited()
        state = self.repository.state()
        self.assertEqual(state["batches"][0]["status"], "PAUSED")
        self.assertTrue(all(item["status"] == "QUEUED" for item in state["batches"][0]["items"]))
        self.assertEqual(state["runs"], [])
        self.assertEqual(state["errors"], [])
        waiting = state["batches"][0]["summary"]["environment_wait"]
        self.assertEqual(waiting["checks"], [check])
        self.assertEqual(waiting["status"], "WAITING_MANUAL")
        self.assertEqual(waiting["reason"], "PRODUCTION_NOT_READY")
        self.assertFalse(any(task["status"] == "FAILED" for task in state["tasks"]))

    async def test_runtime_preflight_failure_preserves_exact_error_and_remaining_queue(self):
        self.batch(status="QUEUED", items=[{"task_id": f"unit-pending-{index}", "status": "QUEUED", "stage": "collector"}
            for index in range(3)])
        check = {"name": "chrome_cdp", "status": "MISSING", "message": "http://127.0.0.1:9222 connection refused"}
        self.production._task = AsyncMock(side_effect=HTTPException(409, {
            "code": "INTEGRATION_NOT_READY", "message": "采集环境检查未通过；没有启动搜索", "checks": [check]}))
        await self.production.run("unit-batch")
        self.production._task.assert_awaited_once()
        state = self.repository.state()
        self.assertEqual(state["batches"][0]["status"], "PAUSED")
        self.assertEqual(state["batches"][0]["active_task_id"], None)
        self.assertTrue(all(item["status"] == "QUEUED" for item in state["batches"][0]["items"]))
        self.assertEqual(state["runs"], [])
        self.assertEqual(state["errors"], [])
        waiting = state["batches"][0]["summary"]["environment_wait"]
        self.assertEqual(waiting["reason"], "INTEGRATION_NOT_READY")
        self.assertEqual(waiting["checks"], [check])
        self.assertIn("没有启动搜索", waiting["message"])

    async def test_paid_interrupted_requests_never_resume_or_repeat(self):
        for provider, stage in (("LegacyPromptAdapter", "prompt"), ("BackgroundRPAAdapter", "background")):
            with self.subTest(provider=provider):
                self.batch(runs=[{"id": "unit-paid-run", "task_id": "unit-task", "stage": stage,
                    "provider": provider, "status": "running", "pid": 123456}])
                self.production.launch.reset_mock()
                await self.production.recover()
                with patch("app.integrations.production_service.probe_process") as probe:
                    await self.production.resume_recovered()
                self.production.launch.assert_not_awaited()
                probe.assert_not_called()
                state = self.repository.state()
                self.assertEqual(state["runs"][0]["error_code"], "UNKNOWN_EXTERNAL_REQUEST")
                self.assertEqual(state["tasks"][0]["context"]["production"]["waiting"]["reason"], "UNKNOWN_EXTERNAL_REQUEST")

    async def test_still_alive_or_unverifiable_original_pid_holds_without_duplicate_worker(self):
        for exception, reason in ((None, "WORKER_STILL_RUNNING"), (PermissionError(), "WORKER_STATUS_UNKNOWN")):
            with self.subTest(reason=reason):
                self.batch(runs=[{"id": "unit-cutout-run", "task_id": "unit-task", "stage": "cutout",
                    "provider": "LegacyComfyUIAdapter", "status": "running", "pid": 123456}])
                self.production.launch.reset_mock()
                await self.production.recover()
                with patch("app.integrations.production_service.probe_process", side_effect=exception):
                    await self.production.resume_recovered()
                self.production.launch.assert_not_awaited()
                self.assertEqual(self.repository.state()["tasks"][0]["context"]["production"]["waiting"]["reason"], reason)

    async def test_finished_historical_pid_does_not_block_unstarted_next_step(self):
        self.batch(status="QUEUED", items=[{"task_id": "unit-task", "status": "QUEUED", "stage": "background"}],
            runs=[{"id": "unit-old-run", "task_id": "unit-task", "stage": "quality",
                "provider": "OpenCVCutoutQualityAnalyzer", "status": "success", "pid": 123456}])
        await self.production.recover()
        with patch("app.integrations.production_service.probe_process") as probe:
            await self.production.resume_recovered()
        probe.assert_not_called()
        self.production.launch.assert_awaited_once_with("unit-batch")

    async def test_unknown_one_task_does_not_stop_other_queued_task_after_restart(self):
        self.batch(items=[{"task_id": "unit-task", "status": "RUNNING", "stage": "background"},
                          {"task_id": "unit-next", "status": "QUEUED", "stage": "collector"}],
            runs=[{"id": "unit-paid-run", "task_id": "unit-task", "stage": "background",
                   "provider": "BackgroundRPAAdapter", "status": "running"}])
        await self.production.recover()
        await self.production.resume_recovered()
        self.production.launch.assert_awaited_once_with("unit-batch")
        state = self.repository.state()
        self.assertEqual(len(state["tasks"]), 2)
        self.assertEqual(state["batches"][0]["items"][0]["status"], "WAITING_REVIEW")
        self.assertEqual(state["batches"][0]["items"][1]["status"], "QUEUED")

    async def test_cancelled_item_is_not_requeued_from_historical_local_error(self):
        self.batch(items=[{"task_id": "unit-task", "status": "CANCELLED", "stage": "cutout"},
                          {"task_id": "unit-next", "status": "QUEUED", "stage": "collector"}],
            runs=[{"id": "unit-old-local-run", "task_id": "unit-task", "stage": "cutout",
                   "provider": "LegacyComfyUIAdapter", "status": "failed", "error_code": "LOCAL_EXECUTION_INTERRUPTED"}])
        await self.production.recover()
        await self.production.resume_recovered()
        state = self.repository.state()
        self.assertEqual(state["tasks"][0]["status"], "CANCELLED")
        self.assertEqual(state["batches"][0]["items"][0]["status"], "CANCELLED")
        self.assertEqual(state["batches"][0]["items"][1]["status"], "QUEUED")

    async def test_safe_collector_checkpoint_resumes_after_collector_recover(self):
        for phase in ("NOT_SUBMITTED", "SEMANTIC_RETRY_READY"):
            with self.subTest(phase=phase):
                self.collector_checkpoint(phase)
                self.production.launch.reset_mock()
                await self.production.recover(active_run_ids={"unit-interrupted-collector"})
                await self.production.resume_recovered()
                self.production.launch.assert_awaited_once_with("unit-batch")
                self.assertEqual(self.repository.state()["runs"][0]["error_code"], "PROCESS_INTERRUPTED")
                self.assertEqual(self.repository.state()["tasks"][0]["status"], "QUEUED")

    async def test_safe_download_checkpoints_resume_existing_vidbee_only(self):
        for phase in ("DOWNLOAD_PENDING", "RESULT_READY", "SOURCE_VERIFICATION_READY"):
            with self.subTest(phase=phase):
                self.collector_checkpoint(phase, "DOWNLOAD_PENDING")
                self.production.launch.reset_mock()
                await self.production.recover(active_run_ids={"unit-interrupted-collector"})
                await self.production.resume_recovered()
                self.production.launch.assert_awaited_once_with("unit-batch")
                self.production.collector.can_resume_download.assert_called_once_with("unit-task")
                self.assertEqual(len(self.repository.state()["runs"]), 1)

    async def test_download_checkpoint_without_verified_resume_boundary_holds(self):
        self.collector_checkpoint("DOWNLOAD_PENDING", "DOWNLOAD_PENDING", download_resume=False)
        await self.production.recover(active_run_ids={"unit-interrupted-collector"})
        await self.production.resume_recovered()
        self.production.launch.assert_not_awaited()
        task = self.repository.state()["tasks"][0]
        self.assertEqual(task["context"]["production"]["waiting"]["reason"], "COLLECTOR_RECOVERY_BLOCKED")

    async def test_unsafe_collector_checkpoint_does_not_replay_paid_screening(self):
        for code in ("PROCESS_INTERRUPTED", "DOWNLOAD_PENDING"):
            for phase in ("MISSING", "CONFLICT", "UNKNOWN_EXTERNAL_REQUEST"):
                with self.subTest(code=code, phase=phase):
                    self.collector_checkpoint(phase, code)
                    self.production.launch.reset_mock()
                    await self.production.recover(active_run_ids={"unit-interrupted-collector"})
                    await self.production.resume_recovered()
                    self.production.launch.assert_not_awaited()
                    task = self.repository.state()["tasks"][0]
                    self.assertEqual(task["status"], "WAITING_REVIEW")
                    self.assertIn(phase, task["context"]["production"]["waiting"]["message"])

    async def test_missing_collector_checkpoint_waits_instead_of_crashing_startup(self):
        self.collector_checkpoint("NOT_SUBMITTED")
        self.production.collector.adapter.checkpoint.side_effect = OSError("isolated missing checkpoint")
        await self.production.recover(active_run_ids={"unit-interrupted-collector"})
        await self.production.resume_recovered()
        self.production.launch.assert_not_awaited()
        waiting = self.repository.state()["tasks"][0]["context"]["production"]["waiting"]
        self.assertEqual(waiting["reason"], "COLLECTOR_RECOVERY_BLOCKED")
        self.assertIn("UNAVAILABLE", waiting["message"])

    async def test_original_active_collector_pid_survives_earlier_collector_recovery(self):
        self.collector_checkpoint("NOT_SUBMITTED", pid=123456)
        # CollectorService has already changed the Run to failed, but lifespan
        # captured this originally-running ID before either recover method.
        await self.production.recover(active_run_ids={"unit-interrupted-collector"})
        with patch("app.integrations.production_service.probe_process") as probe:
            await self.production.resume_recovered()
        probe.assert_called_once_with(123456)
        self.production.launch.assert_not_awaited()
        self.production.collector.adapter.checkpoint.assert_not_called()
        task = self.repository.state()["tasks"][0]
        self.assertEqual(task["context"]["production"]["waiting"]["reason"], "WORKER_STILL_RUNNING")

    async def test_dead_original_collector_pid_allows_checkpoint_verified_resume(self):
        self.collector_checkpoint("NOT_SUBMITTED", pid=123456)
        await self.production.recover(active_run_ids={"unit-interrupted-collector"})
        with patch("app.integrations.production_service.probe_process", side_effect=ProcessLookupError) as probe:
            await self.production.resume_recovered()
        probe.assert_called_once_with(123456)
        self.production.launch.assert_awaited_once_with("unit-batch")
        self.production.collector.adapter.checkpoint.assert_called_once()

    async def test_unknown_history_blocks_even_safe_collector_checkpoint(self):
        self.collector_checkpoint("NOT_SUBMITTED")
        self.repository.mutate(lambda state: state["runs"].insert(0, {"id": "unit-paid-unknown",
            "task_id": "unit-task", "stage": "background", "provider": "BackgroundRPAAdapter",
            "status": "failed", "error_code": "UNKNOWN_EXTERNAL_REQUEST"}))
        await self.production.recover(active_run_ids={"unit-interrupted-collector"})
        await self.production.resume_recovered()
        self.production.launch.assert_not_awaited()
        self.production.collector.adapter.checkpoint.assert_not_called()

    async def test_cancelled_collector_item_stays_cancelled_despite_safe_checkpoint(self):
        self.collector_checkpoint("NOT_SUBMITTED")
        def cancel(state):
            state["batches"][0]["items"][0]["status"] = "CANCELLED"
            state["tasks"][0]["status"] = "CANCELLED"
        self.repository.mutate(cancel)
        await self.production.recover(active_run_ids={"unit-interrupted-collector"})
        await self.production.resume_recovered()
        self.production.launch.assert_not_awaited()
        self.production.collector.adapter.checkpoint.assert_not_called()
        self.assertEqual(self.repository.state()["tasks"][0]["status"], "CANCELLED")

    async def test_mock_mode_does_not_launch_real_production_at_boot(self):
        self.batch(status="QUEUED", items=[{"task_id": "unit-task", "status": "QUEUED", "stage": "collector"}])
        self.production.settings.app_mode = "mock"
        await self.production.recover()
        await self.production.resume_recovered()
        self.production.launch.assert_not_awaited()


class ProcessSupervisionTests(unittest.TestCase):
    def execute(self, script, *, timeout=10):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        events = []
        execution = ProcessRunner().run([sys.executable, "-c", script], cwd=root, log_dir=root,
            timeout_s=timeout, on_event=events.append)
        return execution, events

    def test_real_isolated_child_exit_captures_failure_and_both_streams(self):
        execution, events = self.execute("import sys;print('unit stdout',flush=True);print('unit stderr',file=sys.stderr,flush=True);sys.exit(17)")
        self.assertEqual(execution.exit_code, 17)
        self.assertIsNone(execution.termination_signal)
        self.assertFalse(execution.timed_out)
        self.assertFalse(execution.cancelled)
        self.assertGreater(execution.pid, 0)
        self.assertIn("unit stdout", Path(execution.stdout_log_path).read_text())
        self.assertIn("unit stderr", Path(execution.stderr_log_path).read_text())
        self.assertGreaterEqual(execution.finished_at, execution.started_at)
        self.assertGreaterEqual(execution.duration_ms, 0)
        self.assertEqual(events[-1]["type"], "process_finished")
        self.assertEqual(events[-1]["exit_code"], 17)
        self.assertEqual(execution.resource_info["scope"], "cumulative_child_processes")

    def test_real_isolated_child_signal_is_not_guessed_as_oom(self):
        execution, events = self.execute("import os,signal;os.kill(os.getpid(),signal.SIGTERM)")
        self.assertEqual(execution.exit_code, -signal.SIGTERM)
        self.assertEqual(execution.termination_signal, signal.SIGTERM)
        self.assertEqual(events[-1]["termination_signal"], signal.SIGTERM)
        self.assertFalse(execution.timed_out)
        self.assertFalse(execution.cancelled)

    def test_real_isolated_child_reports_heartbeat_while_silent(self):
        execution, events = self.execute("import time;time.sleep(5.3)")
        heartbeats = [event for event in events if event["type"] == "PROCESS_HEARTBEAT"]
        self.assertEqual(execution.exit_code, 0)
        self.assertTrue(heartbeats)
        self.assertEqual(heartbeats[0]["pid"], execution.pid)
        self.assertGreater(execution.last_heartbeat, execution.started_at)
        self.assertGreaterEqual(heartbeats[0]["elapsed_s"], 5)
        logged = [json.loads(line) for line in Path(execution.log_path).read_text().splitlines()]
        self.assertTrue(any(event["type"] == "PROCESS_HEARTBEAT" for event in logged))


if __name__ == "__main__":
    unittest.main()
