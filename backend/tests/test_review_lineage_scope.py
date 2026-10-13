"""Isolated review version contracts, not production media or live approval evidence."""

import copy
import json
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app.integrations.asset_registry import file_hash
from app.integrations.review_service import RealReviewService
from test_real_review import ReviewFixture
import test_repaired_person_selection as repaired_fixtures


class ReviewLineageScopeTests(ReviewFixture, unittest.IsolatedAsyncioTestCase):
    async def test_startup_cancelled_rejections_share_one_snapshot(self):
        def cancelled(state):
            template = state["tasks"][0]
            state["tasks"] = []
            for index in range(150):
                task = {**copy.deepcopy(template), "id": f"unit-cancelled-{index}", "status": "REJECTED"}
                task["context"]["production"].update(automatic=True, batch_id="unit-cancelled-batch")
                state["tasks"].append(task)
            state["batches"] = [{"id": "unit-cancelled-batch", "status": "CANCELLED",
                "task_ids": [task["id"] for task in state["tasks"]], "items": [], "summary": {}}]
        self.repository.mutate(cancelled)
        with patch.object(self.repository, "state", wraps=self.repository.state) as reads:
            self.assertEqual(await self.review.reconcile_historical_rejections(), [])
        self.assertLessEqual(reads.call_count, 3)
        self.production.continue_task.assert_not_awaited()

    async def test_startup_does_not_reread_full_state_for_unrelated_tasks(self):
        def unrelated(state):
            state["tasks"] = [{**copy.deepcopy(state["tasks"][0]), "id": f"unit-unrelated-{index}", "status": "WAITING_REVIEW"} for index in range(300)]
        self.repository.mutate(unrelated)
        with patch.object(self.repository, "state", wraps=self.repository.state) as reads:
            self.assertEqual(await self.review.reconcile_historical_rejections(), [])
        self.assertLessEqual(reads.call_count, 3)
        self.production.continue_task.assert_not_awaited()

    async def asyncSetUp(self):
        self.prepare("person")
        self.production.start = AsyncMock(return_value={"accepted": True, "unit_fixture": True})
        self.production.continue_task = AsyncMock()
        self.production.event = AsyncMock()
        def current(state):
            person = state["assets"][0]
            person.update(id="unit-person-v4", status="approved", sha256=file_hash(Path(person["storage_path"])))
            person["legacy_reference"].update(version=4, unit_fixture=True)
            state["reviews"][0].update(id="unit-current-person-review", asset_id=person["id"], status="approved")
            state["settings"]["production_policy"] = {"auto_advance": True}
            task = state["tasks"][0]
            task.update(status="RUNNING", current_stage="compose")
            task["context"]["production"] = {"current_person_asset_id": person["id"], "review_approved": True,
                "automatic": True, "batch_id": "unit-batch", "requested_stage": None,
                "compose_generation": 4, "cutout_generation": 4, "policy_snapshot": {"auto_advance": True},
                "first_composition": {"composition_asset_id": "unit-compose-v4", "person_asset_id": person["id"],
                    "human_review": "APPROVED", "status": "PASS", "fingerprint": "unit-first-v4", "unit_fixture": True}}
            state["batches"] = [{"id": "unit-batch", "status": "RUNNING", "task_ids": [task["id"]],
                "active_task_id": task["id"], "items": [{"task_id": task["id"], "status": "RUNNING", "stage": "compose"}]}]
            state["runs"] = [{"id": "unit-live-compose", "task_id": task["id"], "stage": "compose",
                "provider": "LegacyComposeAdapter", "status": "running", "pid": 424242, "unit_fixture": True}]
        self.repository.mutate(current)
        self.add_asset("person", "unit-person-v2")
        self.add_asset("compose", "unit-compose-v4", person_id="unit-person-v4")
        self.add_asset("final", "unit-final-v4", composition_id="unit-compose-v4")

    def add_asset(self, kind, identifier, *, person_id=None, composition_id=None):
        path = self.production.settings.storage_root / (identifier + ".media")
        path.write_bytes(("isolated version scope fixture " + identifier).encode())
        reference = {"unit_fixture": True, "prompt_id": "unit-prompt"}
        if person_id:
            reference["person_asset_id"] = person_id
        if composition_id:
            reference["composition_asset_id"] = composition_id
        asset = {"id": identifier, "task_id": "unit-task", "type": kind, "status": "READY",
            "registry_status": "registered", "storage_path": str(path), "sha256": file_hash(path),
            "legacy_reference": reference}
        self.repository.mutate(lambda state: state["assets"].append(asset))
        return asset

    def historical_review(self, kind):
        if kind == "person":
            asset = self.add_asset("person", "unit-person-v3")
            self.repository.mutate(lambda state: state["assets"][-1]["legacy_reference"].update(
                version=3, parent_person_asset_id="unit-person-v2"))
        elif kind == "compose":
            asset = self.add_asset("compose", "unit-compose-v2", person_id="unit-person-v2")
        else:
            self.add_asset("compose", "unit-compose-v2", person_id="unit-person-v2")
            asset = self.add_asset("final", "unit-final-v2", composition_id="unit-compose-v2")
        review_id = "unit-historical-" + kind + "-review"
        self.repository.mutate(lambda state: state["reviews"].append({"id": review_id,
            "task_id": "unit-task", "asset_id": asset["id"], "status": "pending", "note": "", "unit_fixture": True}))
        return asset["id"], review_id

    def assert_current_unchanged(self, before, historical_id):
        after = self.repository.state()
        for collection in ("tasks", "stages", "runs", "batches", "errors", "publishing"):
            self.assertEqual(after[collection], before[collection], collection)
        self.assertEqual([row for row in after["assets"] if row["id"] != historical_id],
            [row for row in before["assets"] if row["id"] != historical_id])
        self.assertFalse(self.production.registry.registered)
        self.production.start.assert_not_awaited()
        self.production.continue_task.assert_not_awaited()

    async def test_historical_person_compose_and_indirect_final_reject_are_version_scoped(self):
        initial = self.repository.state()
        for kind in ("person", "compose", "final"):
            with self.subTest(kind=kind):
                self.repository.value = copy.deepcopy(initial)
                asset_id, review_id = self.historical_review(kind)
                before = self.repository.state()
                outcome = await self.review.action("reject", review_id, {"note": "old isolated version rejected"})
                self.assertEqual(outcome["scope"], "HISTORICAL_VERSION")
                self.assert_current_unchanged(before, asset_id)
                after = self.repository.state()
                review = next(row for row in after["reviews"] if row["id"] == review_id)
                self.assertEqual(review["status"], "rejected")
                self.assertEqual(review["note"], "old isolated version rejected")
                self.assertEqual(next(row for row in after["assets"] if row["id"] == asset_id)["status"], "rejected")
                self.assertEqual(len(after["audits"]), len(before["audits"]) + 1)

    async def test_historical_fix_keeps_current_queue_and_asset_status(self):
        initial = self.repository.state()
        for kind in ("person", "compose", "final"):
            with self.subTest(kind=kind):
                self.repository.value = copy.deepcopy(initial)
                asset_id, review_id = self.historical_review(kind)
                before = self.repository.state()
                outcome = await self.review.action("fix", review_id, {"note": "isolated historical repair note"})
                self.assertEqual(outcome["scope"], "HISTORICAL_VERSION")
                self.assert_current_unchanged(before, asset_id)
                after = self.repository.state()
                self.assertEqual(after["assets"], before["assets"])
                self.assertEqual(next(row for row in after["reviews"] if row["id"] == review_id)["status"], "manual")

    async def test_historical_retry_or_reprocess_requires_explicit_restore(self):
        initial = self.repository.state()
        for kind in ("person", "compose", "final"):
            for action in ("retry", "reprocess"):
                with self.subTest(kind=kind, action=action):
                    self.repository.value = copy.deepcopy(initial)
                    _, review_id = self.historical_review(kind)
                    before = self.repository.state()
                    with self.assertRaises(HTTPException) as failure:
                        await self.review.action(action, review_id, {})
                    self.assertEqual(failure.exception.status_code, 409)
                    self.assertIn("HISTORICAL_VERSION_RESTORE_REQUIRED", str(failure.exception.detail))
                    self.assertEqual(self.repository.state(), before)
                    self.production.start.assert_not_awaited()

    async def test_historical_approve_still_cannot_adopt_old_version(self):
        initial = self.repository.state()
        for kind in ("person", "compose", "final"):
            with self.subTest(kind=kind):
                self.repository.value = copy.deepcopy(initial)
                _, review_id = self.historical_review(kind)
                before = self.repository.state()
                with self.assertRaises(HTTPException) as failure:
                    await self.review.action("approve", review_id, {})
                self.assertEqual(failure.exception.status_code, 409)
                self.assertEqual(self.repository.state(), before)
                self.production.continue_task.assert_not_awaited()

    async def test_current_person_compose_and_indirect_final_rejection_still_blocks_task(self):
        initial = self.repository.state()
        for kind, asset_id in (("person", "unit-person-v4"), ("compose", "unit-compose-v4"), ("final", "unit-final-v4")):
            with self.subTest(kind=kind):
                self.repository.value = copy.deepcopy(initial)
                review_id = "unit-current-" + kind + "-pending"
                self.repository.mutate(lambda state: state["reviews"].append({"id": review_id,
                    "task_id": "unit-task", "asset_id": asset_id, "status": "pending", "unit_fixture": True}))
                outcome = await self.review.action("reject", review_id, {})
                after = self.repository.state()
                task = after["tasks"][0]
                self.assertNotEqual(outcome.get("scope"), "HISTORICAL_VERSION")
                self.assertEqual(task["status"], "REJECTED")
                self.assertFalse(task["context"]["production"]["review_approved"])
                self.assertEqual(task["context"]["production"]["rejected_review"]["asset_id"], asset_id)
                self.assertEqual(next(row for row in after["stages"] if row["name"] == "review")["status"], "failed")

    async def test_unregistered_historical_asset_cannot_receive_human_decision(self):
        asset_id, review_id = self.historical_review("person")
        self.repository.mutate(lambda state: state["assets"][-1].update(registry_status="unregistered"))
        before = self.repository.state()
        for action in ("reject", "fix"):
            with self.subTest(action=action):
                with self.assertRaises(HTTPException) as failure:
                    await self.review.action(action, review_id, {})
                self.assertEqual(failure.exception.status_code, 409)
                self.assertEqual(self.repository.state(), before)
                self.production.start.assert_not_awaited()

    async def test_technical_review_keeps_business_state_and_no_production_retry(self):
        asset_id, review_id = self.historical_review("person")
        self.repository.mutate(lambda state: state["assets"][-1]["legacy_reference"].update(execution_scope="TECHNICAL_VALIDATION"))
        before = self.repository.state()
        await self.review.action("reject", review_id, {})
        self.assert_current_unchanged(before, asset_id)
        self.assertEqual(self.repository.state()["assets"], before["assets"])
        self.assertEqual(self.repository.state()["audits"][-1]["action"], "technical.review.reject")
        with self.assertRaises(HTTPException) as failure:
            await self.review.action("retry", review_id, {})
        self.assertEqual(failure.exception.status_code, 409)
        self.production.start.assert_not_awaited()


class HistoricalRejectionReconciliationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        fixture = repaired_fixtures.RepairedPersonSelectionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.approve_parent()
        self.parent = fixture.child(suffix="2")
        self.old = fixture.child(self.parent["id"], "3")
        self.current = fixture.child(self.parent["id"], "4")
        self.repository, self.production = fixture.repository, fixture.production
        self.production.jobs = {}
        self.production.cancels = {}
        self.production.closing = False
        self.production.recovered_batches = []
        self.production.recovered_run_ids = set()
        self.production.recovered_item_ids = set()
        self.production.launch = AsyncMock()
        self.review = RealReviewService(self.production)
        def save(state):
            task = state["tasks"][0]
            old_review = next(row for row in state["reviews"] if row["asset_id"] == self.old["id"])
            current_review = next(row for row in state["reviews"] if row["asset_id"] == self.current["id"])
            self.old_review_id, self.current_review_id = old_review["id"], current_review["id"]
            old_review.update(status="rejected", note="isolated historical rejection")
            current_review.update(status="approved", created_at="2026-10-08T05:00:00+00:00")
            next(row for row in state["assets"] if row["id"] == self.old["id"]).update(status="rejected")
            next(row for row in state["assets"] if row["id"] == self.current["id"]).update(status="approved")
            old_repair = next(row for row in task["context"]["matte_repairs"] if row["asset_id"] == self.old["id"])
            old_repair.update(status="UNDONE", undone_at="2026-10-08T03:00:00+00:00")
            next(row for row in task["context"]["matte_repairs"] if row["asset_id"] == self.current["id"]).update(
                applied_at="2026-10-08T04:00:00+00:00")
            task.update(status="REJECTED", current_stage="review")
            task["context"]["production"].update(batch_id="unit-old-batch", automatic=True, review_approved=False,
                rejected_review={"asset_id": self.old["id"], "review_id": old_review["id"],
                    "decided_at": "2026-10-08T06:00:00+00:00"},
                first_composition={"fingerprint": "unit-existing-first", "human_review": "PENDING", "unit_fixture": True})
            state["settings"]["production_policy"] = {"auto_advance": True, "auto_resume": True}
            state["batches"] = [{"id": "unit-old-batch", "status": "COMPLETED_WITH_ERRORS", "task_ids": [task["id"]],
                "items": [{"task_id": task["id"], "status": "REJECTED", "stage": "review"}], "summary": {}, "active_task_id": None}]
            state["audits"].extend([
                {"id": "unit-old-undo-audit", "task_id": task["id"], "action": "repair.undo", "entity_id": "unit-repair-3"},
                {"id": "unit-old-reject-audit", "task_id": task["id"], "action": "review.reject", "entity_id": old_review["id"]},
                {"id": "unit-current-approve-audit", "task_id": task["id"], "action": "review.approve", "entity_id": current_review["id"]}])
        self.repository.mutate(save)
        self.assertEqual(self.production._asset("unit-task", "person")["id"], self.current["id"])

    async def assert_not_reconciled(self):
        self.production.launch.reset_mock()
        before = self.repository.state()
        outcome = await self.review.reconcile_historical_rejections()
        self.assertEqual(outcome, [])
        self.assertEqual(self.repository.state(), before)
        self.production.launch.assert_not_awaited()

    async def test_reconciliation_refreshes_snapshot_after_continuation(self):
        def sibling(state):
            task = copy.deepcopy(state["tasks"][0])
            task["id"] = "unit-next-task"
            state["tasks"].append(task)
            state["batches"][0]["task_ids"].append(task["id"])
        self.repository.mutate(sibling)
        continuation = self.production.continue_task
        async def cancel_origin(task_id, **kwargs):
            result = await continuation(task_id, **kwargs)
            self.repository.mutate(lambda state: state["batches"][0].update(status="CANCELLED"))
            return result
        with patch.object(self.production, "continue_task", side_effect=cancel_origin), patch.object(
                self.review, "historical_rejection_evidence", wraps=self.review.historical_rejection_evidence) as evidence:
            outcome = await self.review.reconcile_historical_rejections()
        self.assertEqual(len(outcome), 1)
        self.assertEqual(evidence.call_count, 1)
        self.assertEqual(self.repository.state()["tasks"][1]["status"], "REJECTED")

    async def test_verified_undone_sibling_rejection_reconciles_once_through_quality_queue(self):
        before = self.repository.state()
        with patch.object(self.production, "continue_task", wraps=self.production.continue_task) as continuation:
            outcome = await self.review.reconcile_historical_rejections()
            repeated = await self.review.reconcile_historical_rejections()
        self.assertEqual(len(outcome), 1)
        self.assertTrue(outcome[0]["recovered"])
        self.assertEqual(repeated, [])
        continuation.assert_awaited_once()
        self.assertEqual(continuation.await_args.args, ("unit-task",))
        self.assertEqual(continuation.await_args.kwargs["stage"], "quality")
        after = self.repository.state()
        production = after["tasks"][0]["context"]["production"]
        self.assertEqual(after["tasks"][0]["status"], "QUEUED")
        self.assertEqual(production["requested_stage"], "quality")
        self.assertTrue(production["review_approved"])
        self.assertNotIn("rejected_review", production)
        self.assertEqual(production["rejected_review_history"][-1]["asset_id"], self.old["id"])
        self.assertEqual(production["current_person_asset_id"], self.current["id"])
        self.assertEqual(production["first_composition"], before["tasks"][0]["context"]["production"]["first_composition"])
        for collection in ("assets", "reviews", "runs"):
            self.assertEqual(after[collection], before[collection], collection)
        reconciled = [row for row in after["audits"] if row["action"] == "production.historical_rejection_reconciled"]
        self.assertEqual(len(reconciled), 1)
        self.assertIn(before["audits"][-2], after["audits"])
        self.production.launch.assert_awaited_once_with(after["batches"][-1]["id"])

    async def test_missing_reject_undo_apply_or_current_approval_audit_cannot_reconcile(self):
        initial = self.repository.state()
        for action, entity in (("review.reject", self.old_review_id), ("repair.undo", "unit-repair-3"),
                ("repair.applied", "unit-repair-3"), ("repair.applied", "unit-repair-4"),
                ("review.approve", self.current_review_id)):
            with self.subTest(action=action, entity=entity):
                self.repository.value = copy.deepcopy(initial)
                self.repository.mutate(lambda state: state.update(audits=[row for row in state["audits"]
                    if not (row["action"] == action and row["entity_id"] == entity)]))
                await self.assert_not_reconciled()

    async def test_missing_current_run_or_applied_repair_proof_cannot_reconcile(self):
        initial = self.repository.state()
        for missing in ("run", "pid", "exit", "current_apply", "old_undo", "old_applied_at"):
            with self.subTest(missing=missing):
                self.repository.value = copy.deepcopy(initial)
                def corrupt(state):
                    run = next(row for row in state["runs"] if row["id"] == "unit-repair-run-4")
                    repairs = state["tasks"][0]["context"]["matte_repairs"]
                    if missing == "run":
                        run["status"] = "failed"
                    elif missing == "pid":
                        run["pid"] = None
                    elif missing == "exit":
                        run["exit_code"] = 9
                    elif missing == "current_apply":
                        next(row for row in repairs if row["id"] == "unit-repair-4")["status"] = "UNDONE"
                    elif missing == "old_undo":
                        next(row for row in repairs if row["id"] == "unit-repair-3")["status"] = "APPLIED"
                    else:
                        next(row for row in repairs if row["id"] == "unit-repair-3").pop("applied_at")
                self.repository.mutate(corrupt)
                await self.assert_not_reconciled()

    async def test_unknown_pause_cancel_and_disabled_policy_hold_reconciliation(self):
        initial = self.repository.state()
        for guard in ("unknown", "pause", "cancel", "auto_advance", "auto_resume"):
            with self.subTest(guard=guard):
                self.repository.value = copy.deepcopy(initial)
                def hold(state):
                    if guard == "unknown":
                        state["runs"].append({"id": "unit-unknown-paid", "task_id": "unit-task", "stage": "background",
                            "provider": "BackgroundRPAAdapter", "status": "failed", "error_code": "UNKNOWN_EXTERNAL_REQUEST"})
                    elif guard in {"pause", "cancel"}:
                        state["batches"][0]["status"] = "PAUSED" if guard == "pause" else "CANCELLED"
                    else:
                        state["settings"]["production_policy"][guard] = False
                self.repository.mutate(hold)
                await self.assert_not_reconciled()

    async def test_reconciliation_cannot_override_current_or_ancestor_rejection(self):
        initial = self.repository.state()
        for identifier in (self.current["id"], self.parent["id"]):
            with self.subTest(identifier=identifier):
                self.repository.value = copy.deepcopy(initial)
                def reject(state):
                    review = next(row for row in state["reviews"] if row["asset_id"] == identifier)
                    review["status"] = "rejected"
                    next(row for row in state["assets"] if row["id"] == identifier)["status"] = "rejected"
                    task = state["tasks"][0]
                    task["context"]["production"]["rejected_review"] = {"review_id": review["id"], "asset_id": identifier}
                    repair = next(row for row in task["context"]["matte_repairs"] if row["asset_id"] == identifier)
                    repair.update(status="UNDONE", undone_at="2026-10-08T07:00:00+00:00")
                    state["audits"].extend([
                        {"id": "unit-protected-reject", "task_id": "unit-task", "action": "review.reject", "entity_id": review["id"]},
                        {"id": "unit-protected-undo", "task_id": "unit-task", "action": "repair.undo", "entity_id": repair["id"]}])
                self.repository.mutate(reject)
                await self.assert_not_reconciled()

    async def test_cross_task_asset_repair_or_run_cannot_reconcile(self):
        initial = self.repository.state()
        for foreign in ("old_asset", "current_asset", "old_repair", "current_run"):
            with self.subTest(foreign=foreign):
                self.repository.value = copy.deepcopy(initial)
                def corrupt(state):
                    if foreign in {"old_asset", "current_asset"}:
                        identifier = self.old["id"] if foreign == "old_asset" else self.current["id"]
                        next(row for row in state["assets"] if row["id"] == identifier)["task_id"] = "unit-foreign-task"
                    elif foreign == "old_repair":
                        next(row for row in state["tasks"][0]["context"]["matte_repairs"] if row["id"] == "unit-repair-3")["task_id"] = "unit-foreign-task"
                    else:
                        next(row for row in state["runs"] if row["id"] == "unit-repair-run-4")["task_id"] = "unit-foreign-task"
                self.repository.mutate(corrupt)
                await self.assert_not_reconciled()

    async def test_changed_rgb_alpha_manifest_or_source_cannot_reconcile(self):
        paths = [Path(self.current["storage_path"]), Path(self.current["legacy_reference"]["alpha_path"]),
            Path(self.current["legacy_reference"]["manifest_path"]), self.production.settings.storage_root / "source.mp4"]
        for path in paths:
            with self.subTest(path=path.name):
                original = path.read_bytes()
                path.write_bytes(b"changed isolated reconciliation fixture")
                try:
                    await self.assert_not_reconciled()
                finally:
                    path.write_bytes(original)

    async def test_active_original_worker_holds_reconciliation(self):
        self.repository.mutate(lambda state: state["runs"].append({"id": "unit-active-original", "task_id": "unit-task",
            "stage": "compose", "provider": "LegacyComposeAdapter", "status": "running", "pid": 424242, "unit_fixture": True}))
        with patch("app.integrations.review_service.probe_process"):
            await self.assert_not_reconciled()

    async def test_pending_intent_for_old_person_is_superseded_and_current_intent_is_consumed(self):
        initial = self.repository.state()
        for identifier, status in ((self.old["id"], "SUPERSEDED"), (self.current["id"], "CONSUMED")):
            with self.subTest(identifier=identifier):
                self.repository.value = copy.deepcopy(initial)
                self.production.launch.reset_mock()
                self.repository.mutate(lambda state: state["tasks"][0]["context"]["production"].update(
                    deferred_continue={"id": "unit-persisted-intent", "status": "PENDING", "person_asset_id": identifier,
                        "stage": "quality", "unit_fixture": True}))
                outcome = await self.review.reconcile_historical_rejections()
                self.assertEqual(len(outcome), 1)
                intent = self.repository.state()["tasks"][0]["context"]["production"]["deferred_continue"]
                self.assertEqual(intent["id"], "unit-persisted-intent")
                self.assertEqual(intent["status"], status)

    async def test_latest_pending_or_rejected_current_review_holds_reconciliation(self):
        initial = self.repository.state()
        for status in ("pending", "rejected"):
            with self.subTest(status=status):
                self.repository.value = copy.deepcopy(initial)
                self.repository.mutate(lambda state: state["reviews"].append({"id": "review-20261008070000000000-latest",
                    "created_at": "2026-10-08T07:00:00+00:00", "task_id": "unit-task", "asset_id": self.current["id"],
                    "status": status, "unit_fixture": True}))
                await self.assert_not_reconciled()

    async def test_original_recovered_pid_is_probed_after_run_was_marked_failed(self):
        self.repository.mutate(lambda state: state["runs"].append({"id": "unit-recovered-original", "task_id": "unit-task",
            "stage": "compose", "provider": "LegacyComposeAdapter", "status": "failed", "pid": 424242,
            "error_code": "LOCAL_EXECUTION_INTERRUPTED", "unit_fixture": True}))
        self.production.recovered_run_ids = {"unit-recovered-original"}
        with patch("app.integrations.review_service.probe_process") as probe:
            await self.assert_not_reconciled()
        probe.assert_called_once_with(424242)

    async def test_mismatched_undone_repair_id_or_current_parent_proof_cannot_reconcile(self):
        initial = self.repository.state()
        for changed in ("old_repair_id", "old_run_id", "current_parent", "parent_rgb_hash", "parent_alpha_hash",
                "old_technical", "old_unregistered"):
            with self.subTest(changed=changed):
                self.repository.value = copy.deepcopy(initial)
                def corrupt(state):
                    if changed == "old_repair_id":
                        next(row for row in state["assets"] if row["id"] == self.old["id"])["legacy_reference"]["repair_id"] = "unit-wrong-repair"
                    elif changed == "old_run_id":
                        next(row for row in state["assets"] if row["id"] == self.old["id"])["legacy_reference"]["processing_run_id"] = "unit-wrong-run"
                    elif changed == "current_parent":
                        next(row for row in state["tasks"][0]["context"]["matte_repairs"] if row["id"] == "unit-repair-4")["person_asset_id"] = "unit-wrong-parent"
                    elif changed == "parent_rgb_hash":
                        next(row for row in state["assets"] if row["id"] == self.parent["id"])["sha256"] = "unit-wrong-parent-rgb"
                    elif changed == "parent_alpha_hash":
                        next(row for row in state["assets"] if row["id"] == self.parent["id"])["legacy_reference"]["alpha_sha256"] = "unit-wrong-parent-alpha"
                    elif changed == "old_technical":
                        next(row for row in state["assets"] if row["id"] == self.old["id"])["legacy_reference"]["execution_scope"] = "TECHNICAL_VALIDATION"
                    else:
                        next(row for row in state["assets"] if row["id"] == self.old["id"])["registry_status"] = "unregistered"
                self.repository.mutate(corrupt)
                await self.assert_not_reconciled()

    async def test_current_manifest_parent_paths_must_match_applied_parent(self):
        initial = self.repository.state()
        path = Path(self.current["legacy_reference"]["manifest_path"])
        original = path.read_bytes()
        for changed in ("parent_rgb_path", "parent_alpha_path"):
            with self.subTest(changed=changed):
                self.repository.value = copy.deepcopy(initial)
                manifest = json.loads(original)
                manifest[changed] = self.current["legacy_reference"]["alpha_path"]
                path.write_text(json.dumps(manifest))
                self.repository.mutate(lambda state: next(row for row in state["assets"] if row["id"] == self.current["id"])[
                    "legacy_reference"].update(manifest_sha256=file_hash(path)))
                try:
                    await self.assert_not_reconciled()
                finally:
                    path.write_bytes(original)


if __name__ == "__main__":
    unittest.main()
