"""Isolated local media exercise saved-background scheduling; not production acceptance."""
import asyncio
import copy
import json
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app.domain import find, now
from app.integrations.asset_registry import file_hash
from app.integrations.manual_compose import ComposeRequest
from tests import test_manual_compose as manual_fixture


class SavedBackgroundComposeTests(unittest.IsolatedAsyncioTestCase):
    ffmpeg = manual_fixture.ManualComposeTests.ffmpeg
    upload = manual_fixture.ManualComposeTests.upload
    pair = manual_fixture.ManualComposeTests.pair
    background = manual_fixture.ManualComposeTests.background

    async def asyncSetUp(self):
        await manual_fixture.ManualComposeTests.asyncSetUp(self)
        self.production = self.service.production
        self.production.closing = False
        self.production.jobs, self.production.cancels = {}, {}
        self.production.recovered_batches, self.production.recovered_run_ids, self.production.recovered_item_ids = [], set(), set()
        self.production._saved_context = AsyncMock()
        self.production._quality = AsyncMock(return_value={"id": "unit-quality", "status": "FAIL", "processing_run_id": "unit-quality-run"})
        self.production.launch = AsyncMock()
        self.person = await self.pair()
        self.source = self.production.registry.register(self.repository.read_task(self.task_id), {
            "path": json.loads(Path(self.person["legacy_reference"]["manifest_path"]).read_text())["source_path"], "source_system": "LegacyCollectorAdapter",
            "legacy_video_id": "1234567890123456789"}, "source", "unit-import-run")
        self.backgrounds = []
        def normal(state):
            task = find(state, "tasks", self.task_id)
            task["context"]["task_context"].update(context_id="manual-unit-context", country=task["country"], keyword=task["keyword"],
                product_id=task["product_id"], source_document="manual", source_row=1)
            task["collector_result"] = {"video_id": "1234567890123456789", "context_id": "manual-unit-context", "decision": "accepted"}
            task["context"]["production"]["results"] = {"prompt": {"status": "READY", "context_id": "manual-unit-context", "run_id": "unit-prompt-run"}}
            find(state, "assets", self.person["id"])["legacy_reference"]["source_sha256"] = self.source["sha256"]
            find(state, "assets", self.source["id"])["legacy_video_id"] = "1234567890123456789"
            for ordinal in range(1, 21):
                state["prompts"].append({"id": f"unit-prompt-{ordinal}", "task_id": self.task_id, "ordinal": ordinal,
                    "prompt": f"Isolated scene {ordinal}", "negative_prompt": "no people", "status": "READY", "provider": "chatgpt_rpa",
                    "scene_type": "unit", "style": "", "lighting": "", "color": "", "aspect_ratio": "9:16", "created_at": now(),
                    "processing_run_id": "unit-prompt-run", "fingerprint": f"unit-{ordinal}"})
        self.repository.mutate(normal)
        for ordinal in range(1, 21):
            image = self.settings.storage_root / f"background-{ordinal:02d}.png"
            self.ffmpeg("-f", "lavfi", "-i", f"color=0x{ordinal * 10:02x}0010:s=64x96", "-frames:v", "1", str(image))
            self.backgrounds.append(self.production.registry.register(self.repository.read_task(self.task_id), {
                "path": str(image), "source_system": "BackgroundRPAAdapter", "legacy_video_id": "1234567890123456789",
                "legacy_reference": {"context_id": "manual-unit-context", "video_id": "1234567890123456789"}}, "background",
                "unit-background-run", prompt_id=f"unit-prompt-{ordinal}"))

    async def asyncTearDown(self):
        await self.service.shutdown()

    async def first(self):
        with patch.object(self.service, "schedule_saved_quality", new=AsyncMock()):
            outcome = await self.service.render(ComposeRequest(task_id=self.task_id, person_asset_id=self.person["id"],
                background_asset_id=self.backgrounds[0]["id"], width=64, height=96))
        return outcome

    def quality(self):
        from app.integrations.quality_adapter import CutoutQualityAdapter
        saved = self.service.saved_backgrounds(self.task_id)
        path = self.settings.storage_root / "unit-quality.json"
        path.write_text(json.dumps({"mode": "REAL", "provider": "OpenCVCutoutQualityAnalyzer", "status": "FAIL",
            "fingerprint": "unit-only-quality", "input_sha256": saved["alpha_sha256"],
            "thresholds": CutoutQualityAdapter(self.settings).thresholds}))
        self.service._saved_item_update(self.task_id, saved["id"], quality={"status": "FAIL", "geometry_status": "GEOMETRY_FAIL",
            "input_sha256": saved["alpha_sha256"], "fingerprint": "unit-only-quality", "report_path": str(path), "report_sha256": file_hash(path)})

    async def authorize(self):
        first = await self.first()
        self.quality()
        response = await self.service.approve(first["review"]["id"])
        return first, response

    async def test_first_requires_real_pending_review_and_quality_not_unknown(self):
        outcome = await self.first()
        saved = self.service.saved_backgrounds(self.task_id)
        self.assertEqual(saved["status"], "WAITING_FIRST_REVIEW")
        self.assertEqual(len(saved["items"]), 20)
        with self.assertRaises(HTTPException):
            self.service.validate_saved_backgrounds(self.repository.read_task(self.task_id))
        with self.assertRaises(HTTPException) as caught:
            await self.service.approve(outcome["review"]["id"])
        self.assertIn("QUALITY_REQUIRED", caught.exception.detail)
        self.assertEqual(next(row for row in self.repository.read_reviews(self.task_id) if row["id"] == outcome["review"]["id"])["status"], "pending")
        self.production.launch.assert_not_awaited()

    async def test_approve_queues_existing_scheduler_without_clearing_manual_guard(self):
        first, result = await self.authorize()
        saved = self.service.saved_backgrounds(self.task_id)
        self.assertEqual(saved["approved_review_id"], first["review"]["id"])
        self.assertTrue(result["continuation"]["accepted"])
        task = self.repository.read_task(self.task_id)
        self.assertEqual(task["status"], "QUEUED")
        self.assertTrue(task["context"]["manual_compose"]["auto_resume_required"])
        self.assertEqual(task["context"]["production"]["requested_stage"], "saved_background_compose")
        self.production.launch.assert_awaited_once()

    async def test_late_prepare_uses_verified_user_approval_without_rerendering_first(self):
        first = await self.first()
        self.repository.mutate(lambda state: find(state, "tasks", self.task_id)["context"]["manual_compose"].pop("saved_backgrounds"))
        await self.service.approve(first["review"]["id"])
        self.assertEqual(self.repository.read_task(self.task_id)["status"], "COMPLETED")
        before = len(self.repository.read_assets(task_id=self.task_id))
        with patch.object(self.service, "schedule_saved_quality", new=AsyncMock()), patch.object(self.service, "render", side_effect=AssertionError("no render")):
            result = await self.service.prepare_latest(self.task_id)
        self.assertEqual(result["saved_backgrounds"]["first_composition_asset_id"], first["asset"]["id"])
        self.assertEqual(len(self.repository.read_assets(task_id=self.task_id)), before)

    async def test_same_person_local_outputs_failure_isolation_and_safe_pause_boundary(self):
        first, result = await self.authorize()
        batch_id = result["continuation"]["batch_id"]
        self.production.cancels[batch_id] = threading.Event()
        self.production._claim_next(batch_id)
        original = self.service.render
        calls = []
        async def limited(request, **kwargs):
            ordinal = next(index + 1 for index, row in enumerate(self.backgrounds) if row["id"] == request.background_asset_id)
            calls.append(ordinal)
            if ordinal == 2:
                self.service.finish(self.task_id, kwargs["run_id"], failure="unit isolated failure")
                raise HTTPException(422, "unit isolated failure")
            output = await original(request, **kwargs)
            self.production._batch_update(batch_id, status="PAUSED")
            return output
        with patch.object(self.service, "render", side_effect=limited):
            job = asyncio.create_task(self.service.compose_saved_backgrounds(batch_id, self.task_id))
            for _ in range(120):
                if self.service.saved_backgrounds(self.task_id)["items"][2]["status"] == "READY":
                    break
                await asyncio.sleep(.1)
            await asyncio.sleep(.3)
            self.assertEqual(calls, [2, 3])
            saved = self.service.saved_backgrounds(self.task_id)
            self.assertEqual(saved["items"][1]["status"], "FAILED")
            third = self.repository.read_asset(saved["items"][2]["asset_id"])
            self.assertEqual(third["legacy_reference"]["person_asset_id"], self.person["id"])
            self.assertEqual(first["asset"]["legacy_reference"]["person_asset_id"], self.person["id"])
            manifest = json.loads(Path(third["legacy_reference"]["manifest_path"]).read_text())
            self.assertEqual(manifest["cutout_inference_executions"], 0)
            self.production.cancels[batch_id].set()
            from app.integrations.production_service import ProductionStopped
            with self.assertRaises(ProductionStopped):
                await job
        self.production.cancels.pop(batch_id)

    async def test_ordinary_import_and_missing_source_association_never_expand(self):
        self.repository.mutate(lambda state: find(state, "tasks", self.task_id).update(collector_result={}))
        first = await self.first()
        self.assertIsNone(self.service.saved_backgrounds(self.task_id))
        await self.service.approve(first["review"]["id"])
        self.production.launch.assert_not_awaited()

    async def test_saved_input_change_and_output_tampering_do_not_reuse(self):
        first, _ = await self.authorize()
        saved = self.service.saved_backgrounds(self.task_id)
        Path(first["asset"]["storage_path"]).write_bytes(b"unit changed output")
        with self.assertRaises(HTTPException):
            self.service.validate_saved_backgrounds(self.repository.read_task(self.task_id))

    async def test_prepare_runs_local_quality_and_preserves_machine_result_without_approving(self):
        first = await self.first()
        result = await self.service.prepare_latest(self.task_id)
        run_id = result["saved_backgrounds"]["quality"]["run_id"]
        job = self.service.jobs.get(run_id)
        if job:
            await job
        saved = self.service.saved_backgrounds(self.task_id)
        self.assertIn(saved["quality"]["status"], {"PASS", "FAIL"})
        report = Path(saved["quality"]["report_path"])
        self.assertEqual(saved["quality"]["report_sha256"], file_hash(report))
        data = json.loads(report.read_text())
        self.assertEqual(data["input_sha256"], self.person["legacy_reference"]["alpha_sha256"])
        self.assertEqual(data["status"], saved["quality"]["status"])
        self.assertEqual(saved["status"], "WAITING_FIRST_REVIEW")
        self.assertEqual(next(row for row in self.repository.read_reviews(self.task_id) if row["id"] == first["review"]["id"])["status"], "pending")
        self.production.launch.assert_not_awaited()

    async def test_approve_joins_running_batch_at_boundary_without_parallel_launch(self):
        self.repository.mutate(lambda state: state["batches"].append({"id": "unit-collector-batch", "status": "RUNNING",
            "task_ids": [state["tasks"][0]["id"]], "items": [{"task_id": state["tasks"][0]["id"], "status": "RUNNING", "stage": "collector"}],
            "active_task_id": state["tasks"][0]["id"], "summary": {}, "created_at": now(), "updated_at": now()}))
        _, result = await self.authorize()
        self.assertEqual(result["continuation"]["batch_id"], "unit-collector-batch")
        batch = next(row for row in self.repository.state()["batches"] if row["id"] == "unit-collector-batch")
        self.assertEqual(batch["status"], "RUNNING")
        self.assertNotEqual(batch["active_task_id"], self.task_id)
        self.assertEqual(batch["items"][0]["task_id"], self.task_id)
        self.assertEqual(batch["items"][0]["status"], "QUEUED")
        self.assertEqual(len(self.repository.state()["batches"]), 1)

    async def test_missing_background_inputs_do_not_start_quality(self):
        await self.first()
        def incomplete(state):
            find(state, "tasks", self.task_id)["context"]["manual_compose"].pop("saved_backgrounds")
            find(state, "prompts", "unit-prompt-20")["status"] = "FAILED"
        self.repository.mutate(incomplete)
        with patch.object(self.service, "schedule_saved_quality", new=AsyncMock()) as schedule:
            result = await self.service.prepare_latest(self.task_id)
        self.assertEqual(result["saved_backgrounds"]["status"], "WAITING_INPUTS")
        schedule.assert_not_awaited()
        self.assertFalse(any(run["stage"] == "quality" for run in self.repository.state()["runs"]))

    async def test_explicit_prepare_reports_missing_normal_source_context(self):
        await self.first()
        self.repository.mutate(lambda state: find(state, "tasks", self.task_id).update(collector_result={}))
        with self.assertRaises(HTTPException) as caught:
            await self.service.prepare_latest(self.task_id)
        self.assertIn("SOURCE_CONTEXT_REQUIRED", caught.exception.detail)

    async def test_checked_asset_uses_single_asset_read_not_full_state(self):
        with patch.object(self.repository, "state", side_effect=AssertionError("no full state")):
            checked, path = self.service.checked_asset(self.task_id, self.backgrounds[0]["id"], "background")
        self.assertEqual(checked["sha256"], file_hash(path))

    async def test_authorized_crash_before_queue_handoff_recovers_same_intent(self):
        _, response = await self.authorize()
        batch_id = response["continuation"]["batch_id"]
        intent_id = self.service.saved_backgrounds(self.task_id)["id"]
        def interrupted(state):
            find(state, "batches", batch_id)["status"] = "COMPLETED_WITH_ERRORS"
            task = find(state, "tasks", self.task_id)
            task.update(status="WAITING_REVIEW")
            task["context"]["production"].pop("requested_stage")
        self.repository.mutate(interrupted)
        self.production.launch.reset_mock()
        await self.production.resume_recovered()
        task = self.repository.read_task(self.task_id)
        self.assertEqual(task["context"]["production"]["requested_stage"], "saved_background_compose")
        self.assertEqual(self.service.saved_backgrounds(self.task_id)["id"], intent_id)
        self.production.launch.assert_awaited_once()

    async def test_authorized_crash_recovery_preserves_origin_pause_cancel(self):
        _, response = await self.authorize()
        batch_id = response["continuation"]["batch_id"]
        for status in ("PAUSED", "CANCELLED"):
            def stopped(state):
                find(state, "batches", batch_id)["status"] = status
                task = find(state, "tasks", self.task_id)
                task.update(status="WAITING_REVIEW")
                task["context"]["production"].pop("requested_stage", None)
            self.repository.mutate(stopped)
            self.production.launch.reset_mock()
            await self.production.resume_recovered()
            self.production.launch.assert_not_awaited()
            self.assertNotIn("requested_stage", self.repository.read_task(self.task_id)["context"]["production"])

    async def test_quality_runtime_error_is_persisted_unknown_not_running(self):
        await self.first()
        with patch("app.integrations.manual_compose.CutoutQualityAdapter.analyze", new=AsyncMock(side_effect=RuntimeError("unit adapter failure"))):
            response = await self.service.prepare_latest(self.task_id)
            run_id = response["saved_backgrounds"]["quality"]["run_id"]
            await self.service.jobs[run_id]
        self.assertEqual(self.service.saved_backgrounds(self.task_id)["quality"]["status"], "UNKNOWN")
        self.assertEqual(self.repository.read_run(run_id)["status"], "failed")
        self.production.launch.assert_not_awaited()

    async def test_runtime_render_failure_finishes_run_and_continues_next_background(self):
        _, response = await self.authorize()
        batch_id = response["continuation"]["batch_id"]
        self.production.cancels[batch_id] = threading.Event()
        self.production._claim_next(batch_id)
        original = self.service.execute
        calls = []
        async def isolated(task_id, run_id, stage, command, directory):
            item = next(row for row in self.service.saved_backgrounds(self.task_id)["items"] if row.get("run_id") == run_id)
            calls.append(item["ordinal"])
            if item["ordinal"] == 2:
                raise RuntimeError("unit callback failure")
            result = await original(task_id, run_id, stage, command, directory)
            self.production._batch_update(batch_id, status="PAUSED")
            return result
        with patch.object(self.service, "execute", side_effect=isolated):
            job = asyncio.create_task(self.service.compose_saved_backgrounds(batch_id, self.task_id))
            for _ in range(120):
                if self.service.saved_backgrounds(self.task_id)["items"][2]["status"] == "READY":
                    break
                await asyncio.sleep(.1)
            self.assertEqual(calls, [2, 3])
            failed = self.service.saved_backgrounds(self.task_id)["items"][1]
            self.assertEqual(failed["status"], "FAILED")
            self.assertEqual(self.repository.read_run(failed["run_id"])["status"], "failed")
            self.production.cancels[batch_id].set()
            from app.integrations.production_service import ProductionStopped
            with self.assertRaises(ProductionStopped):
                await job
        self.production.cancels.pop(batch_id)

    async def test_ready_output_tampering_isolated_without_rerender_and_next_continues(self):
        _, response = await self.authorize()
        batch_id = response["continuation"]["batch_id"]
        self.production.cancels[batch_id] = threading.Event()
        self.production._claim_next(batch_id)
        saved = self.service.saved_backgrounds(self.task_id)
        task = self.service.task(self.task_id, own_batch=batch_id)
        run_id, directory = self.service.begin(task, "compose", "FactoryManualCompose")
        second = await self.service.render(ComposeRequest(task_id=self.task_id, person_asset_id=self.person["id"],
            background_asset_id=self.backgrounds[1]["id"], width=64, height=96), run_id=run_id, directory=directory,
            own_batch=batch_id, saved_intent_id=saved["id"])
        self.service._saved_item_update(self.task_id, saved["id"], 2, status="READY", run_id=run_id,
            asset_id=second["asset"]["id"], output_sha256=second["asset"]["sha256"], review_id=second["review"]["id"])
        Path(second["asset"]["storage_path"]).write_bytes(b"unit altered output")
        original = self.service.render
        calls = []
        async def limited(request, **kwargs):
            calls.append(request.background_asset_id)
            result = await original(request, **kwargs)
            self.production._batch_update(batch_id, status="PAUSED")
            return result
        with patch.object(self.service, "render", side_effect=limited):
            job = asyncio.create_task(self.service.compose_saved_backgrounds(batch_id, self.task_id))
            for _ in range(120):
                if self.service.saved_backgrounds(self.task_id)["items"][2]["status"] == "READY":
                    break
                await asyncio.sleep(.1)
            self.assertEqual(calls, [self.backgrounds[2]["id"]])
            failed = self.service.saved_backgrounds(self.task_id)["items"][1]
            self.assertEqual(failed["status"], "FAILED")
            self.assertEqual(failed["asset_id"], second["asset"]["id"])
            self.production.cancels[batch_id].set()
            from app.integrations.production_service import ProductionStopped
            with self.assertRaises(ProductionStopped):
                await job
        self.production.cancels.pop(batch_id)

    async def test_implementation_is_owned_by_loaded_service_not_live_source_file(self):
        await self.authorize()
        from app.integrations import manual_compose
        original = file_hash
        def unchanged_inputs(path):
            if Path(path) == Path(manual_compose.__file__):
                raise AssertionError("must not hash mutable source during execution")
            return original(path)
        with patch("app.integrations.manual_compose.file_hash", side_effect=unchanged_inputs):
            self.service.validate_saved_backgrounds(self.repository.read_task(self.task_id))

    async def test_old_unstarted_intent_is_replanned_with_history_not_relabelled(self):
        _, response = await self.authorize()
        previous = self.service.saved_backgrounds(self.task_id)
        def old_loaded_version(state):
            task = find(state, "tasks", self.task_id)
            task.update(status="WAITING_REVIEW")
            task["context"]["manual_compose"]["saved_backgrounds"]["implementation_sha256"] = "unit-old-implementation"
            find(state, "batches", response["continuation"]["batch_id"])["status"] = "COMPLETED_WITH_ERRORS"
        self.repository.mutate(old_loaded_version)
        before = len(self.repository.read_assets(task_id=self.task_id))
        with patch.object(self.service, "schedule_saved_quality", new=AsyncMock()):
            result = await self.service.prepare_latest(self.task_id)
        current = result["saved_backgrounds"]
        self.assertNotEqual(current["id"], previous["id"])
        self.assertEqual(current["implementation_sha256"], self.service.implementation_sha256)
        self.assertEqual(current["first_composition_asset_id"], previous["first_composition_asset_id"])
        self.assertEqual(len(self.repository.read_assets(task_id=self.task_id)), before)
        history = self.repository.read_task(self.task_id)["context"]["manual_compose"]["saved_background_history"]
        self.assertEqual(history[-1]["id"], previous["id"])
        self.assertEqual(history[-1]["implementation_sha256"], "unit-old-implementation")

    async def test_old_partially_executed_intent_is_not_replanned(self):
        _, response = await self.authorize()
        def old_loaded_version(state):
            task = find(state, "tasks", self.task_id)
            task.update(status="WAITING_REVIEW")
            saved = task["context"]["manual_compose"]["saved_backgrounds"]
            saved["implementation_sha256"] = "unit-old-implementation"
            saved["items"][1]["attempts"] = ["unit-prior-run"]
            find(state, "batches", response["continuation"]["batch_id"])["status"] = "COMPLETED_WITH_ERRORS"
        self.repository.mutate(old_loaded_version)
        with self.assertRaises(HTTPException) as caught:
            await self.service.prepare_latest(self.task_id)
        self.assertIn("IMPLEMENTATION_CHANGED", caught.exception.detail)
        self.assertEqual(self.service.saved_backgrounds(self.task_id)["items"][1]["attempts"], ["unit-prior-run"])
