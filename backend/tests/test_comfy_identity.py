import argparse
import ast
import copy
import io
import json
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from app.integrations.asset_registry import file_hash
from app.integrations.comfy_asset_adapter import ComfyAssetAdapter
from app.integrations.process_runner import ProcessResult
from app.integrations.legacy.video_worker import base_result
from test_real_review import ReviewFixture


class LocalIdentityRunner:
    """Execute only the worker's local identity branch against isolated fixture files."""
    def __init__(self, config):
        self.config = config
        self.calls = []
        worker = Path(__file__).parents[1] / "app/integrations/legacy/comfy_asset_worker.py"
        definitions = [node for node in ast.parse(worker.read_text()).body if isinstance(node, ast.FunctionDef)
                       and node.name in {"workflow", "implementation_fingerprint", "main"}]
        self.namespace = {"argparse": argparse, "Path": Path, "json": json, "subprocess": subprocess,
            "digest": file_hash, "load_legacy": lambda root: copy.deepcopy(self.config), "base_result": base_result,
            "safe": lambda value: value, "write_json": lambda path, value: path.write_text(json.dumps(value))}
        exec(compile(ast.Module(body=definitions, type_ignores=[]), str(worker), "exec"), self.namespace)

    def run(self, command, **options):
        self.calls.append(command)
        self.assert_local(command)
        output = io.StringIO()
        with patch.object(sys, "argv", command[2:]), redirect_stdout(output):
            result = self.namespace["main"]()
        return ProcessResult(0, result, output.getvalue(), "", "", 0)

    def assert_local(self, command):
        if command[command.index("--stage") + 1] != "identity":
            raise AssertionError("unit fixture cannot call readiness or media execution")


class ComfyIdentityTests(ReviewFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.prepare("person")
        self.root = self.production.settings.storage_root
        self.comfy = self.root / "comfy"
        self.config = {"comfy_input_dir": str(self.comfy / "input")}
        self.node = self.comfy / "custom_nodes/video_auto_matting/__init__.py"
        self.model = self.comfy / "models/RMBG/BiRefNet/unit.safetensors"
        for path in (self.node, self.model, self.comfy / "comfy_extras/nodes_mask.py"):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"isolated identity fixture")
        self.cutout = self.production.settings.factory_comfy_cutout_workflow
        self.compose = self.production.settings.factory_comfy_compose_workflow
        self.cutout.write_text(json.dumps({"11": {"class_type": "BiRefNetRMBG", "inputs": {"model": "unit"}},
            "14": {"class_type": "FactoryLoadCFRContextV1", "inputs": {}},
            "38": {"class_type": "VideoAutoMatteRepair", "inputs": {}},
            "save": {"class_type": "VHS_VideoCombine", "inputs": {}}}))
        self.compose.write_text(json.dumps({"foreground": {"class_type": "FactoryLoadForegroundV1", "inputs": {}},
            "repeat": {"class_type": "RepeatImageBatch", "inputs": {}},
            "blend": {"class_type": "ImageCompositeMasked", "inputs": {}}}))
        (self.root / "config.yaml").write_text("isolated: current")
        self.runner = LocalIdentityRunner(self.config)
        adapter = ComfyAssetAdapter(self.root, runner=self.runner, python_path=Path("unit-only-python"),
            cutout_workflow=self.cutout, compose_workflow=self.compose)
        for provider in self.production.providers.values():
            provider.assets = adapter
        self.production.registry.allowed_path = lambda value: Path(value).resolve()
        self.source, self.alpha, self.manifest = self.root / "source.mkv", self.root / "alpha.mkv", self.root / "manifest.json"
        self.source.write_bytes(b"isolated source")
        self.alpha.write_bytes(b"isolated alpha")
        self.identity = adapter.implementation_fingerprint("cutout")
        asset = self.repository.state()["assets"][0]
        self.manifest.write_text(json.dumps({"task_id": "unit-task", "full_decode_verified": True,
            "rgb_sha256": file_hash(asset["storage_path"]), "alpha_sha256": file_hash(self.alpha),
            "source_path": str(self.source), "source_sha256": file_hash(self.source),
            "workflow_sha256": file_hash(self.cutout), "implementation_hashes": self.identity}))
        fingerprint = self.production._fingerprint(self.repository.state()["tasks"][0], "person")
        def register(state):
            state["tasks"][0]["context"]["production"]["current_person_asset_id"] = asset["id"]
            current = state["assets"][0]
            current["sha256"] = file_hash(current["storage_path"])
            current["legacy_reference"].update(factory_fingerprint=fingerprint, alpha_path=str(self.alpha),
                alpha_sha256=file_hash(self.alpha), manifest_path=str(self.manifest), manifest_sha256=file_hash(self.manifest),
                source_sha256=file_hash(self.source), workflow_sha256=file_hash(self.cutout))
        self.repository.mutate(register)

    async def test_identity_is_local_and_independent_of_ui_scope_or_restart_cache(self):
        task = self.repository.state()["tasks"][0]
        first = self.production._fingerprint(task, "person")
        for cache in (None, {"checks": []}, {"stage": "quality", "checks": [{"name": "Quality", "status": "READY"}]},
                      {"stage": "prompt", "checks": [{"name": "AI API", "status": "READY"}]},
                      {"checks": [{"implementation_fingerprints": {"cutout": {"invalid_ui_only": "ignored"}}}]}):
            self.production.readiness.cached = cache
            self.assertEqual(self.production._fingerprint(task, "person"), first)
            self.assertEqual(self.production._asset("unit-task", "person")["id"], "unit-asset-v1")
        self.assertTrue(self.runner.calls)
        self.assertTrue(all(command[command.index("--stage") + 1] == "identity" for command in self.runner.calls))

    async def test_model_node_workflow_and_config_changes_invalidate_person(self):
        for path in (self.model, self.node, self.cutout, self.root / "config.yaml"):
            original = path.read_bytes()
            path.write_bytes(original + b"\n")
            self.assertIsNone(self.production._asset("unit-task", "person"), str(path))
            path.write_bytes(original)
            self.assertIsNotNone(self.production._asset("unit-task", "person"))

    async def test_source_alpha_rgb_manifest_changes_and_technical_scope_are_not_reused(self):
        rgb = Path(self.repository.state()["assets"][0]["storage_path"])
        for path in (self.source, self.alpha, rgb, self.manifest):
            original = path.read_bytes()
            path.write_bytes(original + b"\n")
            self.assertIsNone(self.production._asset("unit-task", "person"), str(path))
            path.write_bytes(original)
        self.repository.mutate(lambda state: state["assets"][0]["legacy_reference"].update(execution_scope="TECHNICAL_VALIDATION"))
        self.assertIsNone(self.production._asset("unit-task", "person"))

    async def test_compose_identity_is_stable_without_cutout_model_or_readiness(self):
        task = self.repository.state()["tasks"][0]
        before = self.production._fingerprint(task, "compose")
        self.production.readiness.cached = None
        self.model.write_bytes(b"changed cutout-only model")
        self.assertEqual(self.production._fingerprint(task, "compose"), before)
        (self.comfy / "comfy_extras/nodes_mask.py").write_bytes(b"changed composition node")
        self.assertNotEqual(self.production._fingerprint(task, "compose"), before)


if __name__ == "__main__":
    unittest.main()
