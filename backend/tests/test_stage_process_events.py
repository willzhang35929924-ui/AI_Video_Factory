"""Real short-lived children with isolated event persistence, never production media."""

import asyncio
import json
import sys
import threading
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import AsyncMock

from app.integrations.process_runner import ProcessRunner
from test_real_review import ReviewFixture


class StageProcessEventTests(ReviewFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.prepare()
        queued = await self.production.start(["unit-task"], retry=True, stage="compose")
        self.batch_id = queued["batch_id"]
        self.production.cancels = {self.batch_id: threading.Event()}
        self.production._checkpoint = AsyncMock()

    async def stage(self, invoke):
        return await self.production._stage(self.batch_id, "unit-task", "compose", "LegacyComposeAdapter", invoke)

    async def test_slow_event_persistence_does_not_block_or_kill_healthy_child(self):
        entered, release = asyncio.Event(), asyncio.Event()
        finished = threading.Event()
        persisted, captured = [], []
        async def persist(task_id, run_id, stage, event):
            entered.set()
            await release.wait()
            persisted.append(event["type"])
        self.production._process_event = persist
        async def invoke(run_id, directory, callback):
            result = await asyncio.to_thread(ProcessRunner().run,
                [sys.executable, "-c", "print('FACTORY_EVENT '+__import__('json').dumps({'type':'CHUNK_READY'}))"],
                cwd=self.production.settings.storage_root, log_dir=directory, on_event=callback)
            captured.append(result)
            finished.set()
            return {"status": "READY", "_process": asdict(result)}
        job = asyncio.create_task(self.stage(invoke))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            child_finished = await asyncio.to_thread(finished.wait, 2)
            self.assertTrue(child_finished, "event persistence must not hold the child's ProcessRunner")
            self.assertFalse(job.done(), "the Run must wait for its ordered event persistence before finalizing")
        finally:
            release.set()
            outcome = await asyncio.wait_for(job, 3)
        self.assertEqual(outcome["status"], "READY")
        self.assertEqual(captured[0].exit_code, 0)
        self.assertEqual(persisted, ["PROCESS_STARTED", "CHUNK_READY", "process_finished"])
        run = self.repository.state()["runs"][-1]
        self.assertEqual(run["pid"], captured[0].pid)
        self.assertEqual(run["exit_code"], 0)
        self.assertEqual(run["status"], "success")

    async def test_persistence_error_does_not_erase_actual_process_exit(self):
        self.production._process_event = AsyncMock(side_effect=OSError("isolated event database failure"))
        captured = []
        async def invoke(run_id, directory, callback):
            result = await asyncio.to_thread(ProcessRunner().run, [sys.executable, "-c", "print('complete')"],
                cwd=self.production.settings.storage_root, log_dir=directory, on_event=callback)
            captured.append(result)
            return {"status": "READY", "_process": asdict(result)}
        outcome = await self.stage(invoke)
        self.assertEqual(outcome["error_code"], "PROCESS_EVENT_PERSISTENCE_FAILED")
        self.assertFalse(outcome["retryable"])
        run = self.repository.state()["runs"][-1]
        self.assertEqual(run["exit_code"], 0)
        self.assertGreater(run["pid"], 0)
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["error_message"], "OSError")
        records = [json.loads(line) for line in Path(captured[0].log_path).read_text().splitlines()]
        self.assertEqual(records[-1]["exit_code"], 0)

    async def test_adapter_exception_still_preserves_terminal_callback_evidence(self):
        self.production._process_event = AsyncMock()
        async def invoke(run_id, directory, callback):
            await asyncio.to_thread(ProcessRunner().run, [sys.executable, "-c", "print('complete')"],
                cwd=self.production.settings.storage_root, log_dir=directory, on_event=callback)
            raise ValueError("isolated receipt validation failure")
        outcome = await self.stage(invoke)
        self.assertEqual(outcome["error_code"], "STAGE_RESULT_INVALID")
        run = self.repository.state()["runs"][-1]
        self.assertEqual(run["exit_code"], 0)
        self.assertGreater(run["pid"], 0)
        self.assertEqual(run["status"], "failed")

    async def assert_responsive_finalization(self, *, adapter_error=False):
        entered, release = threading.Event(), threading.Event()
        next_stage = asyncio.Event()
        record = self.production._record_result
        loop_thread, recording_threads = threading.get_ident(), []
        self.production._process_event = AsyncMock()
        def slow_record(*args):
            recording_threads.append(threading.get_ident())
            entered.set()
            release.wait(2)
            return record(*args)
        self.production._record_result = slow_record
        async def invoke(run_id, directory, callback):
            result = await asyncio.to_thread(ProcessRunner().run, [sys.executable, "-c", "print('complete')"],
                cwd=self.production.settings.storage_root, log_dir=directory, on_event=callback)
            if adapter_error:
                raise ValueError("isolated invalid receipt")
            return {"status": "READY", "_process": asdict(result)}
        async def pipeline():
            result = await self.stage(invoke)
            next_stage.set()
            return result
        job = asyncio.create_task(pipeline())
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            self.assertFalse(job.done(), "finalization must not block the event loop or report completion early")
            self.assertFalse(next_stage.is_set(), "the next stage must wait until asset and Run persistence finishes")
            self.assertEqual(self.repository.state()["runs"][-1]["status"], "running")
            await asyncio.sleep(.01)
            self.assertFalse(job.done())
        finally:
            release.set()
            outcome = await asyncio.wait_for(job, 3)
        self.assertNotIn(loop_thread, recording_threads)
        run = self.repository.state()["runs"][-1]
        self.assertEqual(run["status"], "failed" if adapter_error else "success")
        self.assertEqual(run["exit_code"], 0)
        self.assertGreater(run["pid"], 0)
        self.assertTrue(next_stage.is_set())
        if adapter_error:
            self.assertEqual(outcome["error_code"], "STAGE_RESULT_INVALID")
            self.assertEqual(run["error_message"], "ValueError")
        else:
            self.assertEqual(outcome["status"], "READY")

    async def test_success_finalization_keeps_event_loop_responsive_and_waits_for_persistence(self):
        await self.assert_responsive_finalization()

    async def test_failed_finalization_keeps_event_loop_responsive_and_preserves_error(self):
        await self.assert_responsive_finalization(adapter_error=True)

    async def test_recording_error_preserves_existing_failure_path_and_process_exit(self):
        self.production._process_event = AsyncMock()
        self.production.registry.failure = OSError("isolated asset validation failure")
        async def invoke(run_id, directory, callback):
            result = await asyncio.to_thread(ProcessRunner().run, [sys.executable, "-c", "print('complete')"],
                cwd=self.production.settings.storage_root, log_dir=directory, on_event=callback)
            return {"status": "READY", "_process": asdict(result), "assets": [{
                "asset_type": "COMPOSITION_VIDEO", "path": self.original_asset["storage_path"], "sha256": "unit-hash"}]}
        outcome = await self.stage(invoke)
        run = self.repository.state()["runs"][-1]
        self.assertEqual(outcome["error_code"], "STAGE_RESULT_INVALID")
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["error_message"], "OSError")
        self.assertEqual(run["exit_code"], 0)
        self.assertGreater(run["pid"], 0)
        self.assertFalse(self.production.registry.registered)


if __name__ == "__main__":
    unittest.main()
