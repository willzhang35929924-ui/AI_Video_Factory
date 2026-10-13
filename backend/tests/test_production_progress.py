import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.integrations.production_service import ProductionService
from app.repository import StateRepository


class ProductionProgressTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.production = object.__new__(ProductionService)
        self.production.hub = SimpleNamespace(broadcast=AsyncMock())
        self.production.repository = StateRepository("sqlite:///" + str(Path(directory.name) / "progress.db"),
            Path(__file__).parents[1] / "app/seed.json")
        self.addCleanup(self.production.repository.engine.dispose)
        self.production.repository.mutate(lambda state: state["runs"][0].update(progress=4, logs=[]))

    async def test_event_preserves_actual_progress_task_and_run_identity(self):
        await self.production.event("isolated-task", "compose", "actual frame progress", "RUNNING", "isolated-run", progress=23.75)
        event = self.production.hub.broadcast.await_args.args[0]
        self.assertEqual(event.task_id, "isolated-task")
        self.assertEqual(event.run_id, "isolated-run")
        self.assertEqual(event.progress, 23.75)

    async def test_callback_saves_and_broadcasts_worker_progress_including_zero(self):
        for progress in (0, 57.25):
            await self.production._process_event("tsk-1001", "run-demo", "compose", {"message": "verified worker progress", "progress": progress})
            self.assertEqual(self.production.repository.state()["runs"][0]["progress"], progress)
            self.assertEqual(self.production.hub.broadcast.await_args.args[0].progress, progress)

    async def test_missing_progress_is_unknown_not_invented_from_queue_or_status(self):
        await self.production._process_event("tsk-1001", "run-demo", "person", {"message": "queued status, no progress provided"})
        self.assertIsNone(self.production.hub.broadcast.await_args.args[0].progress)
        self.assertEqual(self.production.repository.state()["runs"][0]["progress"], 4)


if __name__ == "__main__":
    unittest.main()
