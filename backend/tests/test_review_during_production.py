"""Isolated concurrent review contracts; fixture files are not production evidence."""

import copy
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from app.integrations.asset_registry import file_hash
from app.integrations.review_service import RealReviewService
import test_deferred_production_continue as deferred_fixtures


class ReviewDuringProductionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        fixture = deferred_fixtures.DeferredProductionContinueTests()
        await fixture.asyncSetUp()
        self.addCleanup(fixture.temporary.cleanup)
        self.repository, self.production, self.person = fixture.repository, fixture.production, fixture.person
        self.review = RealReviewService(self.production)
        self.production.collector = SimpleNamespace(active={}, can_resume_download=lambda _: False)
        self.production.source_review = SimpleNamespace(approval_record=lambda _: None)
        path = self.production.settings.storage_root / "unit-compose-02.media"
        path.write_bytes(b"isolated completed composition02 fixture; not production media")
        def prepare(state):
            state["assets"].append({"id": "unit-compose-02", "task_id": "unit-task", "type": "compose", "status": "READY",
                "registry_status": "registered", "storage_path": str(path), "sha256": file_hash(path),
                "legacy_reference": {"prompt_id": "unit-prompt-02", "person_asset_id": self.person["id"],
                    "background_asset_id": "unit-background-01", "processing_run_id": "unit-completed-compose-02", "unit_fixture": True}})
            state["reviews"].append({"id": "unit-review-02", "task_id": "unit-task", "asset_id": "unit-compose-02",
                "status": "pending", "note": "", "unit_fixture": True})
            state["prompts"] = [{"id": f"unit-prompt-{ordinal:02d}", "task_id": "unit-task", "ordinal": ordinal,
                "status": "READY", "fingerprint": f"unit-prompt-fingerprint-{ordinal}", "unit_fixture": True} for ordinal in range(1, 4)]
            task = state["tasks"][0]
            task.update(status="RUNNING", current_stage="compose")
            task["context"]["production"].update(batch_id="unit-batch", automatic=True,
                active_run_id="unit-live-compose-03", active_stage="compose", requested_stage=None,
                results={"compose:unit-prompt-02": {"status": "READY", "run_id": "unit-completed-compose-02", "unit_fixture": True}})
            state["runs"] = [{"id": "unit-completed-compose-02", "task_id": "unit-task", "stage": "compose",
                "provider": "LegacyComposeAdapter", "status": "success", "progress": 100, "pid": 987654320,
                "exit_code": 0, "finished_at": "2026-10-07T00:00:00Z", "unit_fixture": True},
                {"id": "unit-live-compose-03", "task_id": "unit-task", "stage": "compose", "provider": "LegacyComposeAdapter",
                    "status": "running", "progress": 32, "pid": 987654321, "logs": ["isolated machine-owner fixture"], "unit_fixture": True}]
            state["batches"] = [{"id": "unit-batch", "status": "RUNNING", "task_ids": ["unit-task"],
                "active_task_id": "unit-task", "items": [{"task_id": "unit-task", "status": "RUNNING", "stage": "compose"}], "summary": {}}]
            state["quality_assessments"] = []
        self.repository.mutate(prepare)
        self.assertTrue(self.production._first_review_approved("unit-task", self.person))

    async def approve(self):
        with patch.object(self.production, "continue_task", wraps=self.production.continue_task) as continuation:
            result = await self.review.action("approve", "unit-review-02", {"note": "isolated current composition approval"})
        continuation.assert_awaited_once_with("unit-task")
        return result

    def assert_review_and_final_saved(self, before):
        state = self.repository.state()
        self.assertEqual(next(row for row in state["reviews"] if row["id"] == "unit-review-02")["status"], "approved")
        self.assertEqual(next(row for row in state["assets"] if row["id"] == "unit-compose-02")["status"], "approved")
        final = next(row for row in state["assets"] if row["type"] == "final")
        self.assertEqual(final["legacy_reference"]["composition_asset_id"], "unit-compose-02")
        human = next(row for row in state["runs"] if row["id"] == final["legacy_reference"]["processing_run_id"])
        self.assertEqual(human["provider"], "HumanReview")
        self.assertEqual(human["status"], "success")
        self.assertEqual(human["progress"], 100)
        self.assertTrue(human["finished_at"])
        self.assertEqual([row for row in state["runs"] if row["provider"] != "HumanReview"],
            [row for row in before["runs"] if row["provider"] != "HumanReview"])
        self.assertEqual(state["tasks"][0]["context"]["production"]["first_composition"],
            before["tasks"][0]["context"]["production"]["first_composition"])
        self.assertEqual(state["publishing"], before["publishing"])
        self.assertEqual(len(state["tasks"]), len(before["tasks"]))
        self.production.launch.assert_not_awaited()

    async def test_approve_completed_compose_preserves_genuinely_running_or_queued_machine_owner(self):
        original = self.repository.state()
        for status in ("running", "queued"):
            with self.subTest(status=status):
                self.repository.value = copy.deepcopy(original)
                self.production.registry.registered.clear()
                self.repository.mutate(lambda state: state["runs"][-1].update(status=status))
                before = self.repository.state()
                result = await self.approve()
                state = self.repository.state()
                self.assertEqual(state["tasks"][0]["status"], "RUNNING")
                self.assertEqual(state["tasks"][0]["current_stage"], "compose")
                self.assertEqual(state["batches"], before["batches"])
                self.assertFalse(result.get("accepted", False))
                self.assertIn("不重复排队", result["message"])
                self.assert_review_and_final_saved(before)

    async def test_paused_batch_remains_paused_while_current_completed_compose_is_approved(self):
        self.repository.mutate(lambda state: state["batches"][0].update(status="PAUSED"))
        before = self.repository.state()
        result = await self.approve()
        state = self.repository.state()
        self.assertEqual(state["batches"], before["batches"])
        self.assertEqual(state["tasks"][0]["status"], "RUNNING")
        self.assertEqual(state["tasks"][0]["current_stage"], "compose")
        self.assertFalse(result.get("accepted", False))
        self.assert_review_and_final_saved(before)

    async def test_user_cancel_reject_pause_or_skip_is_not_cleared_by_approval(self):
        original = self.repository.state()
        for status in ("CANCELLED", "REJECTED", "PAUSED", "SKIPPED"):
            with self.subTest(status=status):
                self.repository.value = copy.deepcopy(original)
                self.production.registry.registered.clear()
                def hold(state):
                    state["tasks"][0].update(status=status)
                    state["runs"][-1].update(status="success", exit_code=0, finished_at="2026-10-07T00:00:00Z")
                self.repository.mutate(hold)
                before = self.repository.state()
                result = await self.approve()
                state = self.repository.state()
                self.assertEqual(state["tasks"][0]["status"], status)
                self.assertEqual(state["tasks"][0]["current_stage"], "compose")
                self.assertEqual(state["batches"], before["batches"])
                self.assertFalse(result.get("accepted", False))
                self.assert_review_and_final_saved(before)

    async def test_reject_current_compose_still_rejects_task_despite_active_machine_run(self):
        before = self.repository.state()
        with patch.object(self.production, "continue_task", wraps=self.production.continue_task) as continuation:
            await self.review.action("reject", "unit-review-02", {})
        state = self.repository.state()
        self.assertEqual(state["tasks"][0]["status"], "REJECTED")
        self.assertEqual(state["tasks"][0]["current_stage"], "review")
        self.assertEqual(state["tasks"][0]["context"]["production"]["rejected_review"]["asset_id"], "unit-compose-02")
        self.assertEqual(next(row for row in state["reviews"] if row["id"] == "unit-review-02")["status"], "rejected")
        self.assertEqual(state["runs"], before["runs"])
        self.assertEqual(state["batches"], before["batches"])
        self.assertFalse(any(row["type"] == "final" for row in state["assets"]))
        continuation.assert_not_awaited()
        self.production.launch.assert_not_awaited()

    async def test_unknown_paid_request_blocks_real_automatic_continuation(self):
        def change(state):
            state["runs"][-1].update(status="success", exit_code=0, finished_at="2026-10-07T00:00:00Z")
            state["runs"].append({"id": "unit-paid-unknown", "task_id": "unit-task", "stage": "background",
                "provider": "BackgroundRPAAdapter", "status": "failed", "error_code": "UNKNOWN_EXTERNAL_REQUEST", "unit_fixture": True})
        self.repository.mutate(change)
        before = self.repository.state()
        result = await self.approve()
        state = self.repository.state()
        self.assertFalse(result.get("accepted", False))
        self.assertEqual(state["batches"], before["batches"])
        self.assertEqual(state["tasks"][0]["status"], "WAITING_REVIEW")
        self.assertEqual(state["tasks"][0]["context"]["production"]["waiting"]["reason"], "UNKNOWN_EXTERNAL_REQUEST")
        self.assert_review_and_final_saved(before)

    async def test_jobs_entry_alone_with_terminal_run_uses_normal_approval_continuation(self):
        self.production.jobs["unit-batch"] = object()
        self.repository.mutate(lambda state: state["runs"][-1].update(status="success", exit_code=0,
            finished_at="2026-10-07T00:00:00Z"))
        before = self.repository.state()
        result = await self.approve()
        state = self.repository.state()
        self.assertTrue(result["accepted"])
        self.assertEqual(result["batch_id"], "unit-batch")
        self.assertEqual(len(state["batches"]), 1)
        self.assertEqual(state["tasks"][0]["status"], "QUEUED")
        self.assertEqual(state["tasks"][0]["current_stage"], "review")
        self.assertEqual(state["batches"][0]["items"][0]["status"], "QUEUED")
        self.assert_review_and_final_saved(before)

    async def test_unrelated_task_run_cannot_pretend_to_own_current_task(self):
        self.repository.mutate(lambda state: state["runs"][-1].update(task_id="unit-unrelated-task"))
        before = self.repository.state()
        result = await self.approve()
        state = self.repository.state()
        self.assertTrue(result["accepted"])
        self.assertEqual(state["tasks"][0]["status"], "QUEUED")
        self.assertEqual(state["tasks"][0]["current_stage"], "review")
        self.assertEqual(len(state["batches"]), 1)
        self.assert_review_and_final_saved(before)

    async def test_human_review_mock_or_matte_preview_is_not_machine_production_owner(self):
        original = self.repository.state()
        for provider in ("HumanReview", "mock", "FactoryMatteRepair"):
            with self.subTest(provider=provider):
                self.repository.value = copy.deepcopy(original)
                self.production.registry.registered.clear()
                self.repository.mutate(lambda state: state["runs"][-1].update(provider=provider))
                before = self.repository.state()
                result = await self.approve()
                state = self.repository.state()
                self.assertEqual(state["tasks"][0]["status"], "WAITING_REVIEW")
                self.assertEqual(state["tasks"][0]["current_stage"], "review")
                self.assertFalse(result.get("accepted", False))
                self.assertEqual(state["batches"], before["batches"])
                self.assert_review_and_final_saved(before)

    async def test_mismatched_run_stage_or_active_batch_identity_is_not_owner(self):
        original = self.repository.state()
        for guard in ("run_id", "stage", "batch_id", "active_task", "item_status"):
            with self.subTest(guard=guard):
                self.repository.value = copy.deepcopy(original)
                self.production.registry.registered.clear()
                def change(state):
                    production = state["tasks"][0]["context"]["production"]
                    if guard == "run_id":
                        production["active_run_id"] = "unit-other-run"
                    elif guard == "stage":
                        production["active_stage"] = "background"
                    elif guard == "batch_id":
                        production["batch_id"] = "unit-other-batch"
                    elif guard == "active_task":
                        state["batches"][0]["active_task_id"] = "unit-other-task"
                    else:
                        state["batches"][0]["items"][0]["status"] = "QUEUED"
                self.repository.mutate(change)
                before = self.repository.state()
                result = await self.approve()
                state = self.repository.state()
                self.assertEqual(state["tasks"][0]["status"], "WAITING_REVIEW")
                self.assertEqual(state["tasks"][0]["current_stage"], "review")
                self.assertFalse(result.get("accepted", False))
                self.assertEqual(state["batches"], before["batches"])
                self.assert_review_and_final_saved(before)


if __name__ == "__main__":
    unittest.main()
