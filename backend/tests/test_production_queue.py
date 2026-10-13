import asyncio
import copy
import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from app.config import Settings
from app.domain import find, new_id, now
from app.integrations.production_service import ProductionService, PRODUCTION_STAGES
from app.integrations.production_readiness import STAGE_REQUIREMENTS
from app.integrations.production_routes import create_production_router
from app.repository import StateRepository
from tests.test_asset_registry import person_fixture

SEED = Path(__file__).parents[1] / "app/seed.json"


class ReadyDependencies:
    def __init__(self, ready=True):
        self.ready = ready
        self.cached = {"checks": [{"workflow_sha256": "isolated-unit-workflow"}]}

    async def check(self, refresh=False, stage=None, task_id=None):
        names = STAGE_REQUIREMENTS.get(stage, set().union(*STAGE_REQUIREMENTS.values()))
        return {"ready": self.ready, "checks": [{"name": name, "status": "READY" if self.ready else "MISSING"} for name in names]}


class IsolatedCollector:
    def __init__(self, repository):
        self.repository = repository
        self.active = {}
        self.calls = []
        self.retry_flags = []
        self.running = 0
        self.maximum_running = 0
        self.failures = set()
        self.quarantines = set()
        self.entered = asyncio.Event()
        self.hold = None

    def context(self, task_id):
        return find(self.repository.state(), "tasks", task_id)["context"]["task_context"]

    async def expand_search(self, task_id, cancel_event=None):
        return {"status": "READY", "task_ids": [task_id], "discovered_count": 1, "run_id": None}

    async def queue(self, task_id, retry=False, *, cancel_event=None):
        self.retry_flags.append(retry)
        run_id = new_id("run")
        self.active[run_id] = threading.Event()
        if cancel_event is not None and cancel_event.is_set():
            self.active[run_id].set()
        self.repository.mutate(lambda state: state["runs"].append({"id": run_id, "task_id": task_id, "stage": "collector",
            "provider": "LegacyCollectorAdapter", "status": "running", "logs": [], "progress": 0, "created_at": now()}))
        return {"run_id": run_id}

    async def run(self, task_id, run_id):
        self.calls.append(task_id)
        self.running += 1
        self.maximum_running = max(self.maximum_running, self.running)
        self.entered.set()
        if self.hold:
            await self.hold.wait()
        await asyncio.sleep(.01)
        failed = task_id in self.failures
        quarantined = task_id in self.quarantines
        def change(state):
            task = find(state, "tasks", task_id)
            task.update(status="QUARANTINED" if quarantined else "FAILED" if failed else "COLLECTED",
                collector_result={"decision": "manual_review" if failed or quarantined else "rejected",
                    "error_code": "UNIT_IDENTITY_UNCERTAIN" if quarantined else "UNIT_NETWORK_FAILURE" if failed else None,
                    "decision_reason": "unit-only result"})
            find(state, "runs", run_id).update(status="failed" if failed else "success", finished_at=now(),
                                              error_code="UNIT_NETWORK_FAILURE" if failed else None)
        self.repository.mutate(change)
        self.running -= 1
        self.active.pop(run_id, None)


class RecordingProvider:
    def __init__(self):
        self.calls = []
        self.assets = SimpleNamespace(implementation_fingerprint=lambda stage: {"factory_algorithm_version": "isolated-" + stage})

    async def execute(self, stage, context, directory, on_event, cancel_event):
        self.calls.append((stage, context["task_id"]))
        return {"status": "NOT_CONNECTED", "error_code": "CUTOUT_OUTPUT_BOUNDARY", "error_message": "unit-only boundary"}


class ProductionQueueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().slow_callback_duration = 2
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = Settings(app_mode="integration", integration_dry_run=False, storage_root=self.root / "storage",
                                 database_url="sqlite:///" + str(self.root / "queue.db"), legacy_video_auto_root=self.root / "legacy")
        self.repository = StateRepository(self.settings.database_url, SEED)
        self.collector = IsolatedCollector(self.repository)
        self.events = []
        async def broadcast(event):
            self.events.append(event)
        self.hub = SimpleNamespace(broadcast=broadcast)
        self.provider = RecordingProvider()
        self.service = self.new_service()

    def new_service(self):
        service = ProductionService(self.repository, self.hub, self.settings, self.collector,
                                    {name: self.provider for name in ("cutout", "prompt", "background", "compose")})
        service.readiness = ReadyDependencies()
        return service

    async def asyncTearDown(self):
        if self.collector.hold:
            self.collector.hold.set()
        await self.service.shutdown()
        self.repository.engine.dispose()
        self.temp.cleanup()

    def tasks(self, count=2):
        ids = []
        def change(state):
            template = state["tasks"][0]
            for index in range(count):
                task = copy.deepcopy(template)
                identifier = "queue-unit-" + str(index)
                context = {"task_id": identifier, "project_id": task["project_id"], "product_id": task["product_id"],
                    "country": task["country"], "keyword": task["keyword"], "product_images": [],
                    "source_document": "isolated-unit", "source_row": index + 1, "context_id": hashlib.sha256(identifier.encode()).hexdigest()[:24]}
                task.update(id=identifier, source_video_id=None, status="CREATED", execution_mode="integration", current_stage="collector",
                            context={"task_context": context}, collector_result={}, created_at=now(), updated_at=now())
                state["tasks"].append(task)
                ids.append(identifier)
                for stage in PRODUCTION_STAGES:
                    state["stages"].append({"id": identifier + "-" + stage, "task_id": identifier, "name": stage,
                        "status": "not_connected" if stage == "publish" else "pending", "start_time": None, "end_time": None,
                        "duration": 0, "retry_count": 0, "error": None})
        self.repository.mutate(change)
        return ids

    async def test_deleted_asset_family_cannot_be_regenerated_by_start_or_review_continue(self):
        task_id = self.tasks(1)[0]
        self.repository.mutate(lambda state: find(state, "tasks", task_id)["context"].update(
            production={"asset_family_deleted": {"status": "INVALIDATED", "deleted_ids": ["disposed-asset"]}}))
        for retry in (False, True):
            with self.assertRaises(HTTPException) as error:
                await self.service.start([task_id], retry=retry)
            self.assertEqual(error.exception.status_code, 409)
            self.assertIn("ASSET_FAMILY_DELETED", error.exception.detail)
        with self.assertRaises(HTTPException) as error:
            await self.service.continue_task(task_id, explicit=True)
        self.assertIn("ASSET_FAMILY_DELETED", error.exception.detail)
        self.assertEqual(self.collector.calls, [])
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.repository.state()["batches"], [])

    async def test_batch_summary_tracks_completed_items_without_nested_state_reads(self):
        ids = self.tasks(2)
        batch = await self.service.start(ids, stage="collector")
        def decide(state):
            first = find(state, "tasks", ids[0])
            first.update(status="REJECTED", collector_result={"decision": "rejected"})
            find(state, "tasks", ids[1]).update(status="WAITING_REVIEW")
        self.repository.mutate(decide)
        with patch.object(self.repository, "state", side_effect=AssertionError("unexpected nested full read")):
            self.service._item_update(batch["batch_id"], ids[0], "REJECTED", "collector")
            self.service._batch_update(batch["batch_id"], status="PAUSED")
        current = self.service.queue()
        self.assertEqual(current["summary"]["task_count"], 2)
        self.assertEqual(current["summary"]["rejected_count"], 1)
        self.assertEqual(current["summary"]["manual_review_count"], 1)
        self.assertEqual(current["summary"]["accepted_count"], 0)

    async def finish(self, batch):
        await self.service.launch(batch["batch_id"])
        job = self.service.jobs.get(batch["batch_id"])
        if job:
            await asyncio.wait_for(job, 5)
        return self.service.queue()

    async def test_readiness_failure_creates_no_batch_or_run(self):
        ids = self.tasks()
        before = self.repository.state()
        self.service.readiness = ReadyDependencies(False)
        with self.assertRaises(HTTPException) as failure:
            await self.service.start(ids)
        self.assertEqual(failure.exception.detail["code"], "PRODUCTION_NOT_READY")
        after = self.repository.state()
        self.assertEqual(after["batches"], before["batches"])
        self.assertEqual(after["runs"], before["runs"])
        self.assertEqual(self.collector.calls, [])

    async def test_single_concurrency_and_failure_does_not_stop_next_task(self):
        ids = self.tasks(3)
        self.collector.failures.add(ids[0])
        batch = await self.service.start(ids)
        with self.assertRaises(HTTPException) as conflict:
            await self.service.start(ids)
        self.assertEqual(conflict.exception.detail, "BATCH_ALREADY_ACTIVE")
        queue = await self.finish(batch)
        self.assertEqual(self.collector.calls, ids)
        self.assertEqual(self.collector.maximum_running, 1)
        self.assertEqual([item["status"] for item in queue["items"]], ["FAILED", "REJECTED", "REJECTED"])
        self.assertEqual(queue["status"], "COMPLETED_WITH_ERRORS")
        self.assertEqual(queue["summary"]["rejected_count"], 2)

    async def test_serial_scheduler_database_wait_does_not_block_event_loop(self):
        ids = self.tasks(1)
        batch = await self.service.start(ids)
        entered, release = threading.Event(), threading.Event()
        update = self.service._claim_next
        def delayed(*args, **kwargs):
            entered.set()
            if not release.wait(2):
                raise AssertionError("scheduler blocked the isolated event loop")
            return update(*args, **kwargs)
        with patch.object(self.service, "_claim_next", side_effect=delayed):
            await self.service.launch(batch["batch_id"])
            job = self.service.jobs[batch["batch_id"]]
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            await asyncio.wait_for(asyncio.sleep(0), .2)
            release.set()
            await asyncio.wait_for(job, 5)
        self.assertEqual(self.collector.calls, ids)
        self.assertEqual(self.service.queue()["status"], "COMPLETED")

    async def test_collector_quarantine_is_terminal_and_next_tasks_continue(self):
        ids = self.tasks(3)
        self.collector.quarantines.add(ids[0])
        batch = await self.service.start(ids)
        queue = await self.finish(batch)
        self.assertEqual(self.collector.calls, ids)
        self.assertEqual(self.collector.maximum_running, 1)
        self.assertEqual([item["status"] for item in queue["items"]], ["QUARANTINED", "REJECTED", "REJECTED"])
        self.assertEqual(queue["status"], "COMPLETED_WITH_ERRORS")
        self.assertEqual(queue["summary"]["quarantined_count"], 1)
        self.assertEqual(queue["summary"]["manual_review_count"], 0)
        current = find(self.repository.state(), "tasks", ids[0])
        self.assertEqual(current["status"], "QUARANTINED")
        self.assertEqual(current["current_stage"], "collector")
        self.assertEqual(current["collector_result"]["error_code"], "UNIT_IDENTITY_UNCERTAIN")
        self.assertEqual(self.provider.calls, [])

    async def test_quarantined_task_cannot_start_retry_or_continue(self):
        task_id = self.tasks(1)[0]
        self.repository.mutate(lambda state: find(state, "tasks", task_id).update(status="QUARANTINED"))
        for retry in (False, True):
            with self.assertRaises(HTTPException) as error:
                await self.service.start([task_id], retry=retry)
            self.assertEqual(error.exception.status_code, 409)
            self.assertIn("COLLECTOR_QUARANTINED", error.exception.detail)
        with self.assertRaises(HTTPException) as error:
            await self.service.continue_task(task_id, explicit=True)
        self.assertIn("COLLECTOR_QUARANTINED", error.exception.detail)
        self.assertEqual(self.repository.state()["batches"], [])
        self.assertEqual(self.collector.calls, [])
        self.assertEqual(self.provider.calls, [])

    async def test_stale_queued_quarantine_is_not_claimed(self):
        ids = self.tasks(2)
        batch = await self.service.start(ids)
        self.repository.mutate(lambda state: find(state, "tasks", ids[0]).update(status="QUARANTINED"))
        queue = await self.finish(batch)
        self.assertEqual(self.collector.calls, ids[1:])
        self.assertEqual([item["status"] for item in queue["items"]], ["QUARANTINED", "REJECTED"])
        self.assertEqual(queue["summary"]["quarantined_count"], 1)
        self.assertEqual([run["task_id"] for run in self.repository.state()["runs"] if run["task_id"] in ids], ids[1:])

    async def test_resume_skips_quarantine_with_unknown_request_and_continues_pending(self):
        ids = self.tasks(2)
        batch = await self.service.start(ids)
        def pause(state):
            selected = find(state, "batches", batch["batch_id"])
            selected["status"] = "PAUSED"
            selected["items"][0]["status"] = "QUARANTINED"
            find(state, "tasks", ids[0]).update(status="QUARANTINED")
            state["runs"].append({"id": "quarantined-unknown-run", "task_id": ids[0], "stage": "collector",
                "provider": "LegacyCollectorAdapter", "status": "failed", "logs": [], "progress": 0,
                "error_code": "UNKNOWN_EXTERNAL_REQUEST", "created_at": now()})
        self.repository.mutate(pause)
        await self.service.control("resume", batch_id=batch["batch_id"])
        await asyncio.wait_for(self.service.jobs[batch["batch_id"]], 5)
        current = self.repository.state()
        self.assertEqual(self.collector.calls, ids[1:])
        self.assertEqual(find(current, "tasks", ids[0])["status"], "QUARANTINED")
        self.assertEqual(find(current, "runs", "quarantined-unknown-run")["error_code"], "UNKNOWN_EXTERNAL_REQUEST")
        self.assertEqual(self.service.queue()["summary"]["quarantined_count"], 1)

    async def test_restart_preserves_quarantine_and_resumes_other_queued_task(self):
        ids = self.tasks(2)
        batch = await self.service.start(ids)
        def stale(state):
            selected = find(state, "batches", batch["batch_id"])
            selected.update(status="RUNNING", active_task_id=ids[0])
            selected["items"][0]["status"] = "RUNNING"
            find(state, "tasks", ids[0]).update(status="QUARANTINED")
            state["runs"].append({"id": "quarantined-history", "task_id": ids[0], "stage": "collector",
                "provider": "LegacyCollectorAdapter", "status": "failed", "logs": [], "progress": 0,
                "error_code": "UNKNOWN_EXTERNAL_REQUEST", "created_at": now()})
        self.repository.mutate(stale)
        await self.service.recover()
        self.assertEqual(self.service.queue()["items"][0]["status"], "QUARANTINED")
        await self.service.resume_recovered()
        await asyncio.wait_for(self.service.jobs[batch["batch_id"]], 5)
        self.assertEqual(self.collector.calls, ids[1:])
        current = self.repository.state()
        self.assertEqual(find(current, "tasks", ids[0])["status"], "QUARANTINED")
        self.assertNotIn("waiting", find(current, "tasks", ids[0])["context"]["production"])
        self.assertEqual(find(current, "runs", "quarantined-history")["status"], "failed")
        self.assertEqual(self.service.queue()["status"], "COMPLETED_WITH_ERRORS")

    async def test_late_failure_or_wait_cannot_overwrite_quarantine(self):
        task_id = self.tasks(1)[0]
        self.repository.mutate(lambda state: find(state, "tasks", task_id).update(status="QUARANTINED"))
        before = self.repository.state()
        self.service._task_status(task_id, "WAITING_REVIEW", "review")
        self.service._task_failure(task_id, "PRODUCTION_ERROR", "late unit error")
        self.service._wait(task_id, "UNKNOWN_EXTERNAL_REQUEST", "background", "late unit wait")
        after = self.repository.state()
        self.assertEqual(find(after, "tasks", task_id), find(before, "tasks", task_id))
        self.assertEqual(after["errors"], before["errors"])

    async def test_cancel_preserves_stale_queued_quarantine(self):
        ids = self.tasks(2)
        batch = await self.service.start(ids)
        self.repository.mutate(lambda state: find(state, "tasks", ids[0]).update(status="QUARANTINED"))
        await self.service.control("cancel", batch_id=batch["batch_id"])
        self.assertEqual([item["status"] for item in self.service.queue()["items"]], ["QUARANTINED", "CANCELLED"])
        self.assertEqual(find(self.repository.state(), "tasks", ids[0])["status"], "QUARANTINED")
        self.assertEqual(self.collector.calls, [])

    async def _control_at_claim_boundary(self, action, *, after_claim=False):
        ids = self.tasks(3)
        batch = await self.service.start(ids)
        entered, release = threading.Event(), threading.Event()
        claim = self.service._claim_next
        first = True
        def delayed(batch_id):
            nonlocal first
            if not first:
                return claim(batch_id)
            first = False
            claimed = claim(batch_id) if after_claim else None
            entered.set()
            if not release.wait(3):
                raise AssertionError("isolated claim barrier was not released")
            return claimed if after_claim else claim(batch_id)
        with patch.object(self.service, "_claim_next", side_effect=delayed):
            await self.service.launch(batch["batch_id"])
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            try:
                await self.service.control(action, ids[0] if action == "skip" else None)
                self.assertEqual(self.collector.calls, [])
            finally:
                release.set()
            if action == "pause":
                await asyncio.sleep(.1)
                self.assertEqual(self.collector.calls, [])
                self.assertEqual(self.service.queue()["status"], "PAUSED")
                await self.service.control("resume")
            await asyncio.wait_for(self.service.jobs[batch["batch_id"]], 5)
        return ids, self.service.queue()

    async def test_cancel_before_atomic_claim_never_starts_collector(self):
        ids, queue = await self._control_at_claim_boundary("cancel")
        self.assertEqual(self.collector.calls, [])
        self.assertEqual(queue["status"], "CANCELLED")
        self.assertTrue(all(item["status"] == "CANCELLED" for item in queue["items"]))
        self.assertFalse(any(run["task_id"] in ids for run in self.repository.state()["runs"]))

    async def test_skip_before_atomic_claim_preserves_skip_and_runs_remaining(self):
        ids, queue = await self._control_at_claim_boundary("skip")
        self.assertEqual(self.collector.calls, ids[1:])
        self.assertEqual(queue["items"][0]["status"], "SKIPPED")
        self.assertEqual(find(self.repository.state(), "tasks", ids[0])["status"], "SKIPPED")
        self.assertFalse(any(run["task_id"] == ids[0] for run in self.repository.state()["runs"]))

    async def test_cancel_after_claim_before_execution_never_starts_collector(self):
        ids, queue = await self._control_at_claim_boundary("cancel", after_claim=True)
        self.assertEqual(self.collector.calls, [])
        self.assertEqual(queue["status"], "CANCELLED")
        self.assertTrue(all(item["status"] == "CANCELLED" for item in queue["items"]))
        self.assertFalse(any(run["task_id"] in ids for run in self.repository.state()["runs"]))

    async def test_pause_after_claim_waits_for_resume_without_reclaim(self):
        ids, queue = await self._control_at_claim_boundary("pause", after_claim=True)
        self.assertEqual(self.collector.calls, ids)
        self.assertEqual(len([run for run in self.repository.state()["runs"] if run["task_id"] in ids]), len(ids))
        self.assertEqual(queue["status"], "COMPLETED")

    async def test_skip_rechecks_current_claim_inside_the_transaction(self):
        ids = self.tasks(1)
        batch = await self.service.start(ids)
        batch_id = batch["batch_id"]
        self.service.cancels[batch_id] = threading.Event()
        mutate = self.repository.mutate
        def claimed_before_skip(change):
            if change.__name__ == "skip_pending":
                self.assertEqual(self.service._claim_next(batch_id), ids[0])
            return mutate(change)
        with patch.object(self.repository, "mutate", side_effect=claimed_before_skip):
            with self.assertRaises(HTTPException) as conflict:
                await self.service.control("skip", ids[0])
        self.assertEqual(conflict.exception.status_code, 409)
        self.assertEqual(self.service.queue()["items"][0]["status"], "RUNNING")
        self.assertEqual(find(self.repository.state(), "tasks", ids[0])["status"], "QUEUED")
        self.assertEqual(self.collector.calls, [])

    async def test_discovered_children_enter_same_dynamic_queue_and_continue_after_failure(self):
        ids = self.tasks(3)
        self.collector.failures.add(ids[0])
        async def discovered(task_id, cancel_event=None):
            return {"status": "READY", "task_ids": ids if task_id == ids[0] else [task_id], "discovered_count": len(ids) if task_id == ids[0] else 1, "run_id": None}
        self.collector.expand_search = discovered
        batch = await self.service.start(ids[:1])
        queue = await self.finish(batch)
        self.assertEqual(queue["task_ids"], ids)
        self.assertEqual(self.collector.calls, ids)
        self.assertEqual(self.collector.maximum_running, 1)
        self.assertEqual([item["status"] for item in queue["items"]], ["FAILED", "REJECTED", "REJECTED"])
        self.assertEqual(queue["status"], "COMPLETED_WITH_ERRORS")

    async def test_pause_at_stage_boundary_resume_and_skip_preserve_queue(self):
        ids = self.tasks(3)
        first_finished = asyncio.Event()
        broadcast = self.hub.broadcast
        async def notify_finished(event):
            await broadcast(event)
            if event.task_id == ids[0] and event.message == "当前任务结束，继续队列":
                first_finished.set()
        self.hub.broadcast = notify_finished
        self.collector.hold = asyncio.Event()
        batch = await self.service.start(ids)
        await self.service.launch(batch["batch_id"])
        await asyncio.wait_for(self.collector.entered.wait(), 2)
        await self.service.control("pause")
        await self.service.control("skip", ids[2])
        self.collector.hold.set()
        await asyncio.wait_for(first_finished.wait(), 3)
        self.assertEqual(self.collector.calls, [ids[0]])
        self.assertEqual(self.service.queue()["status"], "PAUSED")
        self.assertEqual(self.service.queue()["items"][0]["status"], "REJECTED")
        await self.service.control("resume")
        await asyncio.wait_for(self.service.jobs[batch["batch_id"]], 3)
        self.assertEqual(self.collector.calls, ids[:2])
        self.assertEqual(self.service.queue()["items"][2]["status"], "SKIPPED")

    async def test_failed_retry_creates_new_run_and_retains_failed_history(self):
        task_id = self.tasks(1)[0]
        self.collector.failures.add(task_id)
        first = await self.service.start([task_id])
        await self.finish(first)
        previous = next(run for run in self.repository.state()["runs"] if run["task_id"] == task_id)
        self.assertEqual(previous["error_code"], "UNIT_NETWORK_FAILURE")
        with self.assertRaises(HTTPException):
            await self.service.start([task_id])
        self.collector.failures.clear()
        second = await self.service.start([task_id], retry=True)
        await self.finish(second)
        runs = [run for run in self.repository.state()["runs"] if run["task_id"] == task_id]
        self.assertEqual(len(runs), 2)
        self.assertNotEqual(runs[0]["id"], runs[1]["id"])
        self.assertEqual(runs[0], previous)
        self.assertEqual(runs[1]["status"], "success")
        self.assertEqual(self.collector.retry_flags, [False, True])

    async def test_cancel_before_launch_must_not_execute_collector(self):
        ids = self.tasks()
        batch = await self.service.start(ids)
        await self.service.control("cancel")
        await self.finish(batch)
        self.assertEqual(self.collector.calls, [])
        self.assertEqual(self.service.queue()["status"], "CANCELLED")
        self.assertTrue(all(item["status"] == "CANCELLED" for item in self.service.queue()["items"]))

    async def test_restore_cancelled_keeps_same_batch_historical_results_and_serial_continuation(self):
        ids = self.tasks(5)
        batch = await self.service.start(ids)
        await self.service.control("cancel")
        def historical(state):
            for identifier, status, decision in ((ids[0], "REJECTED", "rejected"), (ids[1], "WAITING_REVIEW", "manual_review")):
                find(state, "tasks", identifier).update(status=status, collector_result={"decision": decision})
                next(item for item in find(state, "batches", batch["batch_id"])["items"] if item["task_id"] == identifier)["status"] = status
                state["runs"].append({"id": "old-" + identifier, "task_id": identifier, "stage": "collector", "provider": "LegacyCollectorAdapter",
                    "status": "success", "progress": 100, "logs": ["original history"], "created_at": now()})
        self.repository.mutate(historical)
        original = self.repository.state()
        plan = self.service.cancelled_restore_plan(batch["batch_id"])
        self.assertEqual(plan["eligible_task_ids"], ids[2:])
        result = await self.service.restore_cancelled(batch["batch_id"], confirmed=True, fingerprint=plan["fingerprint"])
        self.assertEqual(result["batch_id"], batch["batch_id"])
        self.assertEqual(result["restored_count"], 3)
        self.assertEqual(result["preserved_count"], 2)
        self.assertEqual(self.collector.calls, [])
        restored = self.repository.state()
        self.assertEqual(len(restored["batches"]), len(original["batches"]))
        self.assertEqual(restored["runs"], original["runs"])
        self.assertEqual([find(restored, "tasks", identifier) for identifier in ids[:2]], [find(original, "tasks", identifier) for identifier in ids[:2]])
        self.assertEqual(find(restored, "tasks", ids[2])["context"]["production"]["cancellation_restorations"][0]["previous_task_status"], "CANCELLED")
        record = next(row for row in restored["audits"] if row["action"] == "production.restore_cancelled")
        self.assertEqual(json.loads(record["message"])["task_ids"], ids[2:])
        self.collector.failures.add(ids[3])
        finished = await self.finish(batch)
        self.assertEqual(self.collector.calls, ids[2:])
        self.assertEqual(self.collector.maximum_running, 1)
        self.assertEqual(finished["status"], "COMPLETED_WITH_ERRORS")

    async def test_restore_cancelled_requires_confirmation_and_ordinary_resume_stays_blocked(self):
        batch = await self.service.start(self.tasks())
        await self.service.control("cancel")
        plan = self.service.cancelled_restore_plan(batch["batch_id"])
        with self.assertRaises(HTTPException) as denied:
            await self.service.restore_cancelled(batch["batch_id"], confirmed=False, fingerprint=plan["fingerprint"])
        self.assertEqual(denied.exception.status_code, 422)
        with self.assertRaises(HTTPException) as denied:
            await self.service.control("resume", batch_id=batch["batch_id"])
        self.assertEqual(denied.exception.detail, "NO_ACTIVE_BATCH")
        self.assertEqual(self.service.queue()["status"], "CANCELLED")
        self.assertEqual(self.collector.calls, [])

    async def test_restore_cancelled_never_replays_prior_run_unknown_request_or_output(self):
        ids = self.tasks(4)
        batch = await self.service.start(ids)
        await self.service.control("cancel")
        def started(state):
            for identifier, code in ((ids[0], None), (ids[1], "UNKNOWN_EXTERNAL_REQUEST")):
                state["runs"].append({"id": "previous-" + identifier, "task_id": identifier, "stage": "collector", "provider": "LegacyCollectorAdapter",
                    "status": "failed", "error_code": code, "progress": 0, "logs": [], "created_at": now()})
            find(state, "tasks", ids[2])["collector_result"] = {"decision": "rejected"}
        self.repository.mutate(started)
        plan = self.service.cancelled_restore_plan(batch["batch_id"])
        self.assertEqual(plan["eligible_task_ids"], [ids[3]])
        before = self.repository.state()
        await self.service.restore_cancelled(batch["batch_id"], confirmed=True, fingerprint=plan["fingerprint"])
        after = self.repository.state()
        self.assertEqual(after["runs"], before["runs"])
        self.assertTrue(all(find(after, "tasks", identifier)["status"] == "CANCELLED" for identifier in ids[:3]))

    async def test_restore_cancelled_excludes_existing_files_even_without_database_run(self):
        ids = self.tasks(2)
        batch = await self.service.start(ids)
        await self.service.control("cancel")
        task = find(self.repository.state(), "tasks", ids[0])
        path = self.settings.storage_root / "projects" / task["project_id"] / "tasks" / ids[0] / "metadata" / "collector-state.json"
        path.parent.mkdir(parents=True)
        path.write_text('{"status":"external_inflight"}')
        plan = self.service.cancelled_restore_plan(batch["batch_id"])
        self.assertEqual(plan["eligible_task_ids"], [ids[1]])
        self.assertEqual(plan["excluded"][0]["reason"], "PRIOR_FILES_PRESERVED")
        self.assertEqual(path.read_text(), '{"status":"external_inflight"}')

    async def test_restore_cancelled_readiness_failure_keeps_cancelled_history(self):
        batch = await self.service.start(self.tasks())
        await self.service.control("cancel")
        plan = self.service.cancelled_restore_plan(batch["batch_id"])
        self.service.readiness = ReadyDependencies(False)
        before = self.repository.state()
        with self.assertRaises(HTTPException) as denied:
            await self.service.restore_cancelled(batch["batch_id"], confirmed=True, fingerprint=plan["fingerprint"])
        self.assertEqual(denied.exception.detail["code"], "PRODUCTION_NOT_READY")
        self.assertEqual(self.repository.state(), before)

    async def test_restore_cancelled_active_run_or_worker_prevents_restore(self):
        ids = self.tasks()
        batch = await self.service.start(ids)
        await self.service.control("cancel")
        self.repository.mutate(lambda state: state["runs"].append({"id": "live-unit", "task_id": ids[0], "stage": "collector", "provider": "LegacyCollectorAdapter",
            "status": "running", "progress": 0, "logs": [], "created_at": now()}))
        with self.assertRaises(HTTPException) as denied:
            self.service.cancelled_restore_plan(batch["batch_id"])
        self.assertTrue(denied.exception.detail.startswith("COLLECTOR_ACTIVE"))
        self.assertEqual(self.service.queue()["status"], "CANCELLED")

    async def test_restore_cancelled_rechecks_state_after_readiness_and_rejects_stale_plan(self):
        ids = self.tasks()
        batch = await self.service.start(ids)
        await self.service.control("cancel")
        plan = self.service.cancelled_restore_plan(batch["batch_id"])
        async def readiness(**kwargs):
            self.repository.mutate(lambda state: find(state, "tasks", ids[0]).update(collector_result={"decision": "manual_review"}))
            return await ReadyDependencies().check(**kwargs)
        self.service.readiness.check = readiness
        with self.assertRaises(HTTPException) as denied:
            await self.service.restore_cancelled(batch["batch_id"], confirmed=True, fingerprint=plan["fingerprint"])
        self.assertTrue(denied.exception.detail.startswith("RESTORE_PLAN_CHANGED"))
        self.assertEqual(self.service.queue()["status"], "CANCELLED")
        self.assertFalse(any(row["action"] == "production.restore_cancelled" for row in self.repository.state()["audits"]))

    async def test_restore_cancelled_route_returns_202_and_schedules_original_batch_once(self):
        batch = await self.service.start(self.tasks())
        await self.service.control("cancel")
        app = FastAPI()
        app.include_router(create_production_router(self.service))
        with patch.object(self.service, "launch", new_callable=AsyncMock) as launch:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://isolated") as client:
                path = "/api/production/batches/" + batch["batch_id"] + "/restore-cancelled"
                plan = (await client.get(path)).json()
                invalid = await client.post(path, json={"confirmed": "true", "fingerprint": plan["fingerprint"]})
                self.assertEqual(invalid.status_code, 422)
                response = await client.post(path, json={"confirmed": True, "fingerprint": plan["fingerprint"]})
                self.assertEqual(response.status_code, 202, response.text)
                self.assertEqual(response.json()["batch_id"], batch["batch_id"])
                launch.assert_awaited_once_with(batch["batch_id"])
                repeated = await client.post(path, json={"confirmed": True, "fingerprint": plan["fingerprint"]})
                self.assertEqual(repeated.status_code, 409)
                launch.assert_awaited_once()

    async def test_pause_before_launch_must_wait_for_explicit_resume(self):
        ids = self.tasks()
        batch = await self.service.start(ids)
        await self.service.control("pause")
        await self.service.launch(batch["batch_id"])
        await asyncio.sleep(.12)
        self.assertEqual(self.collector.calls, [])
        self.assertEqual(self.service.queue()["status"], "PAUSED")
        await self.service.control("resume")
        await asyncio.wait_for(self.service.jobs[batch["batch_id"]], 3)
        self.assertEqual(self.collector.calls, ids)

    async def test_paused_running_task_resumes_existing_worker_without_duplicate_run(self):
        ids = self.tasks(1)
        self.collector.hold = asyncio.Event()
        batch = await self.service.start(ids)
        await self.service.launch(batch["batch_id"])
        await asyncio.wait_for(self.collector.entered.wait(), 2)
        worker = self.service.jobs[batch["batch_id"]]
        before = copy.deepcopy(self.repository.state()["runs"])
        await self.service.control("pause", ids[0], batch_id=batch["batch_id"])
        result = await self.service.control("resume", ids[0], batch_id=batch["batch_id"])
        self.assertEqual(result["scope"], "BATCH")
        self.assertIs(self.service.jobs[batch["batch_id"]], worker)
        self.assertEqual(self.repository.state()["runs"], before)
        self.assertEqual(self.service.queue()["status"], "RUNNING")
        self.collector.hold.set()
        await asyncio.wait_for(worker, 3)
        self.assertEqual(self.collector.calls, ids)
        self.assertEqual(len([row for row in self.repository.state()["runs"] if row["task_id"] == ids[0]]), 1)

    async def test_task_control_cannot_operate_an_unrelated_or_changed_batch(self):
        ids = self.tasks(3)
        batch = await self.service.start(ids[:2])
        for action, task_id, batch_id in (("cancel", ids[2], batch["batch_id"]), ("pause", ids[0], "stale-batch")):
            with self.subTest(action=action), self.assertRaises(HTTPException) as error:
                await self.service.control(action, task_id, batch_id=batch_id)
            self.assertEqual(error.exception.status_code, 409)
            self.assertEqual(self.service.queue()["status"], "QUEUED")
        result = await self.service.control("cancel", ids[0], batch_id=batch["batch_id"])
        self.assertEqual(result["scope"], "BATCH")
        self.assertEqual(result["affected_task_ids"], ids[:2])

    async def test_collector_queue_controls_reject_stale_batch_without_mutation(self):
        ids = self.tasks(2)
        batch = await self.service.start(ids)
        before = copy.deepcopy(self.repository.state())
        for action in ("pause", "resume", "cancel"):
            with self.subTest(action=action), self.assertRaises(HTTPException) as error:
                await self.service.control(action, batch_id="stale-collector-batch")
            self.assertEqual(error.exception.detail, "BATCH_CHANGED：队列已更新，请刷新后再操作")
            self.assertEqual(self.repository.state(), before)
            self.assertNotIn(batch["batch_id"], self.service.cancels)
        await self.service.control("pause", batch_id=batch["batch_id"])
        self.assertEqual(self.service.queue()["status"], "PAUSED")
        self.assertEqual([item["status"] for item in self.service.queue()["items"]], ["QUEUED", "QUEUED"])

    async def test_duplicate_pause_and_resume_of_running_queue_are_rejected(self):
        ids = self.tasks(1)
        batch = await self.service.start(ids)
        await self.service.control("pause", ids[0], batch_id=batch["batch_id"])
        with self.assertRaises(HTTPException) as error:
            await self.service.control("pause", ids[0], batch_id=batch["batch_id"])
        self.assertEqual(error.exception.detail, "QUEUE_ALREADY_PAUSED")
        self.service._batch_update(batch["batch_id"], status="RUNNING")
        with self.assertRaises(HTTPException) as error:
            await self.service.control("resume", ids[0], batch_id=batch["batch_id"])
        self.assertEqual(error.exception.detail, "QUEUE_ALREADY_RUNNING")

    async def test_cancel_during_last_collector_keeps_history_and_cancelled_batch(self):
        ids = self.tasks(1)
        self.collector.hold = asyncio.Event()
        batch = await self.service.start(ids)
        await self.service.launch(batch["batch_id"])
        await asyncio.wait_for(self.collector.entered.wait(), 2)
        await self.service.control("cancel")
        self.collector.hold.set()
        await asyncio.wait_for(self.service.jobs[batch["batch_id"]], 3)
        self.assertEqual(self.service.queue()["status"], "CANCELLED")
        state = self.repository.state()
        self.assertEqual(len([run for run in state["runs"] if run["task_id"] == ids[0]]), 1)
        self.assertEqual(find(state, "tasks", ids[0])["collector_result"]["decision"], "rejected")

    async def test_readiness_race_cannot_queue_task_that_started_collector(self):
        ids = self.tasks(1)
        entered, release = asyncio.Event(), asyncio.Event()
        async def readiness(refresh=False, stage=None, task_id=None):
            entered.set()
            await release.wait()
            return {"ready": True, "checks": [{"name": name, "status": "READY"} for name in set().union(*STAGE_REQUIREMENTS.values())]}
        self.service.readiness.check = readiness
        start = asyncio.create_task(self.service.start(ids))
        await asyncio.wait_for(entered.wait(), 2)
        run = await self.collector.queue(ids[0])
        release.set()
        with self.assertRaises(HTTPException) as conflict:
            await start
        self.assertEqual(conflict.exception.detail, "ALREADY_RUNNING")
        self.assertEqual(self.repository.state()["batches"], [])
        self.assertEqual(find(self.repository.state(), "runs", run["run_id"])["status"], "running")

    async def test_recover_does_not_auto_launch_and_resume_keeps_pending_items(self):
        ids = self.tasks()
        batch = await self.service.start(ids)
        restarted = self.new_service()
        await restarted.recover()
        self.assertEqual(restarted.queue()["status"], "PAUSED")
        self.assertEqual(restarted.jobs, {})
        self.assertEqual(self.collector.calls, [])
        await restarted.control("resume")
        await asyncio.wait_for(restarted.jobs[batch["batch_id"]], 3)
        self.assertEqual(self.collector.calls, ids)
        await restarted.shutdown()

    async def test_shutdown_does_not_cancel_tasks_that_have_not_started(self):
        ids = self.tasks()
        self.collector.hold = asyncio.Event()
        batch = await self.service.start(ids)
        await self.service.launch(batch["batch_id"])
        await asyncio.wait_for(self.collector.entered.wait(), 2)
        await self.service.control("pause")
        self.collector.hold.set()
        await asyncio.sleep(.12)
        await self.service.shutdown()
        queued = self.service.queue()
        self.assertEqual(queued["status"], "PAUSED")
        self.assertEqual(queued["items"][1]["status"], "QUEUED")
        restarted = self.new_service()
        await restarted.recover()
        await restarted.control("resume")
        await asyncio.wait_for(restarted.jobs[batch["batch_id"]], 3)
        self.assertEqual(self.collector.calls, ids)
        await restarted.shutdown()

    async def test_unknown_paid_run_cannot_be_hidden_by_later_success(self):
        task_id = self.tasks(1)[0]
        def change(state):
            find(state, "tasks", task_id)["status"] = "FAILED"
            for index, code in enumerate(("UNKNOWN_EXTERNAL_REQUEST", None)):
                state["runs"].append({"id": "paid-unit-" + str(index), "task_id": task_id, "stage": "background",
                    "provider": "BackgroundRPAAdapter", "status": "failed" if code else "success", "progress": 0,
                    "logs": [], "created_at": now(), "error_code": code})
        self.repository.mutate(change)
        with self.assertRaises(HTTPException) as blocked:
            await self.service.start([task_id], retry=True)
        self.assertIn("UNKNOWN_EXTERNAL_REQUEST", str(blocked.exception.detail))
        self.assertEqual(self.collector.calls, [])

    async def test_cached_source_retries_downstream_without_recollecting(self):
        task_id = self.tasks(1)[0]
        source_file = self.root / "storage/source.mp4"
        source_file.parent.mkdir(parents=True)
        source_file.write_bytes(b"unit-only verified-provider fixture")
        def change(state):
            task = find(state, "tasks", task_id)
            task.update(status="FAILED", collector_result={"decision": "accepted", "video_id": "unit-video",
                "video_url": "https://www.tiktok.com/@unit/video/123", "context_id": task["context"]["task_context"]["context_id"],
                "legacy_vidbee_task_id": "unit-vidbee"})
            template = copy.deepcopy(state["assets"][0])
            template.update(id="source-unit", task_id=task_id, type="source", status="READY", registry_status="registered",
                asset_type="SOURCE_VIDEO", storage_path=str(source_file), external_path=str(source_file), size=source_file.stat().st_size,
                sha256=hashlib.sha256(source_file.read_bytes()).hexdigest(), legacy_reference={})
            state["assets"].append(template)
        self.repository.mutate(change)
        async def verify(*args):
            return {"status": "READY", "assets": []}
        self.service.vidbee = SimpleNamespace(verify=verify)
        batch = await self.service.start([task_id], retry=True, stage="cutout")
        await self.finish(batch)
        self.assertEqual(self.collector.calls, [])
        self.assertEqual(self.provider.calls, [("cutout", task_id)], [row for row in self.repository.state()["errors"] if row["task_id"] == task_id])
        self.assertEqual(find(self.repository.state(), "tasks", task_id)["status"], "WAITING_REVIEW")

    async def test_composition_cache_checks_input_config_workflow_and_file(self):
        task_id = self.tasks(1)[0]
        self.settings.factory_comfy_compose_workflow = self.root / "isolated-compose-workflow.json"
        task = find(self.repository.state(), "tasks", task_id)
        path = self.root / "composition.mp4"
        path.write_bytes(b"immutable-unit-asset")
        person, background = {"sha256": "person-v1", "legacy_reference": {"alpha_sha256": "alpha-v1"}}, {"sha256": "background-v1"}
        fingerprint = self.service._fingerprint(task, "compose", "prompt-unit", person, background)
        def change(state):
            asset = copy.deepcopy(state["assets"][0])
            asset.update(id="compose-unit", task_id=task_id, type="compose", status="READY", registry_status="registered",
                storage_path=str(path), external_path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                legacy_reference={"factory_fingerprint": fingerprint, "prompt_id": "prompt-unit"})
            state["assets"].append(asset)
        self.repository.mutate(change)
        self.assertIsNotNone(self.service._asset(task_id, "compose", "prompt-unit", person=person, background=background))
        self.assertIsNone(self.service._asset(task_id, "compose", "prompt-unit", person={"sha256": "person-v2"}, background=background))
        self.assertIsNone(self.service._asset(task_id, "compose", "prompt-unit", person={**person, "legacy_reference": {"alpha_sha256": "alpha-v2"}}, background=background))
        self.settings.factory_comfy_compose_workflow.parent.mkdir(parents=True, exist_ok=True)
        self.settings.factory_comfy_compose_workflow.write_text('{"unit_workflow": "changed"}')
        self.assertIsNone(self.service._asset(task_id, "compose", "prompt-unit", person=person, background=background))
        self.settings.factory_comfy_compose_workflow.unlink()
        self.settings.legacy_video_auto_root.mkdir()
        configuration = self.settings.legacy_video_auto_root / "config.yaml"
        configuration.write_text("unit_config: changed")
        self.assertIsNone(self.service._asset(task_id, "compose", "prompt-unit", person=person, background=background))
        configuration.unlink()
        path.write_bytes(b"changed-unit-content")
        self.assertIsNone(self.service._asset(task_id, "compose", "prompt-unit", person=person, background=background))

    async def test_person_registry_requires_independent_aligned_alpha(self):
        task_id = self.tasks(1)[0]
        task = find(self.repository.state(), "tasks", task_id)
        self.settings.storage_root.mkdir()
        person, alpha = self.settings.storage_root / "person.mov", self.settings.storage_root / "alpha.mov"
        person.write_bytes(b"unit-rgb")
        alpha.write_bytes(b"unit-alpha")
        media = {"width": 1080, "height": 1920, "fps": 30, "duration": 2, "codec": "ffv1", "frame_count": 60, "audio": []}
        before = len(self.repository.state()["assets"])
        with patch("app.integrations.asset_registry.probe_media", return_value=media):
            with self.assertRaisesRegex(ValueError, "PERSON_ALPHA_REQUIRED"):
                self.service.registry.register(task, {"path": str(person)}, "person", "run-unit")
            with self.assertRaisesRegex(ValueError, "PERSON_ALPHA_MUST_BE_INDEPENDENT"):
                self.service.registry.register(task, {"path": str(person), "legacy_reference": {"alpha_path": str(person)}}, "person", "run-unit")
        with patch("app.integrations.asset_registry.probe_media", side_effect=[media, {**media, "fps": 25}]):
            with self.assertRaisesRegex(ValueError, "PERSON_ALPHA_ALIGNMENT_MISMATCH"):
                self.service.registry.register(task, {"path": str(person), "legacy_reference": {"alpha_path": str(alpha)}}, "person", "run-unit")
        self.assertEqual(len(self.repository.state()["assets"]), before)

    async def test_new_alpha_preserves_new_person_version_even_if_rgb_unchanged(self):
        task_id = self.tasks(1)[0]
        task = find(self.repository.state(), "tasks", task_id)
        self.settings.storage_root.mkdir()
        first_evidence, _manifest, first_probe = person_fixture(self.settings.storage_root / "mother", task)
        person, alpha_one = Path(first_evidence["path"]), Path(first_evidence["legacy_reference"]["alpha_path"])
        repair = self.settings.storage_root / "repair"
        repair.mkdir()
        alpha_two = repair / "Alpha.mkv"
        alpha_two.write_bytes(b"alpha-version-two")
        second_evidence, _manifest, second_probe = person_fixture(repair, task, rgb=person, alpha=alpha_two)
        with patch("app.integrations.asset_registry.probe_media", side_effect=first_probe):
            first = self.service.registry.register(task, first_evidence, "person", "run-one")
        with patch("app.integrations.asset_registry.probe_media", side_effect=second_probe):
            second = self.service.registry.register(task, second_evidence, "person", "run-two")
        self.assertNotEqual(first["id"], second["id"])
        self.assertNotEqual(first["legacy_reference"]["alpha_sha256"], second["legacy_reference"]["alpha_sha256"])
        self.assertEqual(first["legacy_reference"]["version"], 1)
        self.assertEqual(second["legacy_reference"]["version"], 2)
        self.assertEqual(alpha_one.read_bytes(), b"unit-alpha")


if __name__ == "__main__":
    unittest.main()
