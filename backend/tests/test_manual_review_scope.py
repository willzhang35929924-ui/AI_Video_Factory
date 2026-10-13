"""Real temporary FFmpeg/SQLite contracts; not production acceptance evidence."""
import copy
import threading
import unittest
from unittest.mock import AsyncMock

from fastapi import HTTPException

from app.domain import now
from app.integrations import manual_compose
from app.integrations.asset_registry import file_hash
from app.integrations.manual_compose import TransformRequest
from tests import test_manual_compose as manual_tests


class ManualReviewScopeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = manual_tests.ManualComposeTests()
        await self.fixture.asyncSetUp()
        for function, arguments, keywords in self.fixture._cleanups:
            self.addCleanup(function, *arguments, **keywords)
        self.fixture._cleanups.clear()
        self.service, self.repository = self.fixture.service, self.fixture.repository
        self.production, self.task_id = self.service.production, self.fixture.task_id
        self.production.launch = AsyncMock()
        self.production.jobs = {}
        self.production.cancels = {}
        self.production.closing = False
        self.production.recovered_batches = []
        self.person = await self.fixture.pair()

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    def task(self):
        return next(row for row in self.repository.state()["tasks"] if row["id"] == self.task_id)

    def review(self, asset_id):
        return next(row for row in self.repository.state()["reviews"] if row["asset_id"] == asset_id)

    def normal_parent(self, strategy=None):
        def change(state):
            asset = next(row for row in state["assets"] if row["id"] == self.person["id"])
            asset["source_system"] = "FactoryRGBAlpha2Stream"
            for key in ("manual_upload", "manual_compose", "transform_version_id"):
                asset["legacy_reference"].pop(key, None)
            task = next(row for row in state["tasks"] if row["id"] == self.task_id)
            task["context"].pop("manual_compose", None)
            if strategy is None:
                task["context"].pop("production_strategy", None)
            else:
                task["context"]["production_strategy"] = strategy
        self.repository.mutate(change)

    async def test_normal_asset_in_manual_task_requires_legacy_reviewer(self):
        self.normal_parent()
        self.repository.mutate(lambda state: next(row for row in state["tasks"] if row["id"] == self.task_id)
            ["context"].update(production_strategy="manual_compose", manual_compose={"auto_resume_required": True}))
        review = self.review(self.person["id"])
        before = self.repository.state()
        asset = next(row for row in before["assets"] if row["id"] == self.person["id"])
        self.assertFalse(manual_compose.is_manual_review_asset(asset))
        technical = copy.deepcopy(asset)
        technical["source_system"] = "FactoryTransform"
        technical["legacy_reference"]["execution_scope"] = "TECHNICAL_VALIDATION"
        self.assertFalse(manual_compose.is_manual_review_asset(technical))
        for operation in (self.service.approve, self.service.reject):
            with self.subTest(operation=operation.__name__), self.assertRaises(HTTPException) as caught:
                await operation(review["id"], "UNIT_ONLY_SCOPE")
            self.assertEqual(caught.exception.status_code, 409)
            self.assertEqual(self.repository.state(), before)
        self.production.launch.assert_not_awaited()

    async def test_past_batch_scheduler_cannot_bypass_manual_resume_gate(self):
        baseline = self.repository.state()
        paths = [self.fixture.rgb, self.fixture.alpha, self.fixture.image,
            *self.fixture.settings.storage_root.rglob("*")]
        hashes = {str(path): file_hash(path) for path in paths if path.is_file()}
        self.production.collector = AsyncMock()
        self.production.providers = {"prompt": AsyncMock(), "background": AsyncMock()}
        self.production._recompose_saved = AsyncMock()
        self.production._requested_stage = AsyncMock()
        for parent, requested_stage in (("manual", "compose"), ("normal", None), ("normal", "background")):
            with self.subTest(parent=parent, requested_stage=requested_stage):
                self.repository.mutate(lambda state: state.update(copy.deepcopy(baseline)))
                if parent == "normal":
                    self.normal_parent("rgb_alpha")
                def prepare(state):
                    task = next(row for row in state["tasks"] if row["id"] == self.task_id)
                    task.update(status="RUNNING", current_stage="compose")
                    task["context"]["production_strategy"] = "rgb_alpha"
                    task["context"]["manual_compose"] = {"auto_resume_required": parent == "normal"}
                    task["context"]["production"]["requested_stage"] = requested_stage
                    state["batches"].append({"id": "unit-past-batch", "status": "RUNNING", "task_ids": [self.task_id],
                        "items": [{"task_id": self.task_id, "status": "RUNNING", "stage": "compose"}],
                        "active_task_id": self.task_id, "summary": {}, "created_at": now(), "updated_at": now()})
                self.repository.mutate(prepare)
                self.production.cancels["unit-past-batch"] = threading.Event()
                before = self.repository.state()
                await self.production._task("unit-past-batch", self.task_id)
                after = self.repository.state()
                self.assertEqual(self.task()["status"], "WAITING_REVIEW")
                self.assertEqual(self.task()["context"]["production"]["waiting"]["reason"],
                    "MANUAL_COMPOSE_REQUIRES_EXPLICIT_RESUME")
                for collection in ("runs", "reviews", "assets", "batches", "stages"):
                    self.assertEqual(after[collection], before[collection])
                self.assertEqual({path: file_hash(path) for path in hashes}, hashes)
        self.production._recompose_saved.assert_not_awaited()
        self.production._requested_stage.assert_not_awaited()
        self.assertEqual(self.production.collector.mock_calls, [])
        for provider in self.production.providers.values():
            self.assertEqual(provider.mock_calls, [])
        self.production.launch.assert_not_awaited()

    async def test_manual_person_can_be_reviewed_without_task_manual_strategy(self):
        for operation in ("approve", "reject"):
            with self.subTest(operation=operation):
                person = self.person if operation == "approve" else await self.fixture.pair()
                review = self.review(person["id"])
                self.repository.mutate(lambda state: next(row for row in state["tasks"] if row["id"] == self.task_id)
                    ["context"].update(production_strategy="rgb_alpha"))
                before = self.repository.state()
                self.assertTrue(manual_compose.is_manual_review_asset(person))
                await getattr(self.service, operation)(review["id"], "UNIT_ONLY_HUMAN_DECISION")
                after = self.repository.state()
                self.assertEqual(self.review(person["id"])["status"], "approved" if operation == "approve" else "rejected")
                self.assertEqual(after["runs"], before["runs"])
                self.assertEqual(after["batches"], before["batches"])
                self.assertFalse(any(row["task_id"] == self.task_id and row["type"] == "final" for row in after["assets"]))
        self.production.launch.assert_not_awaited()

    async def test_undo_normal_parent_restores_strategy_but_keeps_explicit_resume_gate(self):
        self.normal_parent()
        baseline = self.repository.state()
        original_review = self.review(self.person["id"])
        hashes = {self.person["storage_path"]: file_hash(self.person["storage_path"]),
            self.person["legacy_reference"]["alpha_path"]: file_hash(self.person["legacy_reference"]["alpha_path"])}
        for original_strategy in (None, "rgb_alpha"):
            with self.subTest(original_strategy=original_strategy):
                self.repository.mutate(lambda state: state.update(copy.deepcopy(baseline)))
                self.normal_parent(original_strategy)
                before = self.repository.state()
                child = (await self.service.apply_transform(self.task_id,
                    TransformRequest(person_asset_id=self.person["id"], x=8)))["person"]
                self.assertTrue(manual_compose.is_manual_review_asset(child))
                await self.service.undo(self.task_id)
                task = self.task()
                self.assertEqual(task["context"]["production"]["current_person_asset_id"], self.person["id"])
                if original_strategy is None:
                    self.assertNotIn("production_strategy", task["context"])
                else:
                    self.assertEqual(task["context"]["production_strategy"], original_strategy)
                self.assertTrue(task["context"]["manual_compose"]["auto_resume_required"])
                self.assertFalse(task["context"]["production"]["review_approved"])
                self.assertEqual(self.review(self.person["id"]), original_review)
                self.assertEqual(self.repository.state()["runs"], before["runs"])
                self.assertEqual(self.repository.state()["batches"], before["batches"])
                self.assertEqual({path: file_hash(path) for path in hashes}, hashes)
        self.production.launch.assert_not_awaited()

    async def test_only_explicit_normal_parent_continuation_can_clear_resume_gate(self):
        self.normal_parent("rgb_alpha")
        def prepare(state):
            state["settings"]["production_policy"] = {"auto_advance": True}
            task = next(row for row in state["tasks"] if row["id"] == self.task_id)
            task["context"]["manual_compose"] = {"auto_resume_required": True, "previous_strategy": "rgb_alpha"}
        self.repository.mutate(prepare)
        baseline = self.repository.state()
        for guard in ("implicit", "manual_person", "missing_parent", "technical_parent", "rejected_parent",
                "unregistered_parent", "superseded_parent", "pause", "cancel", "explicit_normal", "unknown"):
            with self.subTest(guard=guard):
                self.repository.mutate(lambda state: state.update(copy.deepcopy(baseline)))
                def set_guard(state):
                    task = next(row for row in state["tasks"] if row["id"] == self.task_id)
                    if guard == "manual_person":
                        asset = next(row for row in state["assets"] if row["id"] == self.person["id"])
                        asset["source_system"] = "FactoryTransform"
                    elif guard == "missing_parent":
                        task["context"]["production"]["current_person_asset_id"] = "unit-missing-parent"
                    elif guard in {"technical_parent", "rejected_parent", "unregistered_parent", "superseded_parent"}:
                        asset = next(row for row in state["assets"] if row["id"] == self.person["id"])
                        if guard == "technical_parent":
                            asset["legacy_reference"]["execution_scope"] = "TECHNICAL_VALIDATION"
                        elif guard == "rejected_parent":
                            asset["status"] = "rejected"
                        elif guard == "unregistered_parent":
                            asset["registry_status"] = "missing"
                        else:
                            asset["legacy_reference"]["superseded_by_person_asset_id"] = "unit-new-parent"
                    elif guard in {"pause", "cancel"}:
                        task["status"] = "PAUSED" if guard == "pause" else "CANCELLED"
                    elif guard == "unknown":
                        state["runs"].append({"id": "unit-paid-unknown", "task_id": self.task_id, "stage": "background",
                            "provider": "BackgroundRPAAdapter", "status": "failed", "progress": 0, "logs": [],
                            "created_at": now(), "error_code": "UNKNOWN_EXTERNAL_REQUEST"})
                self.repository.mutate(set_guard)
                before = self.repository.state()
                if guard == "implicit":
                    for stage in (None, "prompt"):
                        with self.assertRaises(HTTPException) as caught:
                            await self.production.start([self.task_id], stage=stage)
                        self.assertEqual(caught.exception.status_code, 409)
                        self.assertEqual(self.repository.state(), before)
                if guard == "manual_person":
                    with self.assertRaises(HTTPException) as caught:
                        await self.production.start([self.task_id], stage="compose")
                    self.assertEqual(caught.exception.status_code, 409)
                    self.assertEqual(self.repository.state(), before)
                result = await self.production.continue_task(self.task_id, stage="quality", explicit=guard != "implicit")
                after = self.repository.state()
                manual = self.task()["context"]["manual_compose"]
                if guard == "explicit_normal":
                    self.assertTrue(result.get("accepted"), result)
                    self.assertFalse(manual.get("auto_resume_required"))
                    self.assertEqual(self.task()["status"], "QUEUED")
                else:
                    self.assertFalse(result.get("accepted", False))
                    self.assertTrue(manual["auto_resume_required"])
                    self.assertEqual(after["batches"], before["batches"])
                self.assertEqual(after["reviews"], before["reviews"])
                self.assertEqual(after["runs"], before["runs"])
        self.production.launch.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
