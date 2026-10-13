import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from app.integrations.comfy_asset_adapter import ComfyAssetAdapter
from app.integrations.matte_repair import MatteRepairService
from app.integrations.process_runner import ProcessResult
from test_real_review import MemoryRepository, UnitRegistry


class TechnicalCompositionCacheTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        task = {"id": "isolated-task", "status": "CREATED", "collector_result": {},
                "context": {"technical_validation": {"current_person_asset_id": "person-v1"}}}
        person = {"id": "person-v1", "task_id": task["id"], "type": "person", "sha256": "rgb-v1", "registry_status": "registered",
                  "storage_path": str(self.root / "original-rgb.mkv"), "legacy_reference": {"execution_scope": "TECHNICAL_VALIDATION",
                      "alpha_sha256": "alpha-v1", "technical_binding": {"video_id": "isolated-video"}}}
        background = {"id": "background-v1", "task_id": task["id"], "type": "background", "sha256": "background-sha", "registry_status": "registered",
                      "storage_path": str(self.root / "existing-background.png"), "legacy_reference": {"execution_scope": "TECHNICAL_VALIDATION"}}
        existing = {"id": "old-compose", "task_id": task["id"], "type": "compose", "registry_status": "registered",
                    "storage_path": str(self.root / "old-run/Composite.mp4"),
                    "legacy_reference": {"person_asset_id": person["id"], "background_asset_id": background["id"]}}
        self.repository = MemoryRepository({"tasks": [task], "assets": [person, background, existing], "runs": [], "reviews": [], "audits": []})
        context = {"task_id": task["id"], "project_id": "isolated-project", "country": "MX", "keyword": "bag", "product_id": "isolated-product",
                   "context_id": "isolated-context", "product_images": [str(self.root / "product.png")], "source_document": "explicit-manual", "source_row": 0}
        self.production = SimpleNamespace(repository=self.repository, settings=SimpleNamespace(storage_root=self.root), start_lock=asyncio.Lock(),
            registry=UnitRegistry(self.repository), collector=SimpleNamespace(context=lambda _: copy.deepcopy(context)), save_snapshot=Mock(), event=AsyncMock())
        self.service = MatteRepairService(self.production)
        self.service.assess_technical_person = AsyncMock()
        def pair(task_id, person_id):
            state = self.repository.state()
            parent = next(row for row in state["assets"] if row["id"] == person_id)
            return state["tasks"][0], parent, Path(parent["storage_path"]), self.root / "alpha.mkv", {"source_path": str(self.root / "source.mp4")}
        self.service.pair = pair

    async def test_completed_pair_uses_stable_cache_root_and_keeps_old_media(self):
        old_media = self.root / "old-run/Composite.mp4"
        old_media.parent.mkdir()
        old_media.write_bytes(b"isolated history marker, not production media")
        first, payload, directory = await self.service.technical_compose("isolated-task")
        self.repository.mutate(lambda state: state["runs"][0].update(status="success"))
        second, again, same_directory = await self.service.technical_compose("isolated-task")
        self.assertEqual(directory, self.root / "technical_validation/registered/isolated-task")
        self.assertEqual(directory, same_directory)
        self.assertNotEqual(first["run_id"], second["run_id"])
        self.assertEqual(payload["person_asset"], again["person_asset"])
        self.assertEqual(old_media.read_bytes(), b"isolated history marker, not production media")
        self.assertEqual(len(self.repository.state()["assets"]), 3)
        self.assertEqual(self.repository.state()["tasks"][0]["status"], "CREATED")

    async def test_changed_parent_keeps_stable_root_but_new_input_identity(self):
        _, payload, directory = await self.service.technical_compose("isolated-task")
        def change(state):
            state["runs"][0]["status"] = "success"
            parent = copy.deepcopy(state["assets"][0])
            parent.update(id="person-v2", sha256="rgb-v2")
            parent["legacy_reference"]["alpha_sha256"] = "alpha-v2"
            state["assets"].append(parent)
            state["tasks"][0]["context"]["technical_validation"]["current_person_asset_id"] = parent["id"]
        self.repository.mutate(change)
        _, changed, changed_directory = await self.service.technical_compose("isolated-task")
        self.assertEqual(directory, changed_directory)
        self.assertNotEqual(payload["person_asset"]["legacy_reference"]["alpha_sha256"], changed["person_asset"]["legacy_reference"]["alpha_sha256"])
        self.assertEqual(self.repository.state()["assets"][0]["sha256"], "rgb-v1")

    async def test_remux_parent_only_uses_same_registered_person_and_background(self):
        def bind(state):
            state["assets"][2]["legacy_reference"]["execution_scope"] = "TECHNICAL_VALIDATION"
        self.repository.mutate(bind)
        first, payload, _ = await self.service.technical_compose("isolated-task")
        self.assertEqual(payload["composition_asset"]["id"], "old-compose")
        def change(state):
            state["runs"][0]["status"] = "success"
            state["assets"][2]["legacy_reference"]["person_asset_id"] = "unrelated-person-version"
        self.repository.mutate(change)
        _, payload, _ = await self.service.technical_compose("isolated-task")
        self.assertIsNone(payload["composition_asset"])
        self.assertNotEqual(first["run_id"], self.repository.state()["runs"][-1]["id"])

    async def test_cache_reuses_asset_and_does_not_reopen_approved_review(self):
        queued, payload, directory = await self.service.technical_compose("isolated-task")
        fingerprint = "a" * 64
        evidence = {"path": str(directory / "compositions" / fingerprint / "Composite.mp4"), "sha256": "isolated-output-sha",
                    "legacy_reference": {"input_fingerprint": fingerprint}}
        self.production.providers = {"compose": SimpleNamespace(execute=AsyncMock(return_value={"status": "READY", "assets": [evidence],
            "metadata": {"cache_hit": False, "workflow_snapshot": {"workflow_snapshot_id": "original-proof"}}}))}
        await self.service.run_technical_compose("isolated-task", queued["run_id"], payload, directory)
        asset = self.repository.state()["assets"][-1]
        self.assertEqual(asset["legacy_reference"]["factory_fingerprint"], "technical-compose:" + fingerprint)
        self.repository.mutate(lambda state: state["reviews"][0].update(status="approved"))
        again, payload, directory = await self.service.technical_compose("isolated-task")
        original = copy.deepcopy(asset)
        self.production.registry.register = Mock(return_value=original)
        self.production.providers["compose"].execute.return_value["metadata"].update(cache_hit=True)
        await self.service.run_technical_compose("isolated-task", again["run_id"], payload, directory)
        state = self.repository.state()
        self.assertEqual(len(state["reviews"]), 1)
        self.assertEqual(state["reviews"][0]["status"], "approved")
        self.assertEqual(state["assets"][-1], original)
        self.assertTrue(any("未提交 ComfyUI" in line for line in state["runs"][-1]["logs"]))
        self.assertEqual(state["tasks"][0]["collector_result"], {})
        self.assertEqual(self.production.event.await_args.kwargs["progress"], 100)

    async def test_missing_actual_input_fingerprint_is_not_registered(self):
        queued, payload, directory = await self.service.technical_compose("isolated-task")
        evidence = {"path": str(directory / "unverified.mp4"), "sha256": "isolated-output-sha", "legacy_reference": {}}
        self.production.providers = {"compose": SimpleNamespace(execute=AsyncMock(return_value={"status": "READY", "assets": [evidence], "metadata": {}}))}
        await self.service.run_technical_compose("isolated-task", queued["run_id"], payload, directory)
        self.assertEqual(self.repository.state()["runs"][-1]["status"], "failed")
        self.assertEqual(self.repository.state()["runs"][-1]["error_code"], "TECHNICAL_COMPOSE_INPUT_FINGERPRINT_REQUIRED")
        self.assertFalse(self.production.registry.registered)

    async def test_shared_cache_root_keeps_independent_context_result_journals(self):
        commands = []
        class Runner:
            def run(runner, command, **kwargs):
                commands.append(command)
                request = Path(command[command.index("--context") + 1])
                context = json.loads(request.read_text())
                response = Path(command[command.index("--result") + 1])
                response.write_text(json.dumps({"task_id": context["task_id"], "context_id": context["context_id"], "stage": "compose",
                    "provider": "isolated journal fixture", "status": "READY"}))
                return ProcessResult(0, 0, "", "", "", 0)
        adapter = ComfyAssetAdapter(self.root, storage_root=self.root, runner=Runner(), python_path=self.root / "unused-python")
        first, payload, directory = await self.service.technical_compose("isolated-task")
        await adapter.execute("compose", payload, directory)
        self.repository.mutate(lambda state: state["runs"][0].update(status="success"))
        second, payload, directory = await self.service.technical_compose("isolated-task")
        await adapter.execute("compose", payload, directory)
        self.assertEqual([command[command.index("--directory") + 1] for command in commands], [str(directory), str(directory)])
        for run in (first, second):
            journal = directory / "runs" / run["run_id"]
            self.assertEqual(json.loads((journal / "context.json").read_text())["run_id"], run["run_id"])
            self.assertTrue((journal / "result.json").is_file())


if __name__ == "__main__":
    unittest.main()
