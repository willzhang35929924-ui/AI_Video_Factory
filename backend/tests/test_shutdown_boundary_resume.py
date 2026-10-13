"""Isolated shutdown scheduling contracts, not production media evidence."""

import asyncio
import copy
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app.integrations.asset_registry import file_hash
from app.integrations.production_service import ProductionStopped
import test_deferred_production_continue as deferred_fixtures


class ShutdownBoundaryResumeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        fixture = deferred_fixtures.DeferredProductionContinueTests()
        await fixture.asyncSetUp()
        self.addCleanup(fixture.temporary.cleanup)
        self.repository, self.production, self.person = fixture.repository, fixture.production, fixture.person
        self.production.collector = SimpleNamespace(active={}, can_resume_download=lambda _: False,
            queue=AsyncMock(), run=AsyncMock(), expand_search=AsyncMock(), context=lambda _: {"unit_fixture": True})
        self.production.source_review = SimpleNamespace(approval_record=lambda _: None)
        self.production.settings.factory_comfy_compose_workflow.write_text('{"unit":"compose-workflow"}')
        root = self.production.settings.storage_root
        background_path, compose_path = root / "unit-background-02.media", root / "unit-composition-02.media"
        background_path.write_bytes(b"isolated shutdown background fixture; not production media")
        compose_path.write_bytes(b"isolated shutdown composition fixture; not production media")
        def prepare(state):
            task = state["tasks"][0]
            task.update(status="CANCELLED", current_stage="compose")
            production = task["context"]["production"]
            production.update(batch_id="unit-batch", automatic=True, previous_status="RUNNING", retry=False,
                results={"compose:unit-prompt-02": {"status": "READY", "run_id": "unit-compose-run-02", "unit_fixture": True}})
            for asset in state["assets"]:
                if asset["type"] == "background":
                    asset["legacy_reference"]["factory_fingerprint"] = self.production._fingerprint(task, "background", "unit-prompt-01")
                elif asset["type"] == "compose":
                    first_background = next(row for row in state["assets"] if row["id"] == "unit-background-01")
                    asset["legacy_reference"].update(person_asset_id=self.person["id"], background_asset_id=first_background["id"],
                        factory_fingerprint=self.production._fingerprint(task, "compose", "unit-prompt-01", self.person, first_background))
            background = {"id": "unit-background-02", "task_id": "unit-task", "type": "background", "status": "READY",
                "registry_status": "registered", "storage_path": str(background_path), "sha256": file_hash(background_path),
                "legacy_reference": {"prompt_id": "unit-prompt-02", "unit_fixture": True,
                    "factory_fingerprint": self.production._fingerprint(task, "background", "unit-prompt-02")}}
            composition = {"id": "unit-composition-02", "task_id": "unit-task", "type": "compose", "status": "READY",
                "registry_status": "registered", "storage_path": str(compose_path), "sha256": file_hash(compose_path),
                "legacy_reference": {"prompt_id": "unit-prompt-02", "processing_run_id": "unit-compose-run-02",
                    "person_asset_id": self.person["id"], "background_asset_id": background["id"], "unit_fixture": True,
                    "factory_fingerprint": self.production._fingerprint(task, "compose", "unit-prompt-02", self.person, background)}}
            state["assets"].extend([background, composition])
            state["runs"] = [{"id": "unit-compose-run-02", "task_id": "unit-task", "stage": "compose",
                "provider": "LegacyComposeAdapter", "status": "success", "pid": 987654321, "exit_code": 0,
                "finished_at": "2026-10-07T00:00:00Z", "progress": 100, "unit_fixture": True}]
            state["batches"] = [{"id": "unit-batch", "status": "PAUSED", "task_ids": ["unit-task"],
                "items": [{"task_id": "unit-task", "status": "CANCELLED", "stage": "compose"}],
                "active_task_id": None, "summary": {}}]
            state["audits"] = [{"id": "unit-pause", "action": "production.pause", "entity_id": "unit-batch",
                "message": "isolated pause audit", "created_at": "2026-10-07T00:00:00Z"}]
            state["quality_assessments"] = []
        self.repository.mutate(prepare)
        self.assertTrue(self.boundary())

    def boundary(self):
        state = self.repository.state()
        return self.production._completed_shutdown_boundary(state, state["batches"][0], state["tasks"][0])

    async def assert_resume_blocked(self):
        before = self.repository.state()
        self.assertFalse(self.boundary())
        with self.assertRaises(HTTPException) as error:
            await self.production.control("resume", batch_id="unit-batch")
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(self.repository.state(), before)
        self.production.launch.assert_not_awaited()
        self.production.collector.queue.assert_not_awaited()

    async def test_explicit_resume_requeues_original_completed_boundary_without_rewriting_evidence(self):
        before = self.repository.state()
        result = await self.production.control("resume", batch_id="unit-batch")
        state = self.repository.state()
        self.assertEqual(result["batch_id"], "unit-batch")
        self.assertEqual(result["affected_task_ids"], ["unit-task"])
        self.assertEqual(len(state["batches"]), 1)
        self.assertEqual(len(state["tasks"]), 1)
        self.assertEqual(state["batches"][0]["status"], "RUNNING")
        self.assertEqual(state["batches"][0]["items"][0]["status"], "QUEUED")
        self.assertEqual(state["tasks"][0]["status"], "QUEUED")
        self.assertIsNone(state["tasks"][0]["context"]["production"]["requested_stage"])
        self.assertEqual(state["runs"], before["runs"])
        self.assertEqual(state["assets"], before["assets"])
        self.assertEqual(state["reviews"], before["reviews"])
        self.assertEqual(state["stages"], before["stages"])
        self.assertEqual(state["tasks"][0]["context"]["production"]["first_composition"],
            before["tasks"][0]["context"]["production"]["first_composition"])
        self.production.launch.assert_awaited_once_with("unit-batch")
        self.production.collector.queue.assert_not_awaited()
        self.production.collector.run.assert_not_awaited()

    async def test_cancel_audit_or_missing_pause_audit_cannot_recover_legacy_cancelled_task(self):
        original = self.repository.state()
        for guard in ("cancel", "missing_pause", "shutdown_pause_only"):
            with self.subTest(guard=guard):
                self.repository.value = copy.deepcopy(original)
                def change(state):
                    if guard == "cancel":
                        state["audits"].append({"action": "production.cancel", "entity_id": "unit-batch"})
                    elif guard == "missing_pause":
                        state["audits"] = []
                    else:
                        state["audits"][0]["action"] = "production.shutdown_pause"
                self.repository.mutate(change)
                await self.assert_resume_blocked()

    async def test_large_user_cancelled_batch_cannot_adopt_completed_boundary(self):
        def change(state):
            batch = state["batches"][0]
            batch["status"] = "CANCELLED"
            for ordinal in range(2, 198):
                identifier = f"unit-cancelled-task-{ordinal:03d}"
                task = copy.deepcopy(state["tasks"][0])
                task["id"] = identifier
                state["tasks"].append(task)
                batch["task_ids"].append(identifier)
                batch["items"].append({"task_id": identifier, "status": "CANCELLED", "stage": "collector"})
            state["audits"].append({"action": "production.cancel", "entity_id": "unit-batch"})
        self.repository.mutate(change)
        self.assertEqual(len(self.repository.state()["batches"][0]["task_ids"]), 197)
        await self.assert_resume_blocked()

    async def test_unknown_paid_request_or_active_run_holds_completed_boundary(self):
        original = self.repository.state()
        for status, code in (("running", None), ("queued", None), ("failed", "UNKNOWN_EXTERNAL_REQUEST")):
            with self.subTest(status=status, code=code):
                self.repository.value = copy.deepcopy(original)
                self.repository.mutate(lambda state: state["runs"].insert(0, {"id": "unit-other-run", "task_id": "unit-task",
                    "stage": "background", "provider": "BackgroundRPAAdapter", "status": status, "error_code": code}))
                await self.assert_resume_blocked()

    async def test_current_person_rejection_or_missing_approval_blocks_resume(self):
        original = self.repository.state()
        for guard in ("review_rejected", "review_pending", "review_missing", "rejected_review", "task_rejected"):
            with self.subTest(guard=guard):
                self.repository.value = copy.deepcopy(original)
                def change(state):
                    if guard == "review_missing":
                        state["reviews"] = [row for row in state["reviews"] if row["asset_id"] != self.person["id"]]
                    elif guard == "rejected_review":
                        state["tasks"][0]["context"]["production"]["rejected_review"] = {"asset_id": self.person["id"]}
                    elif guard == "task_rejected":
                        state["tasks"][0]["status"] = "REJECTED"
                    else:
                        state["reviews"][0]["status"] = "rejected" if guard == "review_rejected" else "pending"
                self.repository.mutate(change)
                await self.assert_resume_blocked()

    async def test_first_composition_approval_missing_pending_or_rejected_blocks_resume(self):
        original = self.repository.state()
        for status in (None, "pending", "rejected"):
            with self.subTest(status=status):
                self.repository.value = copy.deepcopy(original)
                def change(state):
                    if status is None:
                        state["reviews"] = [row for row in state["reviews"] if row["id"] != "unit-first-review"]
                    else:
                        next(row for row in state["reviews"] if row["id"] == "unit-first-review")["status"] = status
                self.repository.mutate(change)
                await self.assert_resume_blocked()

    async def test_changed_current_person_or_composition_parent_blocks_resume(self):
        original = self.repository.state()
        for guard in ("selected_person", "composition_person", "composition_background", "composition_fingerprint"):
            with self.subTest(guard=guard):
                self.repository.value = copy.deepcopy(original)
                def change(state):
                    if guard == "selected_person":
                        state["tasks"][0]["context"]["production"]["current_person_asset_id"] = "unit-new-person"
                    else:
                        ref = next(row for row in state["assets"] if row["id"] == "unit-composition-02")["legacy_reference"]
                        key = {"composition_person": "person_asset_id", "composition_background": "background_asset_id",
                            "composition_fingerprint": "factory_fingerprint"}[guard]
                        ref[key] = "unit-changed-identity"
                self.repository.mutate(change)
                await self.assert_resume_blocked()

    async def test_tampered_source_rgb_alpha_manifest_background_or_composition_blocks_resume(self):
        paths = [Path(self.person["storage_path"]), Path(self.person["legacy_reference"]["alpha_path"]),
            Path(self.person["legacy_reference"]["manifest_path"])]
        paths.extend(Path(row["storage_path"]) for row in self.repository.state()["assets"]
            if row["type"] in {"source", "background", "compose"})
        for path in paths:
            with self.subTest(path=path.name):
                previous = path.read_bytes()
                path.write_bytes(b"changed isolated shutdown fixture")
                try:
                    await self.assert_resume_blocked()
                finally:
                    path.write_bytes(previous)

    async def test_missing_process_proof_wrong_provider_or_result_run_blocks_resume(self):
        original = self.repository.state()
        for guard in ("pid", "exit_code", "finished_at", "provider", "stage", "result_run", "result_status", "asset_run"):
            with self.subTest(guard=guard):
                self.repository.value = copy.deepcopy(original)
                def change(state):
                    if guard == "result_run":
                        state["tasks"][0]["context"]["production"]["results"]["compose:unit-prompt-02"]["run_id"] = "unit-wrong-run"
                    elif guard == "result_status":
                        state["tasks"][0]["context"]["production"]["results"]["compose:unit-prompt-02"]["status"] = "FAILED"
                    elif guard == "asset_run":
                        next(row for row in state["assets"] if row["id"] == "unit-composition-02")["legacy_reference"]["processing_run_id"] = "unit-wrong-run"
                    else:
                        state["runs"][0][guard] = {"pid": 0, "exit_code": 7, "finished_at": None,
                            "provider": "mock", "stage": "quality"}[guard]
                self.repository.mutate(change)
                await self.assert_resume_blocked()

    async def test_unregistered_technical_foreign_or_nonautomatic_evidence_cannot_resume(self):
        original = self.repository.state()
        for guard in ("unregistered", "technical", "foreign_task", "batch_identity", "nonautomatic", "mock_task"):
            with self.subTest(guard=guard):
                self.repository.value = copy.deepcopy(original)
                def change(state):
                    composition = next(row for row in state["assets"] if row["id"] == "unit-composition-02")
                    if guard == "unregistered":
                        composition["registry_status"] = "unregistered"
                    elif guard == "technical":
                        composition["legacy_reference"]["execution_scope"] = "TECHNICAL_VALIDATION"
                    elif guard == "foreign_task":
                        composition["task_id"] = "unit-other-task"
                    elif guard == "mock_task":
                        state["tasks"][0]["execution_mode"] = "mock"
                    else:
                        state["tasks"][0]["context"]["production"]["batch_id" if guard == "batch_identity" else "automatic"] = (
                            "unit-other-batch" if guard == "batch_identity" else False)
                self.repository.mutate(change)
                await self.assert_resume_blocked()

    async def stop_during_running_task(self, *, task_status="RUNNING", batch_status="PAUSED", extra_run=None):
        def prepare(state):
            state["batches"][0].update(status="QUEUED")
            state["batches"][0]["items"][0]["status"] = "QUEUED"
            state["tasks"][0]["status"] = "QUEUED"
            if extra_run:
                state["runs"].append(extra_run)
        self.repository.mutate(prepare)
        self.production.cancels["unit-batch"] = threading.Event()
        async def stop(batch_id, task_id):
            self.repository.mutate(lambda state: state["tasks"][0].update(status=task_status))
            self.production._batch_update(batch_id, status=batch_status)
            self.production.closing = True
            raise ProductionStopped()
        with patch.object(self.production, "_task", side_effect=stop):
            await self.production.run("unit-batch")

    async def test_future_shutdown_at_completed_stage_queues_task_and_preserves_evidence(self):
        def queued_sibling(state):
            sibling = copy.deepcopy(state["tasks"][0])
            sibling.update(id="unit-next-task", status="QUEUED", current_stage="collector")
            state["tasks"].append(sibling)
            state["batches"][0]["task_ids"].append(sibling["id"])
            state["batches"][0]["items"].append({"task_id": sibling["id"], "status": "QUEUED", "stage": "collector"})
        self.repository.mutate(queued_sibling)
        before = self.repository.state()
        await self.stop_during_running_task()
        state = self.repository.state()
        self.assertEqual(state["batches"][0]["status"], "PAUSED")
        self.assertIsNone(state["batches"][0]["active_task_id"])
        self.assertEqual(state["batches"][0]["items"][0]["status"], "QUEUED")
        self.assertEqual(state["tasks"][0]["status"], "QUEUED")
        self.assertEqual(state["runs"], before["runs"])
        self.assertEqual(state["assets"], before["assets"])
        self.assertEqual(state["reviews"], before["reviews"])
        self.assertEqual(state["stages"], before["stages"])
        self.assertEqual(state["tasks"][1], before["tasks"][1])
        self.assertEqual(state["batches"][0]["items"][1], before["batches"][0]["items"][1])
        self.assertTrue(any(row["action"] == "production.shutdown_pause" and row["entity_id"] == "unit-batch" for row in state["audits"]))
        self.assertNotIn("unit-batch", self.production.cancels)
        self.production.closing = False
        await self.production.recover()
        await self.production.resume_recovered()
        self.assertEqual(self.repository.state()["batches"][0]["status"], "PAUSED")
        self.production.launch.assert_not_awaited()
        await self.production.control("resume", batch_id="unit-batch")
        self.production.launch.assert_awaited_once_with("unit-batch")

    async def test_closing_preserves_user_cancelled_batch_and_task(self):
        await self.stop_during_running_task(batch_status="CANCELLED")
        state = self.repository.state()
        self.assertEqual(state["batches"][0]["status"], "CANCELLED")
        self.assertEqual(state["batches"][0]["items"][0]["status"], "CANCELLED")
        self.assertEqual(state["tasks"][0]["status"], "CANCELLED")
        self.assertFalse(any(row["action"] == "production.shutdown_pause" for row in state["audits"]))
        self.production.launch.assert_not_awaited()

    async def test_closing_preserves_review_rejected_failed_or_cancelled_task_state(self):
        original = self.repository.state()
        for status in ("WAITING_REVIEW", "REJECTED", "FAILED", "CANCELLED"):
            with self.subTest(status=status):
                self.repository.value = copy.deepcopy(original)
                self.production.closing = False
                await self.stop_during_running_task(task_status=status)
                state = self.repository.state()
                self.assertEqual(state["batches"][0]["status"], "PAUSED")
                self.assertEqual(state["batches"][0]["items"][0]["status"], status)
                self.assertEqual(state["tasks"][0]["status"], status)
                self.production.launch.assert_not_awaited()

    async def test_closing_with_active_or_unknown_run_holds_running_task_for_review(self):
        original = self.repository.state()
        for status, code in (("running", None), ("queued", None), ("failed", "UNKNOWN_EXTERNAL_REQUEST")):
            with self.subTest(status=status, code=code):
                self.repository.value = copy.deepcopy(original)
                self.production.closing = False
                run = {"id": "unit-unresolved", "task_id": "unit-task", "provider": "LegacyComposeAdapter",
                    "stage": "compose", "status": status, "error_code": code}
                await self.stop_during_running_task(extra_run=run)
                state = self.repository.state()
                self.assertEqual(state["batches"][0]["status"], "PAUSED")
                self.assertEqual(state["batches"][0]["items"][0]["status"], "WAITING_REVIEW")
                self.assertEqual(state["tasks"][0]["status"], "WAITING_REVIEW")
                self.assertEqual(state["runs"][-1], run)
                self.production.launch.assert_not_awaited()

    async def test_async_cancel_during_close_uses_same_safe_stage_boundary(self):
        self.repository.mutate(lambda state: state["batches"][0].update(status="RUNNING"))
        self.production.cancels["unit-batch"] = threading.Event()
        self.repository.mutate(lambda state: state["batches"][0]["items"][0].update(status="RUNNING"))
        self.repository.mutate(lambda state: state["tasks"][0].update(status="RUNNING"))
        self.production.closing = True
        with patch.object(self.production, "_checkpoint", side_effect=asyncio.CancelledError):
            await self.production.run("unit-batch")
        state = self.repository.state()
        self.assertEqual(state["batches"][0]["status"], "PAUSED")
        self.assertEqual(state["batches"][0]["items"][0]["status"], "QUEUED")
        self.assertEqual(state["tasks"][0]["status"], "QUEUED")


if __name__ == "__main__":
    unittest.main()
