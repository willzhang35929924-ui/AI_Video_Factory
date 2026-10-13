"""Isolated scheduling contracts; fixture hashes are not production media evidence."""

import asyncio
import copy
import hashlib
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app.integrations.asset_registry import file_hash
from app.integrations.matte_repair import MatteRepairService
from app.integrations.process_runner import ProcessResult
from test_real_review import ReviewFixture


class DeferredProductionContinueTests(ReviewFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.prepare("person")
        self.person = self.verified_person()
        self.production.jobs = {}
        self.production.cancels = {}
        self.production.closing = False
        self.production.recovered_batches = []
        self.production.recovered_run_ids = set()
        self.production.recovered_item_ids = set()
        self.production.launch = AsyncMock()
        self.repository.mutate(lambda state: state["settings"].update(production_policy={}))
        self.first_fixture()

    def production_context(self):
        return self.repository.state()["tasks"][0]["context"]["production"]

    def first_fixture(self):
        source = next(row for row in self.repository.state()["assets"] if row["type"] == "source")
        extra_assets = []
        for kind, identifier in (("background", "unit-background-01"), ("compose", "unit-composition-01")):
            path = self.production.settings.storage_root / (identifier + ".media")
            path.write_bytes(("isolated scheduling fixture " + identifier).encode())
            extra_assets.append({"id": identifier, "task_id": "unit-task", "type": kind,
                "status": "READY", "registry_status": "registered", "storage_path": str(path),
                "sha256": file_hash(path), "legacy_reference": {"prompt_id": "unit-prompt-01", "unit_fixture": True}})
        background, composition = extra_assets
        basis = {"source_sha256": source["sha256"], "person_asset_id": self.person["id"],
            "rgb_sha256": self.person["sha256"], "alpha_sha256": self.person["legacy_reference"]["alpha_sha256"],
            "background_asset_id": background["id"], "background_sha256": background["sha256"],
            "composition_asset_id": composition["id"], "composition_sha256": composition["sha256"]}
        first = {**basis, "status": "PASS", "human_review": "APPROVED", "unit_fixture": True,
            "fingerprint": hashlib.sha256(json.dumps(basis, sort_keys=True).encode()).hexdigest()}
        def save(state):
            state["assets"].extend(extra_assets)
            state["reviews"][0]["status"] = "approved"
            state["reviews"].append({"id": "unit-first-review", "task_id": "unit-task",
                "asset_id": composition["id"], "status": "approved", "unit_fixture": True})
            task = state["tasks"][0]
            task.update(current_stage="review")
            task["context"]["production"].update(first_composition=first, automatic=True)
        self.repository.mutate(save)
        self.assertTrue(self.production._first_review_approved("unit-task", self.person))

    def repair_fixture(self, status="running"):
        def save(state):
            state["runs"] = [{"id": "unit-preview-run", "task_id": "unit-task", "stage": "person",
                "provider": "FactoryMatteRepair", "status": status, "unit_fixture": True}]
            state["tasks"][0]["context"]["matte_repairs"] = [{"id": "unit-preview", "run_id": "unit-preview-run",
                "task_id": "unit-task", "person_asset_id": self.person["id"], "asset_id": None,
                "parent_rgb_sha256": self.person["sha256"],
                "parent_alpha_sha256": self.person["legacy_reference"]["alpha_sha256"],
                "execution_scope": "PRODUCTION", "status": "PROPAGATING" if status == "running" else "QUEUED",
                "unit_fixture": True}]
        self.repository.mutate(save)

    def finish_preview(self):
        def save(state):
            state["runs"][0].update(status="success")
            state["tasks"][0]["context"]["matte_repairs"][0].update(status="PREVIEW_READY", evidence={"unit_fixture": True})
        self.repository.mutate(save)

    def recovery_batch(self, *, status="RUNNING", item_status="WAITING_REVIEW"):
        def save(state):
            task = state["tasks"][0]
            task.update(status=item_status, current_stage="review")
            task["context"]["production"].update(batch_id="unit-batch", automatic=True,
                waiting={"reason": "FIRST_COMPOSITION_REVIEW_REQUIRED", "stage": "review"})
            state["batches"] = [{"id": "unit-batch", "status": status, "task_ids": ["unit-task"],
                "items": [{"task_id": "unit-task", "status": item_status, "stage": "review"}],
                "active_task_id": None, "summary": {}}]
        self.repository.mutate(save)

    async def test_running_or_queued_preview_saves_continue_without_stage_success(self):
        for status in ("queued", "running"):
            with self.subTest(status=status):
                self.repair_fixture(status)
                before = self.repository.state()
                outcome = await self.production.continue_task("unit-task", stage="quality")
                self.assertTrue(outcome.get("deferred"))
                self.assertFalse(outcome.get("accepted", False))
                self.assertEqual(outcome["status"], "WAITING_LOCAL_WORK")
                self.assertEqual(outcome["reason"], "LOCAL_REPAIR_PREVIEW_RUNNING")
                state = self.repository.state()
                intent = state["tasks"][0]["context"]["production"]["deferred_continue"]
                self.assertEqual(intent["stage"], "quality")
                self.assertEqual(intent["person_asset_id"], self.person["id"])
                self.assertEqual(intent["rgb_sha256"], self.person["sha256"])
                self.assertEqual(intent["alpha_sha256"], self.person["legacy_reference"]["alpha_sha256"])
                self.assertEqual(intent["first_human_review"], "APPROVED")
                self.assertEqual(intent["first_fingerprint"], self.production_context()["first_composition"]["fingerprint"])
                self.assertEqual(intent["first_composition_asset_id"], "unit-composition-01")
                self.assertEqual(intent["repair_run_ids"], ["unit-preview-run"])
                self.assertEqual(state["tasks"][0]["context"]["production"]["waiting"]["status"], "WAITING_LOCAL_WORK")
                self.assertEqual(state["tasks"][0]["context"]["production"]["waiting"]["reason"], "LOCAL_REPAIR_PREVIEW_RUNNING")
                self.assertEqual(state["stages"], before["stages"])
                self.assertEqual(state["runs"], before["runs"])
                self.assertEqual(state["assets"], before["assets"])
                self.assertEqual(state["batches"], [])
                self.production.launch.assert_not_awaited()

    async def test_other_active_work_does_not_create_deferred_continue(self):
        for provider, stage, extra_run in (("LegacyComfyUIAdapter", "cutout", False),
                ("FactoryMatteRepair", "quality", False), ("FactoryMatteRepair", "person", True)):
            with self.subTest(provider=provider, stage=stage, extra_run=extra_run):
                self.repair_fixture()
                def change(state):
                    state["tasks"][0]["context"]["production"].pop("deferred_continue", None)
                    state["runs"][0].update(provider=provider, stage=stage)
                    if extra_run:
                        state["runs"].append({"id": "unit-other-run", "task_id": "unit-task", "stage": "compose",
                            "provider": "LegacyComposeAdapter", "status": "running", "unit_fixture": True})
                self.repository.mutate(change)
                outcome = await self.production.continue_task("unit-task")
                self.assertFalse(outcome.get("deferred", False))
                self.assertFalse(outcome.get("accepted", False))
                self.assertNotIn("deferred_continue", self.production_context())
                self.assertEqual(self.repository.state()["batches"], [])

    async def test_auto_advance_disabled_saves_no_intent_and_starts_no_work(self):
        self.repair_fixture()
        self.repository.mutate(lambda state: state["settings"]["production_policy"].update(auto_advance=False))
        outcome = await self.production.continue_task("unit-task")
        self.assertFalse(outcome.get("deferred", False))
        self.assertFalse(outcome.get("accepted", False))
        self.assertNotIn("deferred_continue", self.production_context())
        self.assertEqual(self.repository.state()["batches"], [])
        self.production.launch.assert_not_awaited()

    async def test_completed_preview_consumes_intent_once_through_formal_queue(self):
        self.repair_fixture()
        await self.production.continue_task("unit-task", stage="quality")
        self.finish_preview()
        before = self.repository.state()
        with patch.object(self.production, "continue_task", wraps=self.production.continue_task) as continuation:
            await self.production.consume_deferred_continue("unit-task")
            await self.production.consume_deferred_continue("unit-task")
        continuation.assert_awaited_once()
        self.assertEqual(continuation.await_args.args, ("unit-task",))
        self.assertEqual(continuation.await_args.kwargs["stage"], "quality")
        state = self.repository.state()
        self.assertEqual(len(state["batches"]), 1)
        self.assertEqual(state["batches"][0]["task_ids"], ["unit-task"])
        self.production.launch.assert_awaited_once_with(state["batches"][0]["id"])
        self.assertEqual(state["tasks"][0]["status"], "QUEUED")
        self.assertEqual(state["tasks"][0]["context"]["production"]["requested_stage"], "quality")
        self.assertEqual(state["tasks"][0]["context"]["production"]["current_person_asset_id"], self.person["id"])
        self.assertEqual(state["tasks"][0]["context"]["matte_repairs"][0]["status"], "PREVIEW_READY")
        self.assertIsNone(state["tasks"][0]["context"]["matte_repairs"][0]["asset_id"])
        self.assertEqual(state["assets"], before["assets"])
        self.assertEqual(state["runs"], before["runs"])
        self.assertEqual(state["stages"], before["stages"])

    async def test_active_preview_leaves_saved_intent_unconsumed(self):
        self.repair_fixture()
        await self.production.continue_task("unit-task")
        saved = copy.deepcopy(self.production_context()["deferred_continue"])
        await self.production.consume_deferred_continue("unit-task")
        self.assertEqual(self.production_context()["deferred_continue"], saved)
        self.assertEqual(self.repository.state()["batches"], [])
        self.production.launch.assert_not_awaited()

    async def test_changed_rgb_or_alpha_blocks_deferred_continue(self):
        for path in (Path(self.person["storage_path"]), Path(self.person["legacy_reference"]["alpha_path"])):
            with self.subTest(path=path.name):
                original = path.read_bytes()
                self.repair_fixture()
                await self.production.continue_task("unit-task")
                self.finish_preview()
                path.write_bytes(b"changed isolated fixture")
                try:
                    await self.production.consume_deferred_continue("unit-task")
                    self.assertEqual(self.repository.state()["batches"], [])
                    self.production.launch.assert_not_awaited()
                finally:
                    path.write_bytes(original)

    async def test_changed_mother_or_first_approval_blocks_deferred_continue(self):
        original = copy.deepcopy(self.production_context())
        for changed in ("person", "fingerprint", "review"):
            with self.subTest(changed=changed):
                self.repository.mutate(lambda state: state["tasks"][0]["context"].update(production=copy.deepcopy(original)))
                self.repair_fixture()
                await self.production.continue_task("unit-task")
                self.finish_preview()
                def change(state):
                    production = state["tasks"][0]["context"]["production"]
                    if changed == "person":
                        production["current_person_asset_id"] = "unit-another-person"
                    elif changed == "fingerprint":
                        production["first_composition"]["fingerprint"] = "changed-unit-fingerprint"
                    else:
                        production["first_composition"]["human_review"] = "PENDING"
                self.repository.mutate(change)
                await self.production.consume_deferred_continue("unit-task")
                self.assertEqual(self.repository.state()["batches"], [])
                self.production.launch.assert_not_awaited()

    async def test_cancel_reject_unknown_and_policy_change_hold_deferred_continue(self):
        initial = self.repository.state()
        for guard in ("cancel", "reject", "rejected_review", "unknown", "auto_advance"):
            with self.subTest(guard=guard):
                self.repository.value = copy.deepcopy(initial)
                self.repair_fixture()
                await self.production.continue_task("unit-task")
                self.finish_preview()
                def change(state):
                    if guard in {"cancel", "reject"}:
                        state["tasks"][0]["status"] = "CANCELLED" if guard == "cancel" else "REJECTED"
                    elif guard == "rejected_review":
                        state["tasks"][0]["context"]["production"]["rejected_review"] = {"asset_id": self.person["id"]}
                    elif guard == "unknown":
                        state["runs"].append({"id": "unit-paid-unknown", "task_id": "unit-task", "stage": "background",
                            "provider": "BackgroundRPAAdapter", "status": "failed", "error_code": "UNKNOWN_EXTERNAL_REQUEST"})
                    else:
                        state["settings"]["production_policy"]["auto_advance"] = False
                self.repository.mutate(change)
                await self.production.consume_deferred_continue("unit-task")
                self.assertEqual(self.repository.state()["batches"], [])
                self.production.launch.assert_not_awaited()

    async def test_paused_batch_is_preserved_when_preview_finishes(self):
        self.repair_fixture()
        await self.production.continue_task("unit-task")
        self.finish_preview()
        self.recovery_batch(status="PAUSED")
        before = self.repository.state()["batches"]
        await self.production.consume_deferred_continue("unit-task")
        self.assertEqual(self.repository.state()["batches"], before)
        self.assertNotEqual(self.repository.state()["tasks"][0]["status"], "QUEUED")
        self.production.launch.assert_not_awaited()

    async def test_cancelled_batch_does_not_launch_deferred_work(self):
        self.repair_fixture()
        await self.production.continue_task("unit-task")
        self.finish_preview()
        self.recovery_batch(status="CANCELLED")
        await self.production.consume_deferred_continue("unit-task")
        self.production.launch.assert_not_awaited()
        self.assertEqual(self.repository.state()["batches"][0]["status"], "CANCELLED")
        self.assertNotEqual(self.repository.state()["tasks"][0]["status"], "QUEUED")

    async def test_user_pause_after_restart_overrides_old_recovered_batch_allowlist(self):
        self.recovery_batch()
        await self.production.recover()
        await self.production.resume_recovered()
        self.production.launch.reset_mock()
        self.repair_fixture()
        await self.production.continue_task("unit-task")
        await self.production.control("pause")
        self.production.recovered_batches = ["unit-batch"]
        self.finish_preview()
        before = self.repository.state()["batches"]
        await self.production.consume_deferred_continue("unit-task")
        self.assertEqual(self.repository.state()["batches"], before)
        self.assertNotEqual(self.repository.state()["tasks"][0]["status"], "QUEUED")
        self.production.launch.assert_not_awaited()

    async def test_preview_completion_hook_runs_after_success_or_failure_without_apply(self):
        for terminal in ("success", "failed"):
            with self.subTest(terminal=terminal):
                self.repair_fixture("queued")
                directory = self.production.settings.storage_root / ("unit-preview-" + terminal)
                self.repository.mutate(lambda state: state["tasks"][0]["context"]["matte_repairs"][0].update(
                    directory=str(directory), keyframes=[], start_frame=0, end_frame=1, direction="both"))
                repair = MatteRepairService(self.production)
                repair.cancels["unit-preview"] = threading.Event()
                self.production.settings.quality_python = Path("/isolated-unit-python")
                before = self.repository.state()["assets"]
                pair = (self.repository.state()["tasks"][0], self.person, Path(self.person["storage_path"]),
                    Path(self.person["legacy_reference"]["alpha_path"]), {"unit_fixture": True})
                async def consumed(task_id):
                    self.assertEqual(task_id, "unit-task")
                    state = self.repository.state()
                    self.assertEqual(state["runs"][0]["status"], terminal)
                    self.assertEqual(state["tasks"][0]["context"]["matte_repairs"][0]["status"],
                        "PREVIEW_READY" if terminal == "success" else "FAILED")
                    self.assertNotIn("unit-preview", repair.cancels)
                def run(*args, **kwargs):
                    summary = {"status": "PREVIEW_READY" if terminal == "success" else "FAILED",
                        "error_code": "UNIT_PREVIEW_FAILURE" if terminal == "failed" else None, "unit_fixture": True}
                    (kwargs["cwd"] / "result.json").write_text(json.dumps(summary))
                    return ProcessResult(pid=123456, exit_code=0 if terminal == "success" else 1,
                        stdout="", stderr="", log_path="", duration_s=0)
                with patch.object(repair, "pair", return_value=pair), patch(
                        "app.integrations.matte_repair.ProcessRunner.run", side_effect=run), patch.object(
                        self.production, "consume_deferred_continue", new=AsyncMock(side_effect=consumed)) as consume:
                    await repair.run("unit-task", "unit-preview")
                consume.assert_awaited_once_with("unit-task")
                self.assertEqual(self.repository.state()["assets"], before)
                self.assertIsNone(self.repository.state()["tasks"][0]["context"]["matte_repairs"][0]["asset_id"])
                self.production.launch.assert_not_awaited()

    async def test_repair_shutdown_keeps_pending_continue_without_launching_production(self):
        self.repair_fixture("queued")
        directory = self.production.settings.storage_root / "unit-shutdown-preview"
        self.repository.mutate(lambda state: state["tasks"][0]["context"]["matte_repairs"][0].update(
            directory=str(directory), keyframes=[], start_frame=0, end_frame=1, direction="both"))
        await self.production.continue_task("unit-task")
        intent = copy.deepcopy(self.production_context()["deferred_continue"])
        repair = MatteRepairService(self.production)
        self.production.settings.quality_python = Path("/isolated-unit-python")
        entered = threading.Event()
        pair = (self.repository.state()["tasks"][0], self.person, Path(self.person["storage_path"]),
            Path(self.person["legacy_reference"]["alpha_path"]), {"unit_fixture": True})
        def run(*args, **kwargs):
            entered.set()
            self.assertTrue(kwargs["cancel_event"].wait(2))
            return ProcessResult(pid=123456, exit_code=130, stdout="", stderr="", log_path="",
                duration_s=0, cancelled=True)
        with patch.object(repair, "pair", return_value=pair), patch(
                "app.integrations.matte_repair.ProcessRunner.run", side_effect=run), patch.object(
                self.production, "consume_deferred_continue", new=AsyncMock()) as consume:
            await repair.launch("unit-task", "unit-preview")
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            await repair.shutdown()
        self.assertTrue(repair.closing)
        self.assertFalse(self.production.closing)
        consume.assert_not_awaited()
        self.assertEqual(self.production_context()["deferred_continue"], intent)
        self.assertEqual(self.repository.state()["runs"][0]["error_code"], "PROCESS_CANCELLED")
        self.assertEqual(self.repository.state()["batches"], [])
        self.production.launch.assert_not_awaited()

    async def test_production_shutdown_retains_pending_intent_after_preview_completion(self):
        self.repair_fixture()
        await self.production.continue_task("unit-task")
        self.finish_preview()
        intent = copy.deepcopy(self.production_context()["deferred_continue"])
        self.production.closing = True
        await self.production.consume_deferred_continue("unit-task")
        self.assertEqual(self.production_context()["deferred_continue"], intent)
        self.assertEqual(self.repository.state()["batches"], [])
        self.production.launch.assert_not_awaited()

    async def test_missing_or_unreadable_parent_blocks_intent_without_completion_crash(self):
        initial = self.repository.state()
        for error in (OSError("isolated missing parent"), HTTPException(404, "isolated missing parent")):
            with self.subTest(error=type(error).__name__):
                self.repository.value = copy.deepcopy(initial)
                self.repair_fixture()
                await self.production.continue_task("unit-task")
                self.finish_preview()
                with patch.object(self.production.registry, "content", side_effect=error):
                    await self.production.consume_deferred_continue("unit-task")
                self.assertEqual(self.production_context()["deferred_continue"]["status"], "BLOCKED")
                self.assertEqual(self.production_context()["waiting"]["reason"], "DEFERRED_INPUT_CHANGED")
                self.assertEqual(self.repository.state()["batches"], [])
                self.production.launch.assert_not_awaited()

    async def test_latest_pending_or_rejected_first_review_holds_deferred_continue(self):
        initial = self.repository.state()
        for status in ("pending", "rejected"):
            with self.subTest(status=status):
                self.repository.value = copy.deepcopy(initial)
                self.repair_fixture()
                await self.production.continue_task("unit-task")
                self.finish_preview()
                self.repository.mutate(lambda state: state["reviews"].append({
                    "id": "review-20261008100000000000-new", "task_id": "unit-task",
                    "asset_id": "unit-composition-01", "status": status, "unit_fixture": True}))
                await self.production.consume_deferred_continue("unit-task")
                self.production.launch.assert_not_awaited()
                self.assertEqual(self.repository.state()["batches"], [])

    async def test_restart_resumes_verified_approved_first_held_by_stale_wait(self):
        self.recovery_batch()
        before = self.repository.state()
        await self.production.recover()
        await self.production.resume_recovered()
        state = self.repository.state()
        self.assertEqual(state["tasks"][0]["status"], "QUEUED")
        self.assertEqual(state["batches"][0]["status"], "QUEUED")
        self.assertEqual(state["batches"][0]["items"][0]["status"], "QUEUED")
        self.production.launch.assert_awaited_once_with("unit-batch")
        self.assertEqual(state["assets"], before["assets"])
        self.assertEqual(state["runs"], before["runs"])
        self.assertEqual(state["stages"], before["stages"])

    async def test_idle_recovery_reads_once_without_rewriting_history(self):
        for completed_batch in (False, True):
            with self.subTest(completed_batch=completed_batch):
                if completed_batch:
                    self.recovery_batch(status="COMPLETED_WITH_ERRORS")
                self.repository.mutate(lambda state: state.update(runs=[{
                    "id": "unit-finished-run", "task_id": "unit-task", "stage": "compose",
                    "provider": "LegacyComposeAdapter", "status": "success", "unit_fixture": True}]))
                before = self.repository.state()
                with patch.object(self.repository, "state", wraps=self.repository.state) as read, patch.object(
                        self.repository, "mutate", wraps=self.repository.mutate) as mutate:
                    await self.production.recover(active_run_ids=set())
                    read.assert_called_once_with()
                    mutate.assert_not_called()
                self.assertEqual(self.repository.state(), before)
                self.assertEqual(self.production.recovered_batches, [])
                self.assertEqual(self.production.recovered_run_ids, set())
                self.assertEqual(self.production.recovered_item_ids, set())
                self.production.launch.assert_not_awaited()

    async def test_idle_recovery_still_checks_approved_first_for_continuation(self):
        self.recovery_batch(status="COMPLETED_WITH_ERRORS")
        before = self.repository.state()
        with patch.object(self.repository, "mutate", wraps=self.repository.mutate) as mutate:
            await self.production.recover()
            mutate.assert_not_called()
        await self.production.resume_recovered()
        state = self.repository.state()
        self.production.launch.assert_awaited_once()
        self.assertEqual(state["tasks"][0]["status"], "QUEUED")
        self.assertEqual(state["assets"], before["assets"])
        self.assertEqual(state["runs"], before["runs"])
        self.assertTrue(any(row["action"] == "production.approval_continue_recovered" for row in state["audits"]))

    async def test_idle_recovery_still_consumes_persisted_deferred_continue(self):
        self.repair_fixture()
        await self.production.continue_task("unit-task")
        self.finish_preview()
        self.recovery_batch(status="COMPLETED_WITH_ERRORS")
        before = self.repository.state()
        with patch.object(self.repository, "mutate", wraps=self.repository.mutate) as mutate:
            await self.production.recover()
            mutate.assert_not_called()
        await self.production.resume_recovered()
        state = self.repository.state()
        self.production.launch.assert_awaited_once()
        self.assertEqual(state["tasks"][0]["status"], "QUEUED")
        self.assertEqual(state["tasks"][0]["context"]["production"]["deferred_continue"]["status"], "CONSUMED")
        self.assertEqual(state["assets"], before["assets"])
        self.assertEqual(state["runs"], before["runs"])

    async def test_restart_does_not_trust_approved_label_after_first_media_changes(self):
        for identifier in (self.person["id"], "unit-source", "unit-background-01", "unit-composition-01"):
            with self.subTest(identifier=identifier):
                asset = next(row for row in self.repository.state()["assets"] if row["id"] == identifier)
                path = Path(asset["storage_path"])
                original = path.read_bytes()
                self.recovery_batch()
                path.write_bytes(b"changed isolated first media")
                try:
                    await self.production.recover()
                    await self.production.resume_recovered()
                    self.production.launch.assert_not_awaited()
                    self.assertEqual(self.repository.state()["batches"][0]["status"], "PAUSED")
                finally:
                    path.write_bytes(original)

    async def test_restart_approval_requires_human_review_record(self):
        self.recovery_batch()
        self.repository.mutate(lambda state: state.update(reviews=[state["reviews"][0]]))
        await self.production.recover()
        await self.production.resume_recovered()
        self.production.launch.assert_not_awaited()
        self.assertNotEqual(self.repository.state()["tasks"][0]["status"], "QUEUED")

    async def test_restart_latest_pending_or_rejected_first_review_holds(self):
        initial = self.repository.state()
        for status in ("pending", "rejected"):
            with self.subTest(status=status):
                self.repository.value = copy.deepcopy(initial)
                self.recovery_batch()
                self.repository.mutate(lambda state: state["reviews"].append({
                    "id": "review-20261008100000000000-new", "task_id": "unit-task",
                    "asset_id": "unit-composition-01", "status": status, "unit_fixture": True}))
                await self.production.recover()
                await self.production.resume_recovered()
                self.production.launch.assert_not_awaited()
                self.assertNotEqual(self.repository.state()["tasks"][0]["status"], "QUEUED")

    async def test_restart_preserves_pause_cancel_rejection_unknown_and_disabled_policy(self):
        initial = self.repository.state()
        for guard in ("pause", "cancel", "reject", "unknown", "auto_advance"):
            with self.subTest(guard=guard):
                self.repository.value = copy.deepcopy(initial)
                self.recovery_batch(status="PAUSED" if guard == "pause" else "RUNNING",
                    item_status="CANCELLED" if guard == "cancel" else "REJECTED" if guard == "reject" else "WAITING_REVIEW")
                def change(state):
                    if guard == "unknown":
                        state["runs"].append({"id": "unit-paid-unknown", "task_id": "unit-task", "stage": "background",
                            "provider": "BackgroundRPAAdapter", "status": "failed", "error_code": "UNKNOWN_EXTERNAL_REQUEST"})
                    elif guard == "auto_advance":
                        state["settings"]["production_policy"]["auto_advance"] = False
                self.repository.mutate(change)
                await self.production.recover()
                await self.production.resume_recovered()
                self.production.launch.assert_not_awaited()
                self.assertNotEqual(self.repository.state()["tasks"][0]["status"], "QUEUED")
                self.assertEqual(self.repository.state()["batches"][0]["status"], "PAUSED")

    async def test_restart_approved_first_does_not_requeue_while_original_pid_is_alive(self):
        self.recovery_batch()
        self.repository.mutate(lambda state: state["runs"].append({"id": "unit-original-run", "task_id": "unit-task",
            "stage": "compose", "provider": "LegacyComposeAdapter", "status": "running", "pid": 123456,
            "unit_fixture": True}))
        await self.production.recover()
        with patch("app.integrations.production_service.probe_process") as probe:
            await self.production.resume_recovered()
        probe.assert_called_once_with(123456)
        self.production.launch.assert_not_awaited()
        self.assertEqual(self.production_context()["waiting"]["reason"], "WORKER_STILL_RUNNING")

    async def test_restart_probes_original_active_pid_before_later_successful_preview(self):
        self.recovery_batch()
        self.repository.mutate(lambda state: state["runs"].extend([
            {"id": "unit-original-run", "task_id": "unit-task", "stage": "compose",
                "provider": "LegacyComposeAdapter", "status": "running", "pid": 123456, "unit_fixture": True},
            {"id": "unit-later-preview", "task_id": "unit-task", "stage": "person",
                "provider": "FactoryMatteRepair", "status": "success", "unit_fixture": True}]))
        await self.production.recover()
        with patch("app.integrations.production_service.probe_process") as probe:
            await self.production.resume_recovered()
        probe.assert_called_once_with(123456)
        self.production.launch.assert_not_awaited()
        self.assertEqual(self.production_context()["waiting"]["reason"], "WORKER_STILL_RUNNING")


if __name__ == "__main__":
    unittest.main()
