import copy
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import HTTPException

from app.integrations.production_service import TaskStopped
import test_real_review as review_fixtures


class PromptPreparationAndBackgroundGateTests(review_fixtures.ReviewFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.prepare("person")
        self.person = self.verified_person()
        self.production.collector = SimpleNamespace(context=lambda task_id: {"task_id": task_id, "context_id": "unit-context"},
                                                    queue=AsyncMock(side_effect=AssertionError("no recollection")))
        self.production._video_stage = AsyncMock(return_value={"status": "READY"})

    def complete_prompts(self):
        self.repository.mutate(lambda state: state.update(prompts=[{"id": f"unit-prompt-{ordinal}", "task_id": "unit-task", "ordinal": ordinal,
            "status": "READY", "prompt": "unit background", "negative_prompt": "unit negative", "scene_type": "unit scene",
            "fingerprint": f"unit-prompt-fingerprint-{ordinal}"} for ordinal in range(1, 21)]))

    async def run_queued(self, queued):
        self.production.cancels[queued["batch_id"]] = threading.Event()
        await self.production._task(queued["batch_id"], "unit-task")

    async def test_prompt_can_prepare_with_pending_person_review_without_advancing_review(self):
        before_reviews = copy.deepcopy(self.repository.state()["reviews"])
        self.complete_prompts()
        queued = await self.production.start(["unit-task"], stage="prompt")
        self.assertTrue(queued["accepted"])
        await self.run_queued(queued)
        self.assertEqual([call.args[2] for call in self.production._video_stage.await_args_list], ["prompt"])
        self.production.collector.queue.assert_not_awaited()
        state = self.repository.state()
        self.assertEqual((state["tasks"][0]["status"], state["tasks"][0]["current_stage"]), ("WAITING_REVIEW", "review"))
        self.assertEqual(state["reviews"], before_reviews)
        self.assertFalse(state["tasks"][0]["context"]["production"].get("review_approved"))

    async def test_prompt_missing_source_does_not_invoke_a_paid_provider(self):
        self.repository.mutate(lambda state: state.update(assets=[row for row in state["assets"] if row["type"] != "source"]))
        queued = await self.production.start(["unit-task"], stage="prompt")
        await self.run_queued(queued)
        self.production._video_stage.assert_not_awaited()
        self.assertEqual(self.repository.state()["errors"][-1]["code"], "SAVED_SOURCE_NOT_VERIFIED")

    async def test_pending_person_background_fails_before_queue_run_or_paid_provider(self):
        before = self.repository.state()
        with self.assertRaises(HTTPException) as error:
            await self.production.start(["unit-task"], stage="background", prompt_id="unit-prompt")
        self.assertEqual(error.exception.status_code, 409)
        self.assertIn("PERSON_REVIEW_REQUIRED", error.exception.detail)
        self.assertEqual(self.repository.state(), before)
        self.production._video_stage.assert_not_awaited()

    async def test_retry_cannot_bypass_pending_person_review(self):
        with self.assertRaises(HTTPException) as error:
            await self.production.start(["unit-task"], retry=True, stage="background", prompt_id="unit-prompt")
        self.assertIn("PERSON_REVIEW_REQUIRED", error.exception.detail)
        self.assertEqual(self.repository.state()["runs"], [])
        self.assertEqual(self.repository.state()["batches"], [])

    async def test_approved_current_person_is_required_even_when_generic_flag_is_true(self):
        self.repository.mutate(lambda state: state["tasks"][0]["context"]["production"].update(review_approved=True))
        with self.assertRaises(HTTPException) as error:
            await self.production.start(["unit-task"], retry=True, stage="background", prompt_id="unit-prompt")
        self.assertIn("PERSON_REVIEW_REQUIRED", error.exception.detail)
        self.repository.mutate(lambda state: state["reviews"][0].update(status="approved"))
        queued = await self.production.start(["unit-task"], stage="background", prompt_id="unit-prompt")
        self.assertTrue(queued["accepted"])
        self.assertEqual(self.repository.state()["runs"], [])

    async def test_changed_alpha_of_approved_person_is_rejected_before_queue(self):
        self.repository.mutate(lambda state: state["reviews"][0].update(status="approved"))
        Path(self.person["legacy_reference"]["alpha_path"]).write_bytes(b"unit changed alpha")
        with self.assertRaises(HTTPException) as error:
            await self.production.start(["unit-task"], retry=True, stage="background", prompt_id="unit-prompt")
        self.assertIn("PERSON_REVIEW_REQUIRED", error.exception.detail)
        self.assertEqual(self.repository.state()["batches"], [])

    async def test_approval_change_during_readiness_does_not_return_202(self):
        self.repository.mutate(lambda state: state["reviews"][0].update(status="approved"))
        ready = self.production.readiness.check
        async def changed(**kwargs):
            self.repository.mutate(lambda state: state["reviews"][0].update(status="pending"))
            return await ready(**kwargs)
        self.production.readiness.check = changed
        with self.assertRaises(HTTPException) as error:
            await self.production.start(["unit-task"], stage="background", prompt_id="unit-prompt")
        self.assertIn("PERSON_REVIEW_REQUIRED", error.exception.detail)
        self.assertEqual(self.repository.state()["batches"], [])

    async def test_background_runtime_rechecks_person_before_external_generation(self):
        self.repository.mutate(lambda state: state["reviews"][0].update(status="approved"))
        queued = await self.production.start(["unit-task"], stage="background", prompt_id="unit-prompt")
        self.repository.mutate(lambda state: state["reviews"][0].update(status="pending"))
        await self.run_queued(queued)
        self.production._video_stage.assert_not_awaited()
        self.assertEqual(self.repository.state()["tasks"][0]["status"], "WAITING_REVIEW")
        self.assertEqual(self.repository.state()["runs"], [])

    async def test_prompt_result_does_not_undo_human_rejection(self):
        self.complete_prompts()
        queued = await self.production.start(["unit-task"], stage="prompt")
        async def rejected(*args, **kwargs):
            self.repository.mutate(lambda state: state["tasks"][0]["context"]["production"].update(rejected_review={"asset_id": self.person["id"]}))
            return {"status": "READY"}
        self.production._video_stage = AsyncMock(side_effect=rejected)
        await self.run_queued(queued)
        self.assertEqual(self.repository.state()["tasks"][0]["status"], "REJECTED")

    async def test_background_does_not_replace_human_rejection_with_waiting(self):
        self.repository.mutate(lambda state: state["reviews"][0].update(status="approved"))
        queued = await self.production.start(["unit-task"], stage="background", prompt_id="unit-prompt")
        self.repository.mutate(lambda state: state["tasks"][0]["context"]["production"].update(rejected_review={"asset_id": self.person["id"]}))
        with self.assertRaises(TaskStopped):
            await self.run_queued(queued)
        self.production._video_stage.assert_not_awaited()
        self.assertEqual(self.repository.state()["tasks"][0]["status"], "REJECTED")
        self.assertEqual(self.repository.state()["runs"], [])
