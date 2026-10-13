"""Real isolated child receipts and logs; no model, media or production database."""

import asyncio
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.integrations.matte_repair import MatteRepairService, RepairRequest
from app.integrations.process_runner import ProcessRunner
from test_real_review import MemoryRepository


class MatteRepairEventTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.task = {"id": "isolated-task", "project_id": "isolated-project", "execution_mode": "integration",
                     "context": {"production": {}}, "status": "WAITING_REVIEW", "current_stage": "review"}
        self.person = {"id": "person-v1", "task_id": self.task["id"], "type": "person", "sha256": "isolated-rgb",
                       "legacy_reference": {"version": 1, "alpha_sha256": "isolated-alpha"}}
        self.repository = MemoryRepository({"tasks": [self.task], "assets": [self.person], "runs": [], "reviews": [], "audits": []})
        self.production = SimpleNamespace(repository=self.repository, settings=SimpleNamespace(storage_root=self.root,
            quality_python=sys.executable), event=AsyncMock(), start_lock=asyncio.Lock(), consume_deferred_continue=AsyncMock())
        self.service = MatteRepairService(self.production)
        self.service.pair = lambda *args: (self.task, self.person, self.root / "RGB.mkv", self.root / "Alpha.mkv",
            {"media": {"width": 100, "height": 200, "frame_count": 400}, "source_sha256": "isolated-source"})
        self.record, _ = await self.service.prepare(self.task["id"], RepairRequest.model_validate({
            "person_asset_id": self.person["id"], "start_frame": 0, "end_frame": 400,
            "idempotency_key": "isolated-log-test", "keyframes": [{"frame_index": 200, "object_id": "bag_1", "box": [.1, .1, .5, .6]}]}))
        self.service.cancels[self.record["id"]] = threading.Event()

    def runner(self, script, captured, finished):
        def run(command, **values):
            result_path = Path(values["cwd"]) / "result.json"
            result = ProcessRunner().run([sys.executable, "-c", script, str(result_path)], **values)
            captured.append(result)
            finished.set()
            return result
        return SimpleNamespace(run=run)

    async def test_raw_logs_and_slow_observer_do_not_hold_child_or_publish_early_success(self):
        entered, release = asyncio.Event(), asyncio.Event()
        captured, events = [], []
        finished = threading.Event()
        async def observe(task_id, stage, message, status, run_id, **values):
            if status == "RUNNING":
                entered.set()
                await release.wait()
                events.append(message)
        self.production.event = observe
        tracked = []
        mutate = self.repository.mutate
        def record_changes(change):
            result = mutate(change)
            tracking = self.service.records(self.task["id"])[0].get("tracking_progress")
            if tracking and (not tracked or tracked[-1] != tracking["frame_index"]):
                tracked.append(tracking["frame_index"])
            return result
        self.repository.mutate = record_changes
        script = """
import json, sys
from pathlib import Path
for frame in range(200):
    print('tqdm raw frame '+str(frame), file=sys.stderr)
    print('FACTORY_EVENT '+json.dumps({'type':'STAGE_PROGRESS','tracking_stage':'PROPAGATING',
        'frame_index':frame,'total':400,'direction':'forward'}))
for frame in range(399,199,-1):
    print('tqdm raw frame '+str(frame), file=sys.stderr)
    print('FACTORY_EVENT '+json.dumps({'type':'STAGE_PROGRESS','tracking_stage':'PROPAGATING',
        'frame_index':frame,'total':400,'direction':'backward'}))
print('FACTORY_EVENT '+json.dumps({'message':'real isolated child finished','progress':100}))
Path(sys.argv[1]).write_text(json.dumps({'status':'PREVIEW_READY','isolated_receipt':True}))
"""
        with patch("app.integrations.matte_repair.ProcessRunner", return_value=self.runner(script, captured, finished)):
            job = asyncio.create_task(self.service.run(self.task["id"], self.record["id"]))
            try:
                await asyncio.wait_for(entered.wait(), 5)
                self.assertTrue(await asyncio.to_thread(finished.wait, 5), "slow persistence must not block ProcessRunner")
                self.assertTrue((Path(self.record["directory"]) / "result.json").is_file())
                self.assertFalse(job.done(), "success must wait for meaningful events to persist")
                self.assertEqual(self.service.records(self.task["id"])[0]["status"], "PROPAGATING")
            finally:
                release.set()
                await asyncio.wait_for(job, 5)
        process = captured[0]
        run = self.repository.state()["runs"][0]
        record = self.service.records(self.task["id"])[0]
        self.assertEqual(process.exit_code, 0)
        self.assertEqual(run["pid"], process.pid)
        self.assertEqual(run["exit_code"], 0)
        self.assertEqual(run["status"], "success")
        self.assertEqual(record["status"], "PREVIEW_READY")
        self.assertEqual(record["tracking_progress"]["frame_index"], 200)
        self.assertEqual(record["tracking_progress"]["direction"], "backward")
        self.assertEqual(record["tracking_progress"]["interval_end"], 400)
        self.assertEqual(tracked, [0, 199, 399, 200])
        self.assertEqual(len(Path(process.stderr_log_path).read_text().splitlines()), 400)
        self.assertEqual(run["logs"][-1], "real isolated child finished")
        self.assertFalse(any("tqdm" in message for message in events + run["logs"]))
        self.assertLessEqual(len(events), 7, "rapid tracking frames must be coalesced, not each written to the database")
        journal = [json.loads(line) for line in Path(process.log_path).read_text().splitlines()]
        self.assertEqual(journal[0]["type"], "PROCESS_STARTED")
        self.assertEqual(journal[-1]["type"], "process_finished")
        self.assertEqual(journal[-1]["exit_code"], 0)
        self.assertEqual(sum(row.get("type") == "output" for row in journal), 400)
        self.production.consume_deferred_continue.assert_awaited_once_with(self.task["id"])

    async def test_slow_repository_does_not_block_subprocess_log_reader(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        captured = []
        mutate = self.repository.mutate
        def slow_mutate(change):
            result = mutate(change)
            if self.repository.state()["runs"][0].get("pid") and not entered.is_set():
                entered.set()
                if not release.wait(10):
                    raise TimeoutError("isolated test did not release observer")
            return result
        self.repository.mutate = slow_mutate
        script = """
import json, sys
from pathlib import Path
print('raw stderr is retained', file=sys.stderr)
Path(sys.argv[1]).write_text(json.dumps({'status':'PREVIEW_READY','isolated_receipt':True}))
"""
        with patch("app.integrations.matte_repair.ProcessRunner", return_value=self.runner(script, captured, finished)):
            job = asyncio.create_task(self.service.run(self.task["id"], self.record["id"]))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 5))
                self.assertTrue(await asyncio.to_thread(finished.wait, 5), "repository backpressure must not hold log capture")
                self.assertFalse(job.done())
            finally:
                release.set()
                await asyncio.wait_for(job, 5)
        self.assertEqual(captured[0].exit_code, 0)
        self.assertEqual(self.repository.state()["runs"][0]["status"], "success")
        self.assertEqual(Path(captured[0].stderr_log_path).read_text(), "raw stderr is retained\n")

    async def test_observer_failure_keeps_healthy_child_receipt_pid_and_exit(self):
        captured, finished = [], threading.Event()
        async def observe(task_id, stage, message, status, run_id, **values):
            if status == "RUNNING":
                raise OSError("isolated observer unavailable")
        self.production.event = observe
        script = """
import json, sys, time
from pathlib import Path
print('FACTORY_EVENT '+json.dumps({'message':'computing','progress':15}), flush=True)
time.sleep(.1)
Path(sys.argv[1]).write_text(json.dumps({'status':'PREVIEW_READY','isolated_receipt':True}))
"""
        with patch("app.integrations.matte_repair.ProcessRunner", return_value=self.runner(script, captured, finished)):
            await self.service.run(self.task["id"], self.record["id"])
        self.assertTrue(finished.is_set())
        self.assertEqual(captured[0].exit_code, 0)
        self.assertTrue((Path(self.record["directory"]) / "result.json").is_file())
        run = self.repository.state()["runs"][0]
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["error_code"], "REPAIR_EVENT_PERSISTENCE_FAILED")
        self.assertEqual(run["pid"], captured[0].pid)
        self.assertEqual(run["exit_code"], 0)
        self.assertIsNone(run["termination_signal"])
        self.assertEqual(run["stderr_log_path"], captured[0].stderr_log_path)
        self.assertEqual(self.service.records(self.task["id"])[0]["status"], "FAILED")
        self.assertEqual(self.repository.state()["assets"], [self.person])

    async def test_receipt_error_keeps_terminal_process_evidence(self):
        captured, finished = [], threading.Event()
        script = """
import json, sys
from pathlib import Path
Path(sys.argv[1]).write_text(json.dumps({'status':'FAILED','error_code':'ISOLATED_RECEIPT_INVALID'}))
"""
        with patch("app.integrations.matte_repair.ProcessRunner", return_value=self.runner(script, captured, finished)):
            await self.service.run(self.task["id"], self.record["id"])
        run = self.repository.state()["runs"][0]
        self.assertEqual(run["error_code"], "ISOLATED_RECEIPT_INVALID")
        self.assertEqual(run["pid"], captured[0].pid)
        self.assertEqual(run["exit_code"], 0)
        self.assertEqual(run["status"], "failed")
        self.assertGreaterEqual(run["duration_ms"], 0)

    async def test_prelaunch_cancel_does_not_register_fake_pid(self):
        captured, finished = [], threading.Event()
        self.service.cancels[self.record["id"]].set()
        with patch("app.integrations.matte_repair.ProcessRunner", return_value=self.runner("raise RuntimeError('must not execute')", captured, finished)):
            await self.service.run(self.task["id"], self.record["id"])
        self.assertEqual(captured[0].pid, 0)
        run = self.repository.state()["runs"][0]
        self.assertNotIn("pid", run)
        self.assertEqual(run["error_code"], "PROCESS_CANCELLED")
        self.assertEqual(run["exit_code"], 130)
        self.assertFalse((Path(self.record["directory"]) / "result.json").exists())


if __name__ == "__main__":
    unittest.main()
