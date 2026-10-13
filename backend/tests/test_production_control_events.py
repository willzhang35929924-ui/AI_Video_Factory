import asyncio
import copy
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from fastapi import HTTPException

from app.integrations.production_service import ProductionService
from test_real_review import ReviewFixture


class ProductionControlEventTests(ReviewFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.prepare()
        def prepare(state):
            task = state["tasks"][0]
            task.update(status="QUEUED", current_stage="collector", collector_result={})
            task["context"]["production"] = {"batch_id": "unit-batch"}
            review = copy.deepcopy(task)
            review.update(id="unit-review-task", status="WAITING_REVIEW")
            state["tasks"].append(review)
            state["batches"] = [{"id": "unit-batch", "status": "PAUSED", "task_ids": [task["id"], review["id"]],
                "items": [{"task_id": row["id"], "status": row["status"], "stage": "collector"} for row in state["tasks"]],
                "active_task_id": None, "summary": {}}]
        self.repository.mutate(prepare)
        self.production.launch = AsyncMock()
        self.production.source_review = SimpleNamespace(approval_record=lambda _: None)
        self.production._download_resume = lambda _: False
        self.production._local_resume = lambda _: False

    async def test_slow_repository_and_review_checks_do_not_block_event_loop(self):
        loop_thread = threading.get_ident()
        observed = []
        state, mutate, queue = self.repository.state, self.repository.mutate, self.repository.read_latest_batch
        def slow(function):
            def call(*args, **kwargs):
                observed.append(threading.get_ident())
                time.sleep(.02)
                return function(*args, **kwargs)
            return call
        self.repository.state = slow(state)
        self.repository.mutate = slow(mutate)
        self.repository.read_latest_batch = slow(queue)
        self.production.source_review.approval_record = slow(lambda _: None)
        self.production._download_resume = slow(lambda _: False)
        self.production._local_resume = slow(lambda _: False)
        ticks = 0
        operation = asyncio.create_task(self.production.control("resume", batch_id="unit-batch"))
        while not operation.done():
            ticks += 1
            await asyncio.sleep(.002)
        result = await operation
        self.assertEqual(result["batch_id"], "unit-batch")
        self.assertGreater(ticks, 20)
        self.assertTrue(observed)
        self.assertNotIn(loop_thread, observed)
        self.production.launch.assert_awaited_once_with("unit-batch")

    async def test_resume_handoff_uses_one_transaction_and_preserves_review(self):
        mutations = []
        mutate = self.repository.mutate
        self.repository.mutate = lambda callback: (mutations.append(callback), mutate(callback))[1]
        await self.production.control("resume", batch_id="unit-batch")
        self.assertEqual(len(mutations), 1)
        state = self.repository.state()
        self.assertEqual(state["batches"][0]["status"], "RUNNING")
        self.assertEqual(state["tasks"][1]["status"], "WAITING_REVIEW")
        self.assertEqual(state["batches"][0]["items"][1]["status"], "WAITING_REVIEW")
        self.assertEqual(sum(row["action"] == "production.resume" for row in state["audits"]), 1)

    async def test_cancel_during_preflight_is_not_overwritten_by_late_resume(self):
        ready = self.production.readiness.check
        async def cancelled(*args, **kwargs):
            self.repository.mutate(lambda state: state["batches"][0].update(status="CANCELLED"))
            return await ready(*args, **kwargs)
        self.production.readiness.check = cancelled
        with self.assertRaises(HTTPException) as caught:
            await self.production.control("resume", batch_id="unit-batch")
        self.assertIn("BATCH_CHANGED", caught.exception.detail)
        self.assertEqual(self.repository.state()["batches"][0]["status"], "CANCELLED")
        self.production.launch.assert_not_awaited()

    async def test_simultaneous_launch_after_threaded_read_creates_only_one_worker(self):
        state = self.repository.state
        def slow_state():
            time.sleep(.02)
            return state()
        self.repository.state = slow_state
        release = asyncio.Event()
        async def running(_):
            await release.wait()
        self.production.run = AsyncMock(side_effect=running)
        await asyncio.gather(ProductionService.launch(self.production, "unit-batch"), ProductionService.launch(self.production, "unit-batch"))
        await asyncio.sleep(0)
        self.assertEqual(len(self.production.jobs), 1)
        self.production.run.assert_awaited_once_with("unit-batch")
        jobs = list(self.production.jobs.values())
        release.set()
        await asyncio.gather(*jobs)


if __name__ == "__main__":
    unittest.main()
