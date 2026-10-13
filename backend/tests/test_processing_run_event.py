"""Isolated SQLite and local child events; no production task or media execution."""

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from app.models import Base, ProcessingRun, Task
from app.integrations.process_runner import ProcessRunner
from app.integrations.collector_service import CollectorService
from app.integrations.production_service import ProductionService
from app.repository import StateRepository, record


SEED = Path(__file__).parents[1] / "app/seed.json"


class ProcessingRunEventTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="factory-run-event-unit-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.database_url = "sqlite:///" + str(self.root / "events.db")
        self.repository = StateRepository(self.database_url, SEED)
        self.addCleanup(self.repository.engine.dispose)
        with Session(self.repository.engine) as session:
            task_ids = list(session.scalars(select(Task.id).order_by(Task.id)))
            self.task_id, self.foreign_task_id = task_ids[:2]
            self.run_id = "unit-process-event-run"
            for identifier, task_id in ((self.run_id, self.task_id), ("unit-other-event-run", self.foreign_task_id)):
                session.add(ProcessingRun(id=identifier, task_id=task_id, stage="compose", provider="IsolatedUnitWorker",
                    status="running", progress=7, logs=["initial isolated log"], created_at=datetime.now(timezone.utc)))
            session.commit()
        self.writes = []
        def capture(connection, cursor, statement, parameters, context, executemany):
            if statement.lstrip().split(None, 1)[0].upper() in {"UPDATE", "INSERT", "DELETE"}:
                self.writes.append((statement, parameters))
        event.listen(self.repository.engine, "before_cursor_execute", capture)
        self.addCleanup(event.remove, self.repository.engine, "before_cursor_execute", capture)
        self.production = ProductionService.__new__(ProductionService)
        self.production.repository = self.repository
        self.production.hub = SimpleNamespace(broadcast=AsyncMock())

    def run_row(self, repository=None):
        with Session((repository or self.repository).engine) as session:
            return record(session.get(ProcessingRun, self.run_id))

    def snapshot(self):
        with self.repository.engine.connect() as connection:
            return {table.name: [dict(row) for row in connection.execute(
                table.select().order_by(*table.primary_key.columns)).mappings()]
                for table in Base.metadata.tables.values()}

    def assert_only_target_run_updated(self, before):
        after = self.snapshot()
        for table in before:
            if table != "processing_runs":
                self.assertEqual(after[table], before[table], table)
        self.assertEqual([row for row in after["processing_runs"] if row["id"] != self.run_id],
            [row for row in before["processing_runs"] if row["id"] != self.run_id])
        self.assertTrue(self.writes)
        for statement, parameters in self.writes:
            self.assertTrue(statement.replace('"', '').upper().startswith("UPDATE PROCESSING_RUNS SET "), statement)
            self.assertIn(self.run_id, parameters)

    async def test_stdout_stderr_messages_append_completely_in_order_without_whole_state_write(self):
        before = self.snapshot()
        messages = [f"isolated stdout {index:03d}" if index % 2 == 0 else f"isolated stderr {index:03d}"
            for index in range(230)]
        messages.append("isolated Unicode 中文 " + "x" * 16000)
        with patch.object(self.repository, "_read", side_effect=AssertionError("whole-state read forbidden")), patch.object(
                self.repository, "_write", side_effect=AssertionError("whole-state write forbidden")):
            for index, message in enumerate(messages):
                self.repository.update_processing_run_event(self.task_id, self.run_id,
                    {"last_heartbeat": "2026-10-08T05:00:00+00:00", "progress": index % 100}, log_message=message)
        row = self.run_row()
        self.assertEqual(row["logs"], ["initial isolated log", *messages])
        self.assertEqual(row["status"], "running")
        self.assertIsNone(row["exit_code"])
        self.assertEqual(len(self.writes), len(messages))
        self.assert_only_target_run_updated(before)

    async def test_heartbeat_updates_without_log_or_broadcast(self):
        before = self.snapshot()
        await self.production._process_event(self.task_id, self.run_id, "compose",
            {"type": "PROCESS_HEARTBEAT", "last_heartbeat": 1, "pid": 123456})
        row = self.run_row()
        self.assertIsNotNone(row["last_heartbeat"])
        self.assertEqual(row["logs"], ["initial isolated log"])
        self.assertEqual(row["progress"], 7)
        self.production.hub.broadcast.assert_not_awaited()
        self.assert_only_target_run_updated(before)

    async def test_collector_metrics_and_progress_update_only_target_task_and_run(self):
        collector = CollectorService.__new__(CollectorService)
        collector.repository, collector.hub = self.repository, self.production.hub
        before = self.snapshot()
        with patch.object(self.repository, "_read", side_effect=AssertionError("whole-state read forbidden")), patch.object(
                self.repository, "_write", side_effect=AssertionError("whole-state write forbidden")):
            await collector._event(self.task_id, self.run_id, {"type": "METRICS_UPDATED", "stage": "METRICS",
                "views": 10000, "likes": 500, "metric_source": "isolated-extension",
                "metric_evidence": "isolated actual event", "metrics_provenance": {"retrieved_at": "unit-only"}})
        after = self.snapshot()
        for table in before:
            if table not in {"processing_runs", "tasks"}:
                self.assertEqual(after[table], before[table], table)
        self.assertEqual([row for row in after["tasks"] if row["id"] != self.task_id],
            [row for row in before["tasks"] if row["id"] != self.task_id])
        task = next(row for row in after["tasks"] if row["id"] == self.task_id)
        self.assertEqual(task["progress"], 30)
        self.assertEqual(task["context"]["live_metrics"]["like_view_ratio"], 5)
        self.assertEqual(task["context"]["live_metrics"]["metrics_provenance"], {"retrieved_at": "unit-only"})
        self.assertEqual(self.run_row()["progress"], 30)
        self.assertEqual(self.run_row()["logs"][-1], "[REAL] METRICS")
        self.assertEqual(self.run_row()["status"], "running")
        for statement, _ in self.writes:
            self.assertRegex(statement.upper(), r"^UPDATE (PROCESSING_RUNS|TASKS) SET ")

    async def test_collector_database_wait_keeps_event_loop_responsive(self):
        collector = CollectorService.__new__(CollectorService)
        collector.repository, collector.hub = self.repository, self.production.hub
        entered, release = threading.Event(), threading.Event()
        persist = self.repository.update_processing_run_event
        def delayed(*args, **kwargs):
            entered.set()
            if not release.wait(2):
                raise AssertionError("event loop did not release isolated database wait")
            return persist(*args, **kwargs)
        with patch.object(self.repository, "update_processing_run_event", side_effect=delayed):
            job = asyncio.create_task(collector._event(self.task_id, self.run_id,
                {"type": "PROCESS_HEARTBEAT", "last_heartbeat": 1700000000}))
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            await asyncio.wait_for(asyncio.sleep(0), .2)
            release.set()
            await job
        self.assertEqual(self.run_row()["logs"], ["initial isolated log"])

    async def test_invalid_task_event_rolls_back_run_and_resource_evidence_is_preserved(self):
        before = self.snapshot()
        with self.assertRaises(ValidationError):
            self.repository.update_processing_run_event(self.task_id, self.run_id, {"progress": 30},
                task_progress=101, log_message="must not commit")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.writes, [])
        self.repository.update_processing_run_event(self.task_id, self.run_id,
            {"resource_info": {"collector_environment": {"status": "READY"}}})
        self.repository.update_processing_run_event(self.task_id, self.run_id, {"resource_info": {"max_rss": 123}})
        self.assertEqual(self.run_row()["resource_info"], {"collector_environment": {"status": "READY"}, "max_rss": 123})

    async def test_started_and_finished_metadata_persist_after_reopening_without_terminal_success(self):
        before = self.snapshot()
        command = [sys.executable, "-B", "isolated-unit-worker.py"]
        stdout_path, stderr_path = str(self.root / "stdout.log"), str(self.root / "stderr.log")
        with patch.object(self.repository, "_read", side_effect=AssertionError("whole-state read forbidden")), patch.object(
                self.repository, "_write", side_effect=AssertionError("whole-state write forbidden")):
            await self.production._process_event(self.task_id, self.run_id, "compose", {"type": "PROCESS_STARTED",
                "command": command, "cwd": str(self.root), "pid": 123456,
                "stdout_log_path": stdout_path, "stderr_log_path": stderr_path})
            await self.production._process_event(self.task_id, self.run_id, "compose", {"type": "process_finished",
                "exit_code": -9, "termination_signal": 9, "timed_out": True, "resource_info": {"unit_fixture": True, "max_rss": 42}})
        reopened = StateRepository(self.database_url, SEED)
        self.addCleanup(reopened.engine.dispose)
        row = self.run_row(reopened)
        self.assertEqual(row["pid"], 123456)
        self.assertEqual(row["command"], command)
        self.assertEqual(row["cwd"], str(self.root))
        self.assertEqual(row["stdout_log_path"], stdout_path)
        self.assertEqual(row["stderr_log_path"], stderr_path)
        self.assertIsNotNone(row["last_heartbeat"])
        self.assertEqual(row["termination_signal"], 9)
        self.assertTrue(row["timed_out"])
        self.assertEqual(row["resource_info"], {"unit_fixture": True, "max_rss": 42})
        self.assertEqual(row["logs"], ["initial isolated log", "PROCESS_STARTED", "process_finished"])
        self.assertEqual(row["status"], "running")
        self.assertIsNone(row["exit_code"])
        self.assertIsNone(row["finished_at"])
        self.assert_only_target_run_updated(before)

    async def test_invalid_run_view_values_reject_atomically_including_log_append(self):
        before = self.snapshot()
        for invalid in ({"progress": -1}, {"progress": 101}, {"progress": float("nan")}, {"pid": 0},
                {"command": "not a command list"}, {"resource_info": ["not a resource object"]}):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValidationError):
                    self.repository.update_processing_run_event(self.task_id, self.run_id,
                        {"last_heartbeat": "2026-10-08T05:00:00+00:00", **invalid}, log_message="must not survive validation")
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(self.writes, [])

    async def test_foreign_task_and_missing_run_are_rejected_before_any_mutation(self):
        before = self.snapshot()
        for task_id, run_id, code in ((self.foreign_task_id, self.run_id, "PROCESSING_RUN_TASK_MISMATCH"),
                (self.task_id, "unit-missing-run", "PROCESSING_RUN_NOT_FOUND")):
            with self.subTest(code=code):
                with self.assertRaisesRegex(ValueError, code):
                    self.repository.update_processing_run_event(task_id, run_id, {"progress": 42}, log_message="not saved")
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(self.writes, [])

    async def test_lifecycle_identity_and_logs_fields_cannot_be_changed_through_event_values(self):
        before = self.snapshot()
        for forbidden, value in (("status", "success"), ("task_id", self.foreign_task_id), ("id", "unit-renamed"),
                ("provider", "changed-provider"), ("stage", "review"), ("logs", ["overwrite"]),
                ("exit_code", 0), ("finished_at", "2026-10-08T05:00:00+00:00")):
            with self.subTest(forbidden=forbidden):
                with self.assertRaisesRegex(ValueError, "PROCESSING_RUN_EVENT_FIELD_INVALID"):
                    self.repository.update_processing_run_event(self.task_id, self.run_id,
                        {"progress": 42, forbidden: value}, log_message="must not be partially appended")
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(self.writes, [])

    async def test_concurrent_row_events_preserve_all_messages(self):
        before = self.snapshot()
        messages = [f"isolated concurrent line {index}" for index in range(32)]
        await asyncio.gather(*[asyncio.to_thread(self.repository.update_processing_run_event,
            self.task_id, self.run_id, {"progress": index}, log_message=message)
            for index, message in enumerate(messages)])
        logs = self.run_row()["logs"]
        self.assertEqual(logs[0], "initial isolated log")
        self.assertCountEqual(logs[1:], messages)
        self.assertEqual(len(logs), len(messages) + 1)
        self.assert_only_target_run_updated(before)

    async def test_real_child_stdout_stderr_and_exit_events_use_single_row_callback(self):
        before = self.snapshot()
        loop = asyncio.get_running_loop()
        delivered = []
        def on_event(value):
            if value["type"] != "PROCESS_HEARTBEAT":
                delivered.append(value.get("text") or value.get("message") or value["type"])
            asyncio.run_coroutine_threadsafe(
                self.production._process_event(self.task_id, self.run_id, "compose", value), loop).result(timeout=5)
        progress = {"type": "unit_progress", "message": "isolated actual progress", "progress": 71.25}
        child = "import json, sys\nfor index in range(120): print(f'isolated stdout {index:03d}', flush=True)\n"
        child += "for index in range(12): print(f'isolated stderr {index:03d}', file=sys.stderr, flush=True)\n"
        child += "print('FACTORY_EVENT ' + json.dumps(" + repr(progress) + "), flush=True)\nsys.exit(7)"
        command = [sys.executable, "-B", "-u", "-c", child]
        with patch.object(self.repository, "_read", side_effect=AssertionError("whole-state read forbidden")), patch.object(
                self.repository, "_write", side_effect=AssertionError("whole-state write forbidden")):
            process = await asyncio.to_thread(ProcessRunner().run, command, cwd=self.root,
                log_dir=self.root / "logs", timeout_s=20, on_event=on_event)
        self.assertEqual(process.exit_code, 7)
        self.assertGreater(process.pid, 0)
        self.assertEqual(Path(process.stdout_log_path).read_text(), process.stdout)
        self.assertEqual(Path(process.stderr_log_path).read_text(), process.stderr)
        self.assertEqual(process.stdout.splitlines(), [*[f"isolated stdout {index:03d}" for index in range(120)],
            "FACTORY_EVENT " + json.dumps(progress)])
        self.assertEqual(process.stderr.splitlines(), [f"isolated stderr {index:03d}" for index in range(12)])
        row = self.run_row()
        self.assertEqual(row["logs"], ["initial isolated log", *delivered])
        self.assertEqual([line for line in row["logs"] if line.startswith("isolated stdout ")],
            [f"isolated stdout {index:03d}" for index in range(120)])
        self.assertEqual([line for line in row["logs"] if line.startswith("isolated stderr ")],
            [f"isolated stderr {index:03d}" for index in range(12)])
        self.assertEqual(row["pid"], process.pid)
        self.assertEqual(row["progress"], 71.25)
        self.assertEqual(row["command"], command)
        self.assertEqual(row["stdout_log_path"], process.stdout_log_path)
        self.assertEqual(row["stderr_log_path"], process.stderr_log_path)
        self.assertTrue(row["resource_info"])
        self.assertEqual(row["status"], "running")
        self.assertIsNone(row["exit_code"])
        self.assertIsNone(row["finished_at"])
        self.assert_only_target_run_updated(before)


if __name__ == "__main__":
    unittest.main()
