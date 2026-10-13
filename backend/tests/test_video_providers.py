import io
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from app.integrations.legacy import video_worker
from app.integrations.production_contracts import ProductionContext


class VideoProviderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        source, image = self.root / "source.mp4", self.root / "product.png"
        source.write_bytes(b"source fixture")
        image.write_bytes(b"image fixture")
        self.context = dict(task_id="task-unit", project_id="project-unit", country="MX", keyword="body",
                            product_id="product-unit", context_id="context-unit", product_images=[str(image)],
                            source_document="manual", source_row=0, video_id="123", source_video=str(source))

    def prompt_modules(self, sync_status="verified"):
        jobs = [dict(id=f"BG{index:02d}", scene=f"scene {index}", prompt=f"prompt {index}",
                     negative_prompt=f"negative {index}") for index in range(1, 21)]
        return {"prompts": types.SimpleNamespace(prepare_prompts=lambda entry, directory, config, retry: {"jobs": jobs, "method": "fixture"}),
                "tencent_sync": types.SimpleNamespace(read_synced_prompts=lambda *args: {},
                     sync_prompts=lambda entry, prompts, directory, config: {"status": sync_status})}

    def test_context_requires_country_and_maintains_source_identity(self):
        context = ProductionContext.model_validate(self.context)
        self.assertEqual(context.country, "MX")
        with self.assertRaises(ValueError):
            ProductionContext.model_validate({**self.context, "country": ""})

    def test_context_preserves_authorized_external_request_limits(self):
        payload = ProductionContext.model_validate({**self.context,
            "external_budget_required": True, "external_budget_root": str(self.root)}).model_dump()
        self.assertTrue(payload["external_budget_required"])
        self.assertEqual(payload["external_budget_root"], str(self.root))

    def test_twenty_prompt_records_are_independent_and_keep_lineage(self):
        with patch.dict(sys.modules, self.prompt_modules()):
            result = video_worker.prompt_stage("prompt", self.context, self.root, {"tencent_sync_enabled": True})
        self.assertEqual(result["status"], "READY")
        self.assertEqual(len(result["prompts"]), 20)
        self.assertEqual(len({row["path"] for row in result["prompts"]}), 20)
        for row in result["prompts"]:
            self.assertTrue(Path(row["path"]).is_file())
            self.assertEqual(row["context_id"], self.context["context_id"])
            self.assertEqual(row["country"], "MX")

    def test_sync_failure_preserves_all_prompt_records(self):
        with patch.dict(sys.modules, self.prompt_modules("pending")):
            result = video_worker.prompt_stage("prompt", self.context, self.root, {"tencent_sync_enabled": True})
        self.assertEqual(result["status"], "WAITING_REVIEW")
        self.assertEqual(result["error_code"], "TENCENT_SYNC_UNCONFIRMED")
        self.assertEqual(len(result["prompts"]), 20)
        self.assertTrue(all(Path(row["path"]).is_file() for row in result["prompts"]))

    def test_sync_only_cannot_generate_a_paid_prompt(self):
        with patch.dict(sys.modules, self.prompt_modules()):
            with self.assertRaisesRegex(ValueError, "PROMPTS_NOT_READY"):
                video_worker.prompt_stage("prompt_sync", self.context, self.root, {"tencent_sync_enabled": True})

    def test_prompt_identity_separates_country_and_content(self):
        entry = video_worker.entry_for(self.context)
        before = video_worker.cache_identity(entry, {})
        self.assertNotEqual(before, video_worker.cache_identity({**entry, "country": "FR"}, {}))
        Path(entry["reference_images"][0]).write_bytes(b"changed")
        self.assertNotEqual(before, video_worker.cache_identity(entry, {}))

    def test_secrets_are_redacted_across_separate_writes(self):
        stream = io.StringIO()
        with patch.object(video_worker, "SECRET_VALUES", ["very-sensitive-key"]):
            redacted = video_worker.RedactedStream(stream)
            redacted.write("response very-sensitive-")
            redacted.flush()
            self.assertEqual(stream.getvalue(), "")
            redacted.write("key\n")
            self.assertEqual(stream.getvalue(), "response [REDACTED]\n")

    def test_numeric_limits_and_credential_options_do_not_corrupt_hashes(self):
        config = {"quality_max_tokens": 1234, "allow_workflow_api_key": True,
                  "prompt_api_key_env": "VIDEO_AUTO_PROMPT_API_KEY", "prompt_api_key": "test-private-key"}
        original = "abc1234def" + "1" * 54
        common = types.SimpleNamespace(load_config=lambda path: config)
        with patch.dict(sys.modules, {"common": common}), patch.object(video_worker, "SECRET_VALUES", []), patch.dict("os.environ", {}, clear=True), patch.object(sys, "path", list(sys.path)):
            video_worker.load_legacy(self.root)
            self.assertEqual(video_worker.safe(original), original)
            self.assertEqual(video_worker.safe("response test-private-key"), "response [REDACTED]")
            self.assertEqual(video_worker.SECRET_VALUES, ["test-private-key"])

    def test_readonly_source_files_are_unchanged(self):
        source = Path(self.context["source_video"])
        before = source.read_bytes()
        with patch.dict(sys.modules, self.prompt_modules()):
            video_worker.prompt_stage("prompt", self.context, self.root / "factory", {})
        self.assertEqual(source.read_bytes(), before)

    def background_modules(self):
        def prepare(entry, job, directory, media, config):
            if job["id"] == "BG01":
                raise RuntimeError("fixture background failure")
            image = directory / "background.png"
            image.write_bytes(b"fixture image")
            return image, {"image_sha256": video_worker.digest(image), "execution_id": job["id"]}
        return {"chatgpt_rpa": types.SimpleNamespace(prepare_background_image=prepare,
                    require_rpa=lambda config: None, image_dimensions=lambda path: (1080, 1920)),
                "common": types.SimpleNamespace(probe=lambda path: {"width": 1080, "height": 1920})}

    def test_failed_background_does_not_prevent_other_prompt_output(self):
        config = {"output_dir": str(self.root / "rpa"), "_factory_storage_root": str(self.root),
                  "rpa_app_id": "fixture", "rpa_chatgpt_url": "fixture"}
        context = {**self.context, "job": {"id": "BG01", "prompt": "room", "negative_prompt": "people"}}
        with patch.dict(sys.modules, self.background_modules()):
            with self.assertRaisesRegex(RuntimeError, "fixture background failure"):
                video_worker.background_stage(context, self.root, config)
            context["job"] = {"id": "BG02", "prompt": "garden", "negative_prompt": "people"}
            result = video_worker.background_stage(context, self.root, config)
        self.assertEqual(result["status"], "READY")
        asset = result["assets"][0]
        self.assertEqual(asset["prompt_id"], "BG02")
        self.assertTrue(Path(asset["path"]).is_relative_to(self.root / "rpa" / "backgrounds"))
        self.assertFalse((self.root / "legacy_output").exists())
        self.assertEqual(asset["sha256"], video_worker.digest(Path(asset["path"])))

    def test_background_prompt_ids_cannot_escape_job_directory(self):
        with patch.dict(sys.modules, self.background_modules()):
            with self.assertRaisesRegex(ValueError, "one_saved_prompt_pair"):
                video_worker.background_stage({**self.context, "job": {"id": "../BG02", "prompt": "x", "negative_prompt": "x"}}, self.root, {})

    def graph_config(self):
        graph = {"38": {"class_type": "VideoAutoMatteRepair", "inputs": {"images": ["14", 0], "mask": ["11", 1]}},
                 "23": {"class_type": "LayerUtility: ImageCompositeHandleMask", "inputs": {"layer_image": ["38", 0], "layer_mask": ["38", 1]}},
                 "25": {"class_type": "VHS_VideoCombine", "inputs": {"images": ["23", 0], "format": "video/h264-mp4"}}}
        path = self.root / "workflow.json"
        path.write_text(json.dumps(graph), encoding="utf-8")
        config_path = self.root / "config.yaml"
        config_path.write_text("prompt_backend: local_templates\n", encoding="utf-8")
        return {"workflow_api": str(path), "_factory_config_path": str(config_path), "prompt_backend": "local_templates"}

    def test_composite_workflow_is_not_advertised_as_person_export(self):
        _graph, report = video_worker.workflow_description(self.graph_config())
        self.assertFalse(report["person_export_supported"])
        self.assertFalse(report["compose_from_person_supported"])
        self.assertEqual(report["video_outputs"]["25"]["images"], ["23", 0])
        self.assertEqual(report["boundary_code"], "CUTOUT_OUTPUT_BOUNDARY")

    def test_readiness_does_not_equate_comfy_connection_with_production_ready(self):
        calls = []
        class Client:
            def __init__(self, config):
                pass

            def preflight(self, workflow):
                calls.append("GET object_info")
        modules = {"comfy_client": types.SimpleNamespace(ComfyClient=Client, ImageWorkflow=lambda path: object()),
                   "chatgpt_rpa": types.SimpleNamespace(require_rpa=lambda config: self.root / "queue"),
                   "prompts": types.SimpleNamespace(_vision_configuration=lambda config: None)}
        with patch.dict(sys.modules, modules):
            result = video_worker.readiness(self.graph_config())
        checks = {item["name"]: item for item in result["metadata"]["checks"]}
        self.assertEqual(calls, ["GET object_info"])
        self.assertTrue(checks["ComfyUI"]["ok"])
        self.assertFalse(checks["Person Asset"]["ok"])
        self.assertEqual(checks["RPA"]["desktop_loop"], "UNKNOWN")
        self.assertEqual(result["status"], "NOT_CONNECTED")
        self.assertEqual(result["metadata"]["paid_requests"], 0)

    def test_person_boundary_preserves_full_workflow_without_creating_asset(self):
        config = self.graph_config()
        original = Path(config["workflow_api"]).read_bytes()
        result = video_worker.person_boundary("person", {**self.context, "run_id": "run-1"}, config, self.root / "run-1")
        snapshot = result["metadata"]["workflow_snapshot"]
        self.assertEqual(Path(snapshot["path"]).read_bytes(), original)
        self.assertEqual(Path(config["workflow_api"]).read_bytes(), original)
        self.assertEqual(result["status"], "NOT_CONNECTED")
        self.assertEqual(result["assets"], [])
        self.assertFalse(snapshot["submitted"])
        self.assertIsNone(snapshot["prompt_id"])
        next_run = video_worker.person_boundary("person", {**self.context, "run_id": "run-2"}, config, self.root / "run-2")
        self.assertNotEqual(snapshot["path"], next_run["metadata"]["workflow_snapshot"]["path"])

    def test_workflow_with_secret_cannot_be_exported_as_snapshot(self):
        config = self.graph_config()
        path = Path(config["workflow_api"])
        graph = json.loads(path.read_text())
        graph["25"]["inputs"]["api_key"] = "secret fixture"
        path.write_text(json.dumps(graph))
        with self.assertRaisesRegex(ValueError, "workflow_snapshot_contains_secret"):
            video_worker.snapshot_workflow(self.context, config, self.root / "snapshot")
        self.assertFalse((self.root / "snapshot" / "workflow-api.json").exists())

    def test_compose_never_falls_back_to_repeating_source_cutout(self):
        def forbidden(*args, **kwargs):
            self.fail("legacy full render would repeat cutout for each background")
        with patch.dict(sys.modules, {"run": types.SimpleNamespace(render_job=forbidden)}):
            result = video_worker.execute_stage("compose", self.context, self.root, self.graph_config(), self.root / "compose-run")
        self.assertEqual(result["error_code"], "CUTOUT_OUTPUT_BOUNDARY")
        self.assertEqual(result["assets"], [])
        self.assertFalse(result["metadata"]["submitted"])


if __name__ == "__main__":
    unittest.main()
