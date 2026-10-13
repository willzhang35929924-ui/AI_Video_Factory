import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from app.config import Settings
from app.integrations.asset_registry import file_hash, probe_media
from tests import test_asset_registry as registry_tests


WORKER = Path(__file__).parents[1] / "app/integrations/legacy/alpha_preview_worker.py"


class AlphaPreviewRegistryTests(unittest.TestCase):
    def setUp(self):
        fixture = registry_tests.AssetRegistryTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.registry = fixture.registry
        with patch("app.integrations.asset_registry.probe_media", side_effect=fixture.probe):
            self.asset = fixture.register()
        self.manifest = Path(self.asset["legacy_reference"]["manifest_path"])
        self.alpha = Path(self.asset["legacy_reference"]["alpha_path"])
        self.before = {path: path.read_bytes() for path in (self.manifest, self.alpha, Path(self.asset["storage_path"]))}
        self.before_state = fixture.repository.state()
        self.calls = []

    def render(self, command, **options):
        self.calls.append((command, options))
        request_path = Path(command[command.index("--request") + 1])
        result_path = Path(command[command.index("--result") + 1])
        request = json.loads(request_path.read_text())
        output = Path(request["output_path"])
        output.write_bytes(b"unit-alpha-preview-distinct-from-RGB")
        result = {"schema": "factory.alpha-preview.v1", "status": "READY", "inputs": request["inputs"],
                  "path": str(output), "sha256": file_hash(output), "full_decode_verified": True,
                  "diagnostic_only": True, "cutout_inference_executions": 0,
                  "media": {**self.fixture.manifest["media"], "codec": "h264"}}
        result_path.write_text(json.dumps(result))
        return SimpleNamespace(exit_code=0, timed_out=False, cancelled=False)

    def preview(self):
        with patch("app.integrations.asset_registry.ProcessRunner.run", side_effect=self.render), \
                patch("app.integrations.asset_registry.probe_media", side_effect=self.fixture.probe):
            return self.registry.preview(self.asset["id"], "alpha")

    def test_derived_alpha_is_cached_without_changing_mother_asset_or_database(self):
        path, mime = self.preview()
        same, _ = self.preview()
        self.assertEqual(path, same)
        self.assertEqual(mime, "video/mp4")
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(path.is_relative_to(self.fixture.settings.storage_root / "diagnostic_previews"))
        self.assertNotEqual(path.parent, self.alpha.parent)
        command, options = self.calls[0]
        self.assertEqual(Path(command[0]), self.fixture.settings.quality_python)
        self.assertEqual(Path(command[2]), WORKER.resolve())
        self.assertNotIn("run.py", " ".join(command))
        self.assertNotIn("--stage", command)
        self.assertEqual(options["timeout_s"], 600)
        self.assertEqual({file: file.read_bytes() for file in self.before}, self.before)
        self.assertEqual(self.fixture.repository.state(), self.before_state)

    def test_changed_alpha_or_manifest_never_launches_a_preview(self):
        for path in (self.alpha, self.manifest):
            path.write_bytes(self.before[path] + b"changed")
            with self.assertRaises(HTTPException) as raised:
                self.preview()
            self.assertEqual(raised.exception.status_code, 404)
            self.assertEqual(self.calls, [])
            path.write_bytes(self.before[path])

    def test_changed_cached_output_is_rejected_not_regenerated_or_substituted(self):
        path, _ = self.preview()
        path.write_bytes(b"changed-output")
        with self.assertRaises(HTTPException) as raised:
            self.preview()
        self.assertEqual(raised.exception.status_code, 404)
        self.assertEqual(len(self.calls), 1)

    def test_false_decode_or_model_inference_evidence_is_rejected(self):
        path, _ = self.preview()
        sidecar = path.parents[2] / "preview.json"
        original = json.loads(sidecar.read_text())
        for values in ({"full_decode_verified": False}, {"cutout_inference_executions": 1},
                       {"inputs": {**original["inputs"], "alpha_sha256": "0" * 64}},
                       {"path": str(self.alpha)}):
            sidecar.write_text(json.dumps({**original, **values}))
            with self.assertRaises(HTTPException):
                self.preview()
            self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.fixture.repository.state(), self.before_state)

    def test_wrong_preview_alignment_or_non_object_sidecar_never_passes(self):
        path, _ = self.preview()
        with patch("app.integrations.asset_registry.probe_media", return_value={**self.fixture.manifest["media"], "codec": "h264", "frame_count": 11}), \
                self.assertRaises(HTTPException):
            self.registry.preview(self.asset["id"], "alpha")
        (path.parents[2] / "preview.json").write_text("[]")
        with self.assertRaises(HTTPException):
            self.preview()
        self.assertEqual(len(self.calls), 1)

    def test_repaired_version_has_distinct_cache_and_preserves_parent_preview(self):
        parent, _ = self.preview()
        original_preview = parent.read_bytes()
        directory = self.fixture.settings.storage_root / "repair"
        evidence, _, probe = registry_tests.person_fixture(directory, self.fixture.task, rgb=Path(self.asset["storage_path"]))
        Path(evidence["legacy_reference"]["alpha_path"]).write_bytes(b"unit-repaired-alpha")
        evidence, _, probe = registry_tests.person_fixture(directory, self.fixture.task, rgb=Path(self.asset["storage_path"]))
        with patch("app.integrations.asset_registry.probe_media", side_effect=probe):
            repaired = self.registry.register(self.fixture.task, evidence, "person", "unit-repair")
        with patch("app.integrations.asset_registry.ProcessRunner.run", side_effect=self.render), \
                patch("app.integrations.asset_registry.probe_media", side_effect=probe):
            child, _ = self.registry.preview(repaired["id"], "alpha")
        self.assertNotEqual(parent.parents[2], child.parents[2])
        self.assertEqual(parent.read_bytes(), original_preview)
        self.assertEqual(self.alpha.read_bytes(), self.before[self.alpha])
        self.assertEqual(repaired["sha256"], self.asset["sha256"])

    def test_process_failure_preserves_attempt_logs_and_does_not_publish_ready(self):
        with patch("app.integrations.asset_registry.ProcessRunner.run", return_value=SimpleNamespace(exit_code=1, timed_out=False, cancelled=False)), \
                self.assertRaises(HTTPException):
            self.registry.preview(self.asset["id"], "alpha")
        self.assertEqual(list((self.fixture.settings.storage_root / "diagnostic_previews").rglob("preview.json")), [])
        self.assertEqual(self.fixture.repository.state(), self.before_state)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "Local FFmpeg is required")
class AlphaPreviewMediaTests(unittest.TestCase):
    def test_real_alpha_frames_and_rational_fps_are_preserved_without_cutout(self):
        with tempfile.TemporaryDirectory() as temporary:
            storage = Path(temporary).resolve()
            mother, attempt = storage / "mother", storage / "previews/runs/attempt-01"
            mother.mkdir()
            attempt.mkdir(parents=True)
            alpha = mother / "Alpha.mkv"
            subprocess.run(["ffmpeg", "-n", "-v", "error", "-f", "lavfi", "-i",
                "nullsrc=s=64x96:r=30000/1001,format=gray16le,geq=lum='if(lt(X,32),0,65535)'",
                "-frames:v", "13", "-c:v", "ffv1", "-pix_fmt", "gray16le", str(alpha)],
                check=True, capture_output=True, timeout=30)
            manifest = mother / "manifest.json"
            manifest.write_text(json.dumps({"fixture": "UNIT_MEDIA_DIAGNOSTIC_ONLY", "alpha_sha256": file_hash(alpha)}))
            before_alpha, before_manifest = file_hash(alpha), manifest.read_bytes()
            source = probe_media(alpha, count_frames=True)
            inputs = {"asset_id": "unit-alpha", "alpha_path": str(alpha), "alpha_sha256": before_alpha,
                      "manifest_path": str(manifest), "manifest_sha256": file_hash(manifest),
                      "media": {key: source[key] for key in ("width", "height", "fps", "frame_count")},
                      "worker_sha256": file_hash(WORKER), "helper_sha256": file_hash(WORKER.with_name("comfy_asset_worker.py"))}
            output, request, result = attempt / "Alpha.mp4", attempt / "request.json", storage / "previews/preview.json"
            request.write_text(json.dumps({"inputs": inputs, "storage_root": str(storage), "output_path": str(output)}))
            python = Settings().quality_python
            process = subprocess.run([str(python), "-B", str(WORKER), "--request", str(request), "--result", str(result)],
                                     capture_output=True, text=True, timeout=60)
            self.assertEqual(process.returncode, 0, process.stderr)
            evidence = json.loads(result.read_text())
            checked = probe_media(output, count_frames=True)
            self.assertEqual((checked["width"], checked["height"], checked["frame_count"]), (64, 96, 13))
            self.assertAlmostEqual(checked["fps"], 30000 / 1001, places=4)
            self.assertEqual(checked["audio"], [])
            self.assertEqual(evidence["cutout_inference_executions"], 0)
            self.assertTrue(evidence["full_decode_verified"])
            self.assertEqual(evidence["sha256"], file_hash(output))
            pixels = subprocess.run(["ffmpeg", "-v", "error", "-i", str(output), "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                                    check=True, capture_output=True, timeout=30).stdout
            self.assertLess(max(pixels[y * 64 + 4] for y in range(96)), 4)
            self.assertGreater(min(pixels[y * 64 + 60] for y in range(96)), 250)
            self.assertEqual(file_hash(alpha), before_alpha)
            self.assertEqual(manifest.read_bytes(), before_manifest)
