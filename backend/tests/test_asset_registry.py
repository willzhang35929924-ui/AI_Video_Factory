import copy
import csv
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from app.config import Settings
from app.domain import now
from app.integrations.asset_registry import AssetRegistry, file_hash, probe_media
from app.repository import StateRepository

SEED = Path(__file__).parents[1] / "app/seed.json"


def person_fixture(directory, task, *, rgb=None, alpha=None):
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    rgb, alpha = Path(rgb).resolve() if rgb else directory / "RGB.mkv", Path(alpha).resolve() if alpha else directory / "Alpha.mkv"
    for path, content in ((rgb, b"unit-rgb"), (alpha, b"unit-alpha"), (directory / "source.mp4", b"unit-source"),
                          (directory / "Preview_black.mp4", b"unit-preview")):
        if not path.exists():
            path.write_bytes(content)
    media = {"width": 64, "height": 96, "fps": 6, "duration": 2, "codec": "ffv1", "pix_fmt": "gbrp16le", "frame_count": 12, "audio": []}
    manifest = {"schema": "factory.foreground.v1", "status": "READY", "full_decode_verified": True,
        "task_id": task["id"], "context_id": task["context"]["task_context"]["context_id"],
        "alpha_mode": "straight", "precision": {"RGB": "source 8bit; lossless RGB16 storage", "Alpha": "gray16le storage; source precision retained"},
        "rgb_source": "original RGB, uncomposited and not premultiplied", "source_path": str(directory / "source.mp4"),
        "source_sha256": file_hash(directory / "source.mp4"), "workflow_sha256": "f"*64,
        "rgb_sha256": file_hash(rgb), "alpha_sha256": file_hash(alpha), "media": media, "rgb_media": media,
        "alpha_media": {**media, "pix_fmt": "gray16le"}, "previews": [{"background": "black", "filename": "Preview_black.mp4",
            "sha256": file_hash(directory / "Preview_black.mp4"), "full_decode_verified": True}]}
    if rgb.parent != directory:
        manifest.update(schema="factory.foreground.repair.v1", parent_rgb_path=str(rgb))
    (directory / "manifest.json").write_text(json.dumps(manifest))
    reference = {"alpha_path": str(alpha), "alpha_sha256": manifest["alpha_sha256"], "manifest_path": str(directory / "manifest.json"),
        "alpha_mode": "straight", "source_sha256": manifest["source_sha256"], "workflow_sha256": manifest["workflow_sha256"],
        "preview_path": str(directory / "Preview_black.mp4"), "preview_sha256": manifest["previews"][0]["sha256"]}
    evidence = {"path": str(rgb), "sha256": manifest["rgb_sha256"], "legacy_reference": reference}
    def probe(path, **kwargs):
        if Path(path) == alpha:
            return manifest["alpha_media"]
        if Path(path).suffix == ".mp4":
            return {**media, "codec": "h264", "pix_fmt": "yuv420p"}
        return media
    return evidence, manifest, probe


class AssetRegistryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.settings = Settings(storage_root=self.root / "storage", legacy_tiktok_auto_root=self.root / "legacy", database_url="sqlite:///" + str(self.root / "registry.db"))
        self.repository = StateRepository(self.settings.database_url, SEED)
        self.addCleanup(self.repository.engine.dispose)
        self.registry = AssetRegistry(self.repository, self.settings)
        task = copy.deepcopy(self.repository.state()["tasks"][0])
        task.update(id="registry-unit", execution_mode="integration", source_video_id=None, created_at=now(), updated_at=now(),
            context={"task_context": {"context_id": "registry-unit-context"}}, collector_result={})
        self.repository.mutate(lambda state: state["tasks"].append(task))
        self.task = task
        self.evidence, self.manifest, self.probe = person_fixture(self.settings.storage_root / "mother", task)

    def register(self, **kwargs):
        return self.registry.register(self.task, self.evidence, "person", "run-unit", **kwargs)

    def technical_fixture(self):
        directory = self.settings.storage_root / "technical_validation" / "unit"
        evidence, manifest, probe = person_fixture(directory, self.task)
        images, legacy_images = [], []
        legacy_root = self.settings.legacy_tiktok_auto_root
        (legacy_root / "data/product_references").mkdir(parents=True)
        for index in range(2):
            image, legacy_image = directory / f"product-{index}.jpg", legacy_root / f"data/product_references/product-{index}.jpg"
            image.write_bytes(f"unit-product-{index}".encode())
            legacy_image.write_bytes(image.read_bytes())
            images.append(str(image))
            legacy_images.append(str(legacy_image))
        self.task["context"]["task_context"] = {"task_id": self.task["id"], "project_id": self.task["project_id"],
            "product_id": self.task["product_id"], "country": self.task["country"], "keyword": self.task["keyword"],
            "context_id": "legacy-context", "source_document": "https://docs.qq.com/unit", "source_row": 2, "product_images": images}
        self.repository.mutate(lambda state: next(task for task in state["tasks"] if task["id"] == self.task["id"]).update(context=self.task["context"]))
        binding = {"country": self.task["country"], "keyword": self.task["keyword"], "legacy_context_id": "legacy-context",
            "source_document": "https://docs.qq.com/unit", "source_row": 2, "file_path": str(directory / "source.mp4"),
            "sha256": manifest["source_sha256"], "bytes": (directory / "source.mp4").stat().st_size,
            "vidbee_status": "completed", "vidbee_task_id": "unit-vidbee", "video_id": "123",
            "video_url": "https://www.tiktok.com/@unit/video/123", "product_image": legacy_images[0],
            "legacy_decision": "manual_review", "not_factory_accepted_task": True}
        manifest.update(task_id="technical-unit", context_id="technical-source-sha", execution_scope="TECHNICAL_VALIDATION_NO_BUSINESS_TASK", resolved_source=binding)
        (directory / "source_binding.json").write_text(json.dumps(binding))
        path = directory / "manifest.json"
        path.write_text(json.dumps(manifest))
        row = {key: str(binding[key]) for key in ("country", "keyword", "source_document", "source_row", "video_id", "vidbee_task_id")}
        row.update(context_id="legacy-context", url=binding["video_url"], reference_images="|".join(legacy_images))
        (legacy_root / "output").mkdir()
        with (legacy_root / "output/candidates.csv").open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(row))
            writer.writeheader()
            writer.writerow(row)
        return path, manifest, probe

    def test_person_ready_has_pair_manifest_and_verified_browser_preview(self):
        with patch("app.integrations.asset_registry.probe_media", side_effect=self.probe):
            asset = self.register()
            path, mime = self.registry.preview(asset["id"])
        self.assertEqual(asset["status"], "READY")
        self.assertTrue(asset["legacy_reference"]["full_decode_verified"])
        self.assertEqual(asset["legacy_reference"]["manifest_sha256"], file_hash(Path(self.evidence["legacy_reference"]["manifest_path"])))
        self.assertEqual(path.name, "Preview_black.mp4")
        self.assertEqual(mime, "video/mp4")

    def test_missing_or_false_contract_never_registers_ready(self):
        path = Path(self.evidence["legacy_reference"]["manifest_path"])
        before = self.repository.state()["assets"]
        for values in ({"full_decode_verified": False}, {"schema": "factory.composition.v1"}, {"alpha_mode": "premultiplied"},
                       {"precision": {}}, {"rgb_source": "already composited"}, {"task_id": "other-task"},
                       {"rgb_sha256": "0"*64}, {"media": {**self.manifest["media"], "frame_count": 11}}):
            path.write_text(json.dumps({**self.manifest, **values}))
            with patch("app.integrations.asset_registry.probe_media", side_effect=self.probe), self.assertRaises(ValueError):
                self.register()
            self.assertEqual(self.repository.state()["assets"], before)

    def test_missing_manifest_and_legacy_h264_composite_are_rejected(self):
        with patch("app.integrations.asset_registry.probe_media", side_effect=self.probe):
            original = self.evidence["legacy_reference"].pop("manifest_path")
            with self.assertRaisesRegex(ValueError, "PERSON_MANIFEST_REQUIRED"):
                self.register()
            self.evidence["legacy_reference"]["manifest_path"] = original
        media = {**self.manifest["rgb_media"], "codec": "h264", "pix_fmt": "yuv420p"}
        with patch("app.integrations.asset_registry.probe_media", side_effect=[media, self.manifest["alpha_media"]]), self.assertRaisesRegex(ValueError, "LOSSLESS"):
            self.register()

    def test_unknown_frame_count_fails_clearly_without_registering_ready(self):
        before = self.repository.state()["assets"]
        for media, alpha in (({key: value for key, value in self.manifest["rgb_media"].items() if key != "frame_count"}, self.manifest["alpha_media"]),
                             (self.manifest["rgb_media"], {**self.manifest["alpha_media"], "frame_count": None})):
            with patch("app.integrations.asset_registry.probe_media", side_effect=[media, alpha]), self.assertRaisesRegex(ValueError, "PERSON_FRAME_COUNT_REQUIRED"):
                self.register()
            self.assertEqual(self.repository.state()["assets"], before)

    def test_counted_video_duration_is_not_extended_by_audio_container(self):
        data = {"streams": [{"codec_type": "video", "width": 64, "height": 96, "avg_frame_rate": "6/1", "nb_read_frames": "12", "codec_name": "ffv1"},
                            {"codec_type": "audio", "codec_name": "aac", "duration": "2.3"}], "format": {"duration": "2.3"}}
        process = subprocess.CompletedProcess([], 0, json.dumps(data), "")
        with patch("app.integrations.asset_registry.subprocess.run", return_value=process):
            media = probe_media(self.evidence["path"], count_frames=True)
        self.assertEqual(media["duration"], 2)
        self.assertEqual(media["container_duration"], 2.3)
        self.assertEqual(media["audio"][0]["duration"], "2.3")

    def test_technical_pair_preserves_origin_and_never_changes_business_task(self):
        path, _manifest, probe = self.technical_fixture()
        original_bytes = path.read_bytes()
        before = self.repository.state()
        with patch("app.integrations.asset_registry.probe_media", side_effect=probe):
            asset = self.registry.register_technical_pair(self.task, path, "technical-run")
            same = self.registry.register_technical_pair(self.task, path, "technical-run-2")
        self.assertEqual(asset["id"], same["id"])
        self.assertEqual(asset["legacy_reference"]["execution_scope"], "TECHNICAL_VALIDATION")
        self.assertEqual(asset["legacy_reference"]["origin_task_id"], "technical-unit")
        self.assertEqual(asset["legacy_reference"]["technical_binding"]["product_image_sha256"], [file_hash(value) for value in self.task["context"]["task_context"]["product_images"]])
        self.assertEqual(asset["legacy_video_id"], "123")
        self.assertEqual(path.read_bytes(), original_bytes)
        after = self.repository.state()
        for key in ("tasks", "stages", "runs", "collector", "legacy_bindings"):
            self.assertEqual(after[key], before[key])

    def test_technical_binding_rejects_changed_context_images_and_origin(self):
        path, _manifest, probe = self.technical_fixture()
        before = self.repository.state()["assets"]
        for key, value in (("country", "wrong-country"), ("keyword", "wrong-keyword"), ("source_row", 3), ("context_id", "wrong-context"),
                           ("product_images", list(reversed(self.task["context"]["task_context"]["product_images"])) )):
            task = copy.deepcopy(self.task)
            task["context"]["task_context"][key] = value
            if key in task:
                task[key] = value
            with patch("app.integrations.asset_registry.probe_media", side_effect=probe), self.assertRaises(ValueError):
                self.registry.register_technical_pair(task, path, "technical-run")
            self.assertEqual(self.repository.state()["assets"], before)

    def test_technical_repair_and_composition_cannot_drop_technical_scope(self):
        path, manifest, probe = self.technical_fixture()
        with patch("app.integrations.asset_registry.probe_media", side_effect=probe):
            parent = self.registry.register_technical_pair(self.task, path, "technical-run")
            compose = self.registry.register(self.task, {"path": parent["legacy_reference"]["preview_path"]}, "compose", "technical-compose", person=parent)
        self.assertEqual(compose["legacy_reference"]["execution_scope"], "TECHNICAL_VALIDATION")
        self.assertEqual(compose["legacy_reference"]["technical_person_asset_id"], parent["id"])
        repair, repaired, repair_probe = person_fixture(self.settings.storage_root / "repair-technical", self.task, rgb=Path(parent["storage_path"]))
        repaired.update(task_id=manifest["task_id"], context_id=manifest["context_id"], execution_scope=manifest["execution_scope"], resolved_source=manifest["resolved_source"],
            source_path=manifest["source_path"], source_sha256=manifest["source_sha256"])
        Path(repair["legacy_reference"]["manifest_path"]).write_text(json.dumps(repaired))
        repair["legacy_reference"] = {**parent["legacy_reference"], **repair["legacy_reference"], "source_sha256": manifest["source_sha256"]}
        with patch("app.integrations.asset_registry.probe_media", side_effect=repair_probe):
            new = self.registry.register(self.task, repair, "person", "technical-repair")
        self.assertEqual(new["legacy_reference"]["execution_scope"], "TECHNICAL_VALIDATION")
        self.assertEqual(new["legacy_reference"]["origin_manifest_sha256"], parent["legacy_reference"]["origin_manifest_sha256"])
        invalid = copy.deepcopy(repair)
        invalid["legacy_reference"].pop("execution_scope")
        with patch("app.integrations.asset_registry.probe_media", side_effect=repair_probe), self.assertRaisesRegex(ValueError, "TECHNICAL_BINDING_REQUIRED"):
            self.registry.register(self.task, invalid, "person", "technical-stripped")

    def test_independent_technical_background_is_real_image_not_fake_parent(self):
        path = self.settings.storage_root / "background.png"
        path.write_bytes(b"unit-image")
        evidence = {"path": str(path), "sha256": file_hash(path), "legacy_reference": {"execution_scope": "TECHNICAL_VALIDATION",
            "background_origin": "existing actual image; no new generation"}}
        media = {**self.manifest["media"], "codec": "png", "fps": 0, "duration": 0}
        before = self.repository.state()
        with patch("app.integrations.asset_registry.probe_media", return_value=media):
            asset = self.registry.register(self.task, evidence, "background", "technical-background")
            same = self.registry.register(self.task, evidence, "background", "technical-background-repeat")
        self.assertEqual(asset["id"], same["id"])
        self.assertEqual(asset["sha256"], file_hash(path))
        self.assertEqual(asset["legacy_reference"]["execution_scope"], "TECHNICAL_VALIDATION")
        self.assertIsNone(asset["legacy_reference"]["person_asset_id"])
        self.assertNotIn("technical_person_asset_id", asset["legacy_reference"])
        for key in ("tasks", "stages", "runs", "collector"):
            self.assertEqual(self.repository.state()[key], before[key])

    def test_technical_image_cannot_be_video_or_hide_as_business_background(self):
        path = self.settings.storage_root / "background.png"
        path.write_bytes(b"unit-image")
        evidence = {"path": str(path), "legacy_reference": {"execution_scope": "TECHNICAL_VALIDATION"}}
        with patch("app.integrations.asset_registry.probe_media", return_value={**self.manifest["media"], "codec": "h264"}), self.assertRaisesRegex(ValueError, "TECHNICAL_BACKGROUND_IMAGE_REQUIRED"):
            self.registry.register(self.task, evidence, "background", "technical-not-image")
        with patch("app.integrations.asset_registry.probe_media", return_value={**self.manifest["media"], "codec": "png"}):
            business = self.registry.register(self.task, {"path": str(path)}, "background", "business-background")
            technical = self.registry.register(self.task, evidence, "background", "technical-background")
        self.assertNotEqual(business["id"], technical["id"])
        self.assertNotEqual(business["legacy_reference"].get("factory_fingerprint"), technical["legacy_reference"]["factory_fingerprint"])
        with self.assertRaisesRegex(ValueError, "TECHNICAL_PARENT_REQUIRED"):
            self.registry.register(self.task, evidence, "compose", "technical-no-parent")

    def test_preview_channels_do_not_fabricate_or_serve_raw_ffv1(self):
        with patch("app.integrations.asset_registry.probe_media", side_effect=self.probe):
            asset = self.register()
            with self.assertRaises(HTTPException) as error:
                self.registry.preview(asset["id"], "source")
            self.assertEqual(error.exception.status_code, 404)
            with self.assertRaises(HTTPException) as error:
                self.registry.preview(asset["id"], "../../RGB.mkv")
            self.assertEqual(error.exception.status_code, 400)
            Path(self.evidence["legacy_reference"]["preview_path"]).write_bytes(b"tampered-preview")
            with self.assertRaises(HTTPException):
                self.registry.preview(asset["id"])

    def test_changed_mask_creates_immutable_new_version_with_reused_rgb(self):
        with patch("app.integrations.asset_registry.probe_media", side_effect=self.probe):
            first = self.register()
        directory = self.settings.storage_root / "repair"
        second, _manifest, probe = person_fixture(directory, self.task, rgb=Path(self.evidence["path"]))
        alpha = Path(second["legacy_reference"]["alpha_path"])
        alpha.write_bytes(b"changed-alpha")
        second, _manifest, probe = person_fixture(directory, self.task, rgb=Path(self.evidence["path"]))
        with patch("app.integrations.asset_registry.probe_media", side_effect=probe):
            new = self.registry.register(self.task, second, "person", "run-repair")
        self.assertNotEqual(first["id"], new["id"])
        self.assertEqual(new["legacy_reference"]["version"], 2)
        self.assertEqual(new["sha256"], first["sha256"])
        self.assertEqual(Path(self.evidence["legacy_reference"]["alpha_path"]).read_bytes(), b"unit-alpha")

    def test_explicit_same_rgb_revision_reuses_verified_parent_probe_but_checks_hash_and_new_alpha(self):
        with patch("app.integrations.asset_registry.probe_media", side_effect=self.probe):
            parent = self.register()
        evidence, _manifest, probe = person_fixture(self.settings.storage_root / "bound-revision", self.task, rgb=Path(parent["storage_path"]))
        evidence["legacy_reference"]["parent_person_asset_id"] = parent["id"]
        before = next(row for row in self.repository.state()["assets"] if row["id"] == parent["id"])
        with patch("app.integrations.asset_registry.probe_media", side_effect=probe) as called, patch("app.integrations.asset_registry.file_hash", wraps=file_hash) as hashed:
            revision = self.registry.register(self.task, evidence, "person", "unit-bound-revision")
        self.assertNotIn(Path(parent["storage_path"]), [Path(call.args[0]) for call in called.call_args_list])
        self.assertIn(Path(parent["storage_path"]), [Path(call.args[0]) for call in hashed.call_args_list])
        self.assertIn(Path(evidence["legacy_reference"]["alpha_path"]), [Path(call.args[0]) for call in called.call_args_list if call.kwargs.get("count_frames")])
        self.assertEqual(revision["probe_metadata"], parent["probe_metadata"])
        self.assertEqual(revision["legacy_reference"]["parent_person_asset_id"], parent["id"])
        self.assertEqual(next(row for row in self.repository.state()["assets"] if row["id"] == parent["id"]), before)

    def test_revision_probe_reuse_requires_exact_verified_business_parent(self):
        with patch("app.integrations.asset_registry.probe_media", side_effect=self.probe):
            parent = self.register()
        evidence, _manifest, _probe = person_fixture(self.settings.storage_root / "fallback-revision", self.task, rgb=Path(parent["storage_path"]))
        evidence["legacy_reference"]["parent_person_asset_id"] = parent["id"]
        mutations = [("task_id", "other-task"), ("type", "source"), ("registry_status", "candidate"),
            ("sha256", "0" * 64), ("size", parent["size"] + 1), ("storage_path", str(self.settings.storage_root / "different-rgb.mkv")),
            ("legacy_reference", {**parent["legacy_reference"], "full_decode_verified": False}),
            ("legacy_reference", {**parent["legacy_reference"], "execution_scope": "TECHNICAL_VALIDATION"}),
            ("probe_metadata", {**parent["probe_metadata"], "frame_count": None}),
            ("probe_metadata", {**parent["probe_metadata"], "width": 1})]
        for key, value in mutations:
            with self.subTest(key=key, value=value):
                state = self.repository.state()
                next(row for row in state["assets"] if row["id"] == parent["id"])[key] = value
                with patch.object(self.repository, "state", return_value=state), patch("app.integrations.asset_registry.probe_media", side_effect=ValueError("UNIT_RGB_PROBE_REQUIRED")) as called, self.assertRaisesRegex(ValueError, "UNIT_RGB_PROBE_REQUIRED"):
                    self.registry.register(self.task, evidence, "person", "unit-fallback")
                self.assertEqual(Path(called.call_args.args[0]), Path(parent["storage_path"]))
                self.assertTrue(called.call_args.kwargs["count_frames"])
        evidence["legacy_reference"].pop("parent_person_asset_id")
        with patch("app.integrations.asset_registry.probe_media", side_effect=ValueError("UNIT_RGB_PROBE_REQUIRED")), self.assertRaisesRegex(ValueError, "UNIT_RGB_PROBE_REQUIRED"):
            self.registry.register(self.task, evidence, "person", "unit-unbound")

    def test_changed_rgb_never_reuses_parent_probe_or_bypasses_hash_validation(self):
        with patch("app.integrations.asset_registry.probe_media", side_effect=self.probe):
            parent = self.register()
        evidence, _manifest, _probe = person_fixture(self.settings.storage_root / "changed-rgb", self.task, rgb=Path(parent["storage_path"]))
        evidence["legacy_reference"]["parent_person_asset_id"] = parent["id"]
        Path(parent["storage_path"]).write_bytes(b"different-unit-rgb")
        with patch("app.integrations.asset_registry.probe_media") as called, self.assertRaisesRegex(ValueError, "ASSET_HASH_MISMATCH"):
            self.registry.register(self.task, evidence, "person", "unit-changed-rgb")
        called.assert_not_called()
        evidence["sha256"] = file_hash(Path(parent["storage_path"]))
        with patch("app.integrations.asset_registry.probe_media", side_effect=ValueError("UNIT_RGB_PROBE_REQUIRED")), self.assertRaisesRegex(ValueError, "UNIT_RGB_PROBE_REQUIRED"):
            self.registry.register(self.task, evidence, "person", "unit-new-rgb")

    def test_bound_revision_still_rejects_changed_alpha(self):
        with patch("app.integrations.asset_registry.probe_media", side_effect=self.probe):
            parent = self.register()
        evidence, _manifest, probe = person_fixture(self.settings.storage_root / "changed-alpha", self.task, rgb=Path(parent["storage_path"]))
        evidence["legacy_reference"]["parent_person_asset_id"] = parent["id"]
        Path(evidence["legacy_reference"]["alpha_path"]).write_bytes(b"different-unit-alpha")
        before = self.repository.state()["assets"]
        with patch("app.integrations.asset_registry.probe_media", side_effect=probe), self.assertRaisesRegex(ValueError, "PERSON_ALPHA_HASH_MISMATCH"):
            self.registry.register(self.task, evidence, "person", "unit-changed-alpha")
        self.assertEqual(self.repository.state()["assets"], before)

    def test_failed_relational_link_rolls_back_ready_asset(self):
        before = self.repository.state()["assets"]
        preview = Path(self.evidence["legacy_reference"]["preview_path"])
        with patch("app.integrations.asset_registry.probe_media", side_effect=self.probe), self.assertRaises(IntegrityError):
            self.registry.register(self.task, {"path": str(preview)}, "background", "run-background", prompt_id="missing-prompt-fk")
        self.assertEqual(self.repository.state()["assets"], before)

    def test_failure_after_asset_write_rolls_back_person_ready(self):
        before = self.repository.state()["assets"]
        with patch("app.integrations.asset_registry.probe_media", side_effect=self.probe), patch.object(self.repository, "_register_real_asset_links", side_effect=ValueError("unit-link-failure")), self.assertRaisesRegex(ValueError, "unit-link-failure"):
            self.register()
        self.assertEqual(self.repository.state()["assets"], before)

    def test_real_ffmpeg_rgb_alpha_pair_and_preview_pass_actual_counted_probe(self):
        directory = self.settings.storage_root / "actual-media"
        directory.mkdir()
        source = directory / "source.mp4"
        def run(arguments):
            subprocess.run(["ffmpeg", "-n", "-v", "error", *arguments], shell=False, check=True, capture_output=True, timeout=30)
        run(["-f", "lavfi", "-i", "color=orange:size=64x96:rate=6:duration=2", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source)])
        run(["-i", str(source), "-c:v", "ffv1", "-level", "3", "-g", "1", "-pix_fmt", "gbrp16le", str(directory / "RGB.mkv")])
        run(["-f", "lavfi", "-i", "nullsrc=size=64x96:rate=6:duration=2,geq=lum=X/W*255", "-c:v", "ffv1", "-level", "3", "-g", "1", "-pix_fmt", "gray16le", str(directory / "Alpha.mkv")])
        run(["-i", str(directory / "RGB.mkv"), "-i", str(directory / "Alpha.mkv"), "-filter_complex",
             "[0:v]settb=expr=1/6,setpts=N[rgb];[1:v]settb=expr=1/6,setpts=N[a];[rgb][a]alphamerge[fg];color=black:size=64x96:rate=6:duration=2[bg];[bg][fg]overlay=shortest=1:format=auto,format=yuv420p[out]",
             "-map", "[out]", "-frames:v", "12", "-r", "6", "-fps_mode", "cfr", "-c:v", "libx264", str(directory / "Preview_black.mp4")])
        evidence, manifest, _probe = person_fixture(directory, self.task)
        manifest.update(rgb_media=probe_media(directory / "RGB.mkv", count_frames=True), alpha_media=probe_media(directory / "Alpha.mkv", count_frames=True))
        (directory / "manifest.json").write_text(json.dumps(manifest))
        asset = self.registry.register(self.task, evidence, "person", "real-media-unit")
        self.assertEqual(asset["probe_metadata"]["frame_count"], 12)
        self.assertEqual(self.registry.preview(asset["id"])[0].name, "Preview_black.mp4")
